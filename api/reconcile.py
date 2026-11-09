"""Periodic reconciliation for dynamic booking schedules."""

import hmac
import json
import logging
import os
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from qstash import Receiver

from api.bookings import (
    SCHEDULING_RECOVERY_SECONDS,
    _publish_booking,
)
from redis_store import RedisUnavailable, UpstashRedis
from scheduled import load_config

ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger("fitbot.reconcile")
MAX_BOOKINGS_PER_RUN = 10
PROCESSING_RECOVERY_SECONDS = 5 * 60


def _respond(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _authorized(handler):
    supplied = handler.headers.get("Authorization", "")
    expected = os.environ.get("FITBOT_ADMIN_TOKEN", "")
    return bool(
        expected
        and supplied.startswith("Bearer ")
        and hmac.compare_digest(supplied[7:], expected)
    )


def _verify_qstash(handler, raw_body):
    current_key = os.environ.get("QSTASH_CURRENT_SIGNING_KEY")
    next_key = os.environ.get("QSTASH_NEXT_SIGNING_KEY")
    host = handler.headers.get("x-forwarded-host") or handler.headers.get("host")
    signature = handler.headers.get("Upstash-Signature")
    if not current_key or not next_key or not host or not signature:
        return False

    proto = handler.headers.get("x-forwarded-proto", "https").split(",")[0].strip()
    if proto != "https":
        return False
    destination = f"https://{host}{urlsplit(handler.path).path}"
    receiver = Receiver(
        current_signing_key=current_key,
        next_signing_key=next_key,
    )
    try:
        receiver.verify(
            body=raw_body.decode("utf-8"),
            signature=signature,
            url=destination,
        )
        return True
    except Exception:
        logger.warning("Rejected reconciliation request with invalid QStash signature")
        return False


def _aware_timestamp(record, field="updated_at"):
    raw = record.get(field) or record.get("created_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _is_stale(record, now, seconds):
    updated_at = _aware_timestamp(record)
    return updated_at is not None and now - updated_at > timedelta(seconds=seconds)


def _is_expired(record, now, timezone, window_minutes):
    raw = record.get("opens_at")
    if not isinstance(raw, str):
        return False
    try:
        opens_at = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if opens_at.tzinfo is None:
        opens_at = opens_at.replace(tzinfo=timezone)
    return now > opens_at.astimezone(timezone) + timedelta(minutes=window_minutes)


def _record_terminal_result(redis, booking, status, dry_run, error_code):
    try:
        redis.record_booking_result(
            {
                "booking_id": booking["id"],
                "person_id": booking["person_id"],
                "date": booking["date"],
                "time": booking["time"],
                "class": booking["name"],
                "status": status,
                "dry_run": dry_run,
                "error_code": error_code,
            }
        )
    except RedisUnavailable:
        logger.exception(
            "Could not save reconciliation history for booking %s",
            booking["id"],
        )


def _reconcile(redis, records, now, timezone, window_minutes, dry_run):
    """Recover abandoned publishing and close safely expired/uncertain records."""
    counts = {
        "scanned": len(records),
        "republished": 0,
        "missed": 0,
        "outcome_unconfirmed": 0,
        "skipped": 0,
        "errors": 0,
    }
    candidates = sorted(
        records,
        key=lambda item: (
            _aware_timestamp(item) or datetime.min.replace(tzinfo=now.tzinfo)
        ),
    )

    processed = 0
    for booking in candidates:
        status = booking.get("status")
        if status not in (
            "pending_schedule",
            "scheduling",
            "schedule_failed",
            "scheduled",
            "processing",
        ):
            continue

        if status == "processing":
            if not _is_stale(booking, now, PROCESSING_RECOVERY_SECONDS):
                continue
            try:
                updated = redis.update_booking_status(
                    booking["id"],
                    "outcome_unconfirmed",
                    expected_statuses=["processing"],
                    metadata={"error_code": "stale_processing_recovered"},
                )
            except (RedisUnavailable, KeyError):
                logger.exception(
                    "Could not recover processing booking %s", booking["id"]
                )
                counts["errors"] += 1
                continue
            if updated is None:
                counts["skipped"] += 1
                continue
            counts["outcome_unconfirmed"] += 1
            _record_terminal_result(
                redis,
                booking,
                "outcome_unconfirmed",
                dry_run,
                "stale_processing_recovered",
            )
            continue

        if status == "scheduled":
            if not _is_expired(booking, now, timezone, window_minutes):
                continue
            try:
                missed_at = now.astimezone(timezone).isoformat(timespec="seconds")
                updated = redis.update_booking_status(
                    booking["id"],
                    "booking_missed",
                    expected_statuses=["scheduled"],
                    metadata={
                        "missed_at": missed_at,
                        "error_code": "execution_window_expired",
                    },
                )
            except (RedisUnavailable, KeyError):
                logger.exception("Could not expire scheduled booking %s", booking["id"])
                counts["errors"] += 1
                continue
            if updated is None:
                counts["skipped"] += 1
                continue
            counts["missed"] += 1
            _record_terminal_result(
                redis,
                booking,
                "booking_missed",
                dry_run,
                "execution_window_expired",
            )
            continue

        if not _is_stale(booking, now, SCHEDULING_RECOVERY_SECONDS):
            continue

        if _is_expired(booking, now, timezone, window_minutes):
            try:
                missed_at = now.astimezone(timezone).isoformat(timespec="seconds")
                updated = redis.update_booking_status(
                    booking["id"],
                    "booking_missed",
                    expected_statuses=[status],
                    metadata={
                        "missed_at": missed_at,
                        "error_code": "execution_window_expired",
                    },
                )
            except (RedisUnavailable, KeyError):
                logger.exception(
                    "Could not expire unscheduled booking %s", booking["id"]
                )
                counts["errors"] += 1
                continue
            if updated is None:
                counts["skipped"] += 1
                continue
            counts["missed"] += 1
            _record_terminal_result(
                redis,
                booking,
                "booking_missed",
                dry_run,
                "execution_window_expired",
            )
            continue

        if processed >= MAX_BOOKINGS_PER_RUN:
            counts["skipped"] += 1
            continue

        if status == "scheduling":
            try:
                recovered = redis.update_booking_status(
                    booking["id"],
                    "schedule_failed",
                    expected_statuses=["scheduling"],
                    metadata={"error_code": "stale_scheduling_recovered"},
                )
            except (RedisUnavailable, KeyError):
                logger.exception("Could not recover scheduling booking %s", booking["id"])
                counts["errors"] += 1
                continue
            if recovered is None:
                counts["skipped"] += 1
                continue
            booking = recovered

        processed += 1
        try:
            published, did_publish = _publish_booking(booking, redis)
            if did_publish:
                counts["republished"] += 1
            else:
                counts["skipped"] += 1
        except Exception:
            # A failed publish remains recoverable as schedule_failed. The next
            # cron pass retries it; it never calls AimHarder from this endpoint.
            logger.exception("Could not republish booking %s", booking["id"])
            counts["errors"] += 1

    return counts


class handler(BaseHTTPRequestHandler):
    def _run(self):
        if os.environ.get("VERCEL_ENV") not in ("preview", "production"):
            return _respond(
                self,
                403,
                {"error": "Vercel Preview or Production environment only"},
            )

        try:
            config = load_config(ROOT / "reservas.json")
            timezone = ZoneInfo(config.get("timezone", "Europe/Madrid"))
            now = datetime.now(timezone)
            window_minutes = int(config.get("window_minutes", 30))
            is_preview = os.environ.get("VERCEL_ENV") == "preview"
            dry_run = is_preview or os.environ.get("DRY_RUN", "true").lower() != "false"
            redis = UpstashRedis()
            records = redis.list_bookings(limit=UpstashRedis.BOOKINGS_MAX_ITEMS)
            counts = _reconcile(
                redis,
                records,
                now,
                timezone,
                window_minutes,
                dry_run,
            )
            return _respond(self, 200, {"status": "completed", **counts})
        except RedisUnavailable:
            logger.exception("Redis unavailable during reconciliation")
            return _respond(self, 503, {"error": "Booking store unavailable"})
        except Exception:
            logger.exception("Unexpected reconciliation failure")
            return _respond(self, 503, {"error": "Reconciliation unavailable"})

    def do_GET(self):
        # Manual invocation for a maintainer; scheduled runs use signed QStash POSTs.
        if not _authorized(self):
            return _respond(self, 401, {"error": "Unauthorized"})
        return self._run()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return _respond(self, 400, {"error": "Invalid Content-Length"})
        if length <= 0 or length > 16_384:
            return _respond(self, 413, {"error": "Invalid request body size"})
        raw_body = self.rfile.read(length)
        if not _verify_qstash(self, raw_body):
            return _respond(self, 401, {"error": "Invalid QStash signature"})
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _respond(self, 400, {"error": "Invalid JSON body"})
        if payload != {"task": "reconcile"}:
            return _respond(self, 400, {"error": "Invalid reconciliation task"})
        return self._run()
