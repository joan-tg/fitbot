"""Protected endpoint for creating persistent, one-off booking schedules."""

import hashlib
import hmac
import json
import logging
import os
import re
import sys
import uuid
from urllib.parse import parse_qs, urlsplit
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from redis_store import RedisUnavailable, UpstashRedis  # noqa: E402
from scheduled import load_config  # noqa: E402

logger = logging.getLogger("fitbot.bookings")
MAX_BODY_BYTES = 16_384
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")
ALLOWED_REQUEST_FIELDS = {"person_id", "date", "time", "name"}
CLAIMABLE_STATUSES = ("pending_schedule", "schedule_failed")
QSTASH_MAX_DELAY = timedelta(days=7)


def _respond(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _authorized(handler):
    expected = os.environ.get("FITBOT_ADMIN_TOKEN", "")
    supplied = handler.headers.get("Authorization", "")
    if not expected or not supplied.startswith("Bearer "):
        return False
    return hmac.compare_digest(supplied[7:], expected)


def _next_month_first_day(value):
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _build_booking(payload, config, now=None, booking_id=None):
    """Validate a client request and derive its opening time from policy."""
    if not isinstance(payload, dict):
        raise ValueError("Body must be a JSON object")
    if set(payload) != ALLOWED_REQUEST_FIELDS:
        raise ValueError("Body must contain person_id, date, time and name only")

    timezone_name = config.get("timezone", "Europe/Madrid")
    local_tz = ZoneInfo(timezone_name)
    now = now or datetime.now(local_tz)
    if now.tzinfo is None:
        raise ValueError("Current time must include a timezone")
    now = now.astimezone(local_tz)

    person_id = payload.get("person_id")
    members = config.get("members", {})
    if (
        not isinstance(person_id, str)
        or person_id not in members
        or person_id not in ("person_1", "person_2")
    ):
        raise ValueError("Unknown person_id")

    raw_date = payload.get("date")
    raw_time = payload.get("time")
    name = payload.get("name")
    if not isinstance(raw_date, str) or not isinstance(raw_time, str):
        raise ValueError("date and time must be strings")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
        raise ValueError("date must use YYYY-MM-DD")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw_time):
        raise ValueError("time must use HH:MM")
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise ValueError("Invalid activity name")

    try:
        target_date = date.fromisoformat(raw_date)
        target_time = time.fromisoformat(raw_time)
    except ValueError as error:
        raise ValueError("Invalid class date or time") from error

    policy = config.get("booking_policy", {})
    horizon = policy.get("horizon", {})
    if horizon.get("type") != "calendar_month_plus_days":
        raise ValueError("Unsupported booking horizon policy")
    next_month_days = horizon.get("next_month_days")
    if not isinstance(next_month_days, int) or next_month_days < 0:
        raise ValueError("Invalid next-month booking horizon")

    first_next_month = _next_month_first_day(now.date())
    last_allowed_date = first_next_month + timedelta(days=next_month_days - 1)
    if target_date < now.date() or target_date > last_allowed_date:
        raise ValueError(
            "Class date must be in the current calendar month or the first "
            "three days of next month"
        )

    activities = config.get("activities", {})
    matching_types = [
        activity_type
        for activity_type in ("class", "open_box")
        if name in activities.get(activity_type, [])
    ]
    if len(matching_types) != 1:
        raise ValueError("Activity is not in the configured allowlist")
    activity_type = matching_types[0]

    naive_start = datetime.combine(target_date, target_time)
    start_fold_0 = naive_start.replace(tzinfo=local_tz, fold=0)
    start_fold_1 = naive_start.replace(tzinfo=local_tz, fold=1)
    roundtrip_aware = start_fold_0.astimezone(timezone.utc).astimezone(local_tz)
    roundtrip = roundtrip_aware.replace(tzinfo=None)
    if roundtrip != naive_start:
        raise ValueError("Class time does not exist in the configured timezone")
    if start_fold_0.utcoffset() != start_fold_1.utcoffset():
        raise ValueError("Ambiguous class time in the configured timezone")

    lead_hours = policy.get("opening_lead_hours")
    if lead_hours != 72:
        raise ValueError("Booking policy must use a 72-hour opening lead")
    # Subtract 72 elapsed hours in UTC, then convert back to local time.
    opens_at = (
        start_fold_0.astimezone(timezone.utc) - timedelta(hours=lead_hours)
    ).astimezone(local_tz)
    if opens_at <= now:
        raise ValueError("Booking opening time has already passed")
    qstash_delay = opens_at.astimezone(timezone.utc) - now.astimezone(timezone.utc)
    if qstash_delay > QSTASH_MAX_DELAY:
        raise ValueError(
            "Booking opening time must be within QStash's 7-day free-plan limit"
        )

    return {
        "id": booking_id or uuid.uuid4().hex,
        "person_id": person_id,
        "date": target_date.isoformat(),
        "time": raw_time,
        "name": name,
        "activity_type": activity_type,
        "opens_at": opens_at.isoformat(timespec="seconds"),
        "status": "pending_schedule",
        "created_at": now.isoformat(timespec="seconds"),
    }


def _publish_booking(booking, redis):
    """Claim a stored booking and publish only its ID to QStash."""
    claimed = redis.update_booking_status(
        booking["id"],
        "scheduling",
        expected_statuses=list(CLAIMABLE_STATUSES),
        metadata={"error_code": None},
    )
    if claimed is None:
        latest = redis.get_booking(booking["id"])
        if latest is None:
            raise RedisUnavailable("Booking disappeared during scheduling")
        return latest, False

    opens_at = datetime.fromisoformat(booking["opens_at"])
    not_before = int(opens_at.astimezone(timezone.utc).timestamp())
    destination = os.environ["FITBOT_WEBHOOK_URL"]
    headers = {
        "Authorization": f"Bearer {os.environ['QSTASH_TOKEN']}",
        "Content-Type": "application/json",
        "Upstash-Not-Before": str(not_before),
    }
    bypass = os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET")
    if bypass:
        headers["Upstash-Forward-x-vercel-protection-bypass"] = bypass

    try:
        response = requests.post(
            f"https://qstash.upstash.io/v2/publish/{destination}",
            json={"booking_id": booking["id"]},
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        message_id = response.json().get("messageId")
        if not message_id:
            raise requests.RequestException("QStash returned no message ID")
    except (requests.RequestException, ValueError, AttributeError):
        try:
            redis.update_booking_status(
                booking["id"],
                "schedule_failed",
                expected_statuses=["scheduling"],
                metadata={"error_code": "qstash_publish_failed"},
            )
        except RedisUnavailable:
            logger.exception("Could not record failed QStash publication")
        raise

    try:
        scheduled = redis.update_booking_status(
            booking["id"],
            "scheduled",
            expected_statuses=["scheduling"],
            metadata={
                "message_id": message_id,
                "scheduled_at": booking["opens_at"],
            },
        )
    except RedisUnavailable:
        logger.exception("QStash accepted the message but Redis update failed")
        raise
    if scheduled is None:
        latest = redis.get_booking(booking["id"])
        return latest or claimed, False
    return scheduled, True


def _create_booking(payload, idempotency_key, prepared_booking=None):
    deployment_env = os.environ.get("VERCEL_ENV")
    if deployment_env not in ("preview", "production"):
        raise PermissionError(
            "Booking scheduling is available only in Vercel Preview or Production"
        )
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key or ""):
        raise ValueError("A valid Idempotency-Key header is required")

    config = load_config(ROOT / "reservas.json")
    booking = prepared_booking or _build_booking(payload, config)

    qstash_token = os.environ.get("QSTASH_TOKEN")
    webhook_url = os.environ.get("FITBOT_WEBHOOK_URL")
    bypass = os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET")
    if not qstash_token or not webhook_url:
        raise RuntimeError("QStash publishing is not configured")
    if deployment_env == "preview" and not bypass:
        raise RuntimeError("Vercel Preview bypass is not configured")

    redis = UpstashRedis()
    created, booking_id = redis.create_booking_idempotent(booking, idempotency_key)
    stored = booking if created else redis.get_booking(booking_id)
    if stored is None:
        raise RedisUnavailable("Idempotent booking record is unavailable")

    if stored["status"] == "scheduled":
        return stored, False
    if stored["status"] not in CLAIMABLE_STATUSES:
        return stored, False

    scheduled, published = _publish_booking(stored, redis)
    return scheduled, published


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if not _authorized(self):
            return _respond(self, 401, {"error": "Unauthorized"})
        if os.environ.get("VERCEL_ENV") not in ("preview", "production"):
            return _respond(
                self,
                403,
                {"error": "Vercel Preview or Production environment only"},
            )
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return _respond(self, 400, {"error": "Invalid Content-Length"})
        if length <= 0 or length > MAX_BODY_BYTES:
            return _respond(self, 413, {"error": "Invalid request body size"})

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            idempotency_key = self.headers.get("Idempotency-Key")
            booking, published = _create_booking(payload, idempotency_key)
            status = (
                201
                if published
                else (202 if booking["status"] == "scheduling" else 200)
            )
            return _respond(
                self,
                status,
                {
                    "status": booking["status"],
                    "booking": booking,
                    "qstash_published": published,
                },
            )
        except PermissionError as error:
            return _respond(self, 403, {"error": str(error)})
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            return _respond(self, 400, {"error": str(error)})
        except requests.RequestException:
            logger.exception("Could not publish booking to QStash")
            return _respond(self, 502, {"error": "QStash scheduling failed"})
        except RedisUnavailable:
            logger.exception("Redis booking store unavailable")
            return _respond(self, 503, {"error": "Booking store unavailable"})
        except Exception:
            logger.exception("Unexpected error creating booking")
            return _respond(self, 503, {"error": "Booking scheduling unavailable"})

    def do_PATCH(self):
        """Replace a pending booking; the old QStash delivery becomes harmless."""
        if not _authorized(self):
            return _respond(self, 401, {"error": "Unauthorized"})
        if os.environ.get("VERCEL_ENV") not in ("preview", "production"):
            return _respond(
                self,
                403,
                {"error": "Vercel Preview or Production environment only"},
            )
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return _respond(self, 400, {"error": "Invalid Content-Length"})
        if length <= 0 or length > MAX_BODY_BYTES:
            return _respond(self, 413, {"error": "Invalid request body size"})

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != (
                ALLOWED_REQUEST_FIELDS | {"id"}
            ):
                raise ValueError(
                    "Body must contain id, person_id, date, time and name only"
                )
            old_id = payload["id"]
            if not isinstance(old_id, str):
                raise ValueError("Invalid booking id")
            if not UpstashRedis.BOOKING_ID_PATTERN.fullmatch(old_id):
                raise ValueError("Invalid booking id")

            new_payload = {key: payload[key] for key in ALLOWED_REQUEST_FIELDS}
            idempotency_key = self.headers.get("Idempotency-Key")
            if not IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key or ""):
                raise ValueError("A valid Idempotency-Key header is required")

            config = load_config(ROOT / "reservas.json")
            prepared_booking = _build_booking(new_payload, config)
            if not os.environ.get("QSTASH_TOKEN") or not os.environ.get(
                "FITBOT_WEBHOOK_URL"
            ):
                raise RuntimeError("QStash publishing is not configured")
            if os.environ.get("VERCEL_ENV") == "preview" and not os.environ.get(
                "VERCEL_AUTOMATION_BYPASS_SECRET"
            ):
                raise RuntimeError("Vercel Preview bypass is not configured")

            redis = UpstashRedis()
            previous = redis.get_booking(old_id)
            if previous is None:
                return _respond(self, 404, {"error": "Booking not found"})

            allowed = [
                "pending_schedule",
                "schedule_failed",
                "scheduling",
                "scheduled",
            ]
            replacement_key_hash = hashlib.sha256(
                idempotency_key.encode("utf-8")
            ).hexdigest()
            previous_status = previous.get("status")

            if previous_status in allowed:
                replaced_at = datetime.now(ZoneInfo("Europe/Madrid")).isoformat(
                    timespec="seconds"
                )
                replaced = redis.update_booking_status(
                    old_id,
                    "replaced",
                    expected_statuses=allowed,
                    metadata={
                        "replaced_at": replaced_at,
                        "replacement_key_hash": replacement_key_hash,
                    },
                )
                if replaced is None:
                    latest = redis.get_booking(old_id)
                    return _respond(
                        self,
                        409,
                        {
                            "error": (
                                "This booking has already started or changed "
                                "and can no longer be edited"
                            ),
                            "status": (
                                latest.get("status") if latest else "not_found"
                            ),
                        },
                    )
            elif previous_status == "replaced":
                stored_key_hash = previous.get("replacement_key_hash", "")
                replacement_key_matches = (
                    isinstance(stored_key_hash, str)
                    and hmac.compare_digest(
                        stored_key_hash,
                        replacement_key_hash,
                    )
                )
                if not replacement_key_matches:
                    return _respond(
                        self,
                        409,
                        {
                            "error": "This booking has already been replaced by another edit",
                            "status": previous_status,
                        },
                    )
            else:
                return _respond(
                    self,
                    409,
                    {
                        "error": "This booking is cancelled or can no longer be edited",
                        "status": previous_status,
                    },
                )

            replacement, published = _create_booking(
                new_payload,
                idempotency_key,
                prepared_booking=prepared_booking,
            )
            status = (
                201
                if published
                else (202 if replacement["status"] == "scheduling" else 200)
            )
            return _respond(
                self,
                status,
                {
                    "status": replacement["status"],
                    "booking": replacement,
                    "replaced_booking_id": old_id,
                    "qstash_published": published,
                },
            )
        except PermissionError as error:
            return _respond(self, 403, {"error": str(error)})
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            return _respond(self, 400, {"error": str(error)})
        except requests.RequestException:
            logger.exception("Could not publish replacement booking to QStash")
            return _respond(
                self,
                502,
                {
                    "error": (
                        "Old schedule was marked as replaced, but QStash "
                        "scheduling failed. Retry the same request."
                    )
                },
            )
        except RedisUnavailable:
            logger.exception("Redis booking store unavailable during replacement")
            return _respond(self, 503, {"error": "Booking store unavailable"})
        except Exception:
            logger.exception("Unexpected error replacing booking")
            return _respond(self, 503, {"error": "Booking replacement unavailable"})

    def do_GET(self):
        if not _authorized(self):
            return _respond(self, 401, {"error": "Unauthorized"})
        if os.environ.get("VERCEL_ENV") not in ("preview", "production"):
            return _respond(
                self,
                403,
                {"error": "Vercel Preview or Production environment only"},
            )

        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        if set(query) - {"limit"} or len(query.get("limit", [])) > 1:
            return _respond(
                self,
                400,
                {"error": "Only one limit parameter is supported"},
            )
        raw_limit = query.get("limit", ["50"])[0]
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return _respond(self, 400, {"error": "limit must be an integer"})
        if not 1 <= limit <= 100:
            return _respond(self, 400, {"error": "limit must be between 1 and 100"})

        try:
            records = UpstashRedis().list_bookings(limit=limit)
            fields = (
                "id",
                "person_id",
                "date",
                "time",
                "name",
                "activity_type",
                "status",
                "created_at",
                "updated_at",
                "opens_at",
                "message_id",
                "scheduled_at",
                "error_code",
                "cancelled_at",
                "replaced_at",
            )
            bookings = [
                {field: record[field] for field in fields if field in record}
                for record in records
            ]
            return _respond(
                self,
                200,
                {"count": len(bookings), "bookings": bookings},
            )
        except RedisUnavailable:
            logger.exception("Redis booking store unavailable during listing")
            return _respond(self, 503, {"error": "Booking store unavailable"})

    def do_DELETE(self):
        """Cancel a booking before its execution has started.

        The QStash message is intentionally allowed to expire: the signed
        webhook checks Redis status before claiming work and ignores cancelled
        bookings. This avoids relying on queue deletion for the safety boundary.
        """
        if not _authorized(self):
            return _respond(self, 401, {"error": "Unauthorized"})
        if os.environ.get("VERCEL_ENV") not in ("preview", "production"):
            return _respond(
                self,
                403,
                {"error": "Vercel Preview or Production environment only"},
            )

        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        if set(query) != {"id"} or len(query.get("id", [])) != 1:
            return _respond(self, 400, {"error": "Provide exactly one booking id"})
        booking_id = query["id"][0]
        if not UpstashRedis.BOOKING_ID_PATTERN.fullmatch(booking_id):
            return _respond(self, 400, {"error": "Invalid booking id"})

        try:
            redis = UpstashRedis()
            booking = redis.get_booking(booking_id)
            if booking is None:
                return _respond(self, 404, {"error": "Booking not found"})
            if booking.get("status") == "cancelled":
                return _respond(
                    self, 200, {"status": "cancelled", "booking": booking}
                )
            cancellable = [
                "pending_schedule",
                "schedule_failed",
                "scheduling",
                "scheduled",
            ]
            cancelled_at = datetime.now(ZoneInfo("Europe/Madrid")).isoformat(
                timespec="seconds"
            )
            cancelled = redis.update_booking_status(
                booking_id,
                "cancelled",
                expected_statuses=cancellable,
                metadata={"cancelled_at": cancelled_at},
            )
            if cancelled is None:
                latest = redis.get_booking(booking_id)
                if latest is None:
                    return _respond(self, 404, {"error": "Booking not found"})
                return _respond(
                    self,
                    409,
                    {
                        "error": "This booking can no longer be cancelled",
                        "status": latest.get("status"),
                    },
                )
            return _respond(
                self,
                200,
                {
                    "status": "cancelled",
                    "booking": cancelled,
                    "queue_message": "It will be ignored if QStash delivers it",
                },
            )
        except KeyError:
            return _respond(self, 404, {"error": "Booking not found"})
        except RedisUnavailable:
            logger.exception("Redis booking store unavailable during cancellation")
            return _respond(self, 503, {"error": "Booking store unavailable"})

    def log_message(self, format, *args):
        logger.info("FitBot bookings request: " + format, *args)
