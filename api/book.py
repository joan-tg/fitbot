"""Vercel HTTP endpoint invoked by a scheduled QStash message.

Required environment variables:
- EMAIL, PASSWORD
- QSTASH_CURRENT_SIGNING_KEY, QSTASH_NEXT_SIGNING_KEY
- FITBOT_WEBHOOK_URL (the exact public URL of this endpoint)

DRY_RUN defaults to true. Set DRY_RUN=false only after a successful end-to-end
simulation and explicit activation of live bookings.
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from qstash import Receiver

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scheduled import load_config, parse_local_datetime, run  # noqa: E402
from exceptions import (  # noqa: E402
    AimHarderUnavailable,
    IncorrectCredentials,
    TooManyWrongAttempts,
)
from redis_store import RedisUnavailable, UpstashRedis  # noqa: E402

logger = logging.getLogger("fitbot.vercel")
INTEGRATION_TEST_NAME = "FITBOT_QSTASH_REDIS_INTEGRATION_TEST"
LIVE_LOCK_TTL = 45 * 24 * 60 * 60
AUTH_FAILURE_LOCK_TTL = 5 * 60
AUTH_RETRY_DELAY_SECONDS = 60
PROCESSING_LOCK_TTL = 5 * 60


def _response(handler, status, payload, headers=None):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


def _verify_qstash(raw_body, signature):
    current_key = os.environ.get("QSTASH_CURRENT_SIGNING_KEY")
    next_key = os.environ.get("QSTASH_NEXT_SIGNING_KEY")
    destination_url = os.environ.get("FITBOT_WEBHOOK_URL")
    if not current_key or not next_key or not destination_url:
        raise RuntimeError("QStash signature verification is not configured")
    if not signature:
        return False

    receiver = Receiver(
        current_signing_key=current_key,
        next_signing_key=next_key,
    )
    try:
        receiver.verify(
            body=raw_body.decode("utf-8"),
            signature=signature,
            url=destination_url,
        )
        return True
    except Exception:
        logger.warning("Rejected request with an invalid QStash signature")
        return False


def _find_configured_booking(config, payload):
    fields = ("date", "opens_at", "time", "name")
    if not isinstance(payload, dict):
        return None
    for booking in config.get("bookings", []):
        if all(payload.get(field) == booking.get(field) for field in fields):
            return booking
    return None


def _is_booking_due(booking, timezone, now, window_minutes):
    opens_at = parse_local_datetime(booking["opens_at"], timezone)
    elapsed = now - opens_at
    return timedelta(0) <= elapsed <= timedelta(minutes=window_minutes)


def _recover_stale_processing(booking, redis, dry_run, now=None):
    """Mark an abandoned processing attempt as uncertain without retrying it."""
    raw_updated_at = booking.get("updated_at")
    if not isinstance(raw_updated_at, str):
        return False
    try:
        updated_at = datetime.fromisoformat(raw_updated_at)
    except ValueError:
        return False
    if updated_at.tzinfo is None:
        return False

    now = now or datetime.now(updated_at.tzinfo)
    if now.tzinfo is None:
        raise ValueError("Recovery time must include a timezone")
    age = now - updated_at
    if age <= timedelta(seconds=PROCESSING_LOCK_TTL):
        return False

    recovered = redis.update_booking_status(
        booking["id"],
        "outcome_unconfirmed",
        expected_statuses=["processing"],
        metadata={"error_code": "stale_processing_recovered"},
    )
    if recovered is None:
        return False

    # The original five-minute lease should have expired. If it has, replace
    # it with a terminal safety lock. A live booking must never be retried
    # automatically after an interrupted attempt with an unknown remote result.
    lock_token = redis.acquire(
        booking,
        dry_run=dry_run,
        ttl_seconds=PROCESSING_LOCK_TTL,
    )
    if lock_token:
        redis.complete(
            booking,
            lock_token,
            "outcome_unconfirmed",
            dry_run=dry_run,
            ttl_seconds=LIVE_LOCK_TTL if not dry_run else 60,
        )
    else:
        logger.warning(
            "Recovered stale processing booking %s; an existing lock remains",
            booking["id"],
        )
    logger.error(
        "Recovered stale processing booking %s as outcome_unconfirmed",
        booking["id"],
    )
    return True


class handler(BaseHTTPRequestHandler):
    def _handle_dynamic_booking(self, payload, config):
        booking_id = payload.get("booking_id")
        if not isinstance(booking_id, str):
            return _response(self, 400, {"error": "booking_id is required"})

        redis = UpstashRedis()
        booking = redis.get_booking(booking_id)
        if booking is None:
            return _response(self, 404, {"error": "Booking not found"})

        status = booking.get("status")
        if status == "cancelled":
            return _response(self, 200, {"status": "cancelled_ignored"})

        # Preview remains forcibly dry-run. Determine the lock namespace before
        # inspecting stale processing records so recovery uses the same namespace.
        is_preview = os.environ.get("VERCEL_ENV") == "preview"
        dry_run = is_preview or os.environ.get("DRY_RUN", "true").lower() != "false"
        if status == "scheduling":
            # A valid QStash delivery proves publication succeeded. Recover the
            # intermediate state left behind if QStash accepted the message but
            # the API could not persist its message ID/status in Redis.
            recovered = redis.update_booking_status(
                booking_id,
                "scheduled",
                expected_statuses=["scheduling"],
                metadata={
                    "scheduled_at": booking.get("opens_at"),
                    "error_code": None,
                },
            )
            if recovered is None:
                booking = redis.get_booking(booking_id)
                if booking is None:
                    return _response(self, 404, {"error": "Booking not found"})
                status = booking.get("status")
            else:
                booking = recovered
                status = "scheduled"
            if status == "cancelled":
                return _response(self, 200, {"status": "cancelled_ignored"})
            if status != "scheduled":
                return _response(
                    self,
                    200,
                    {"status": "duplicate_ignored", "booking_id": booking_id},
                )
            logger.warning(
                "Recovered booking %s from scheduling after QStash delivery",
                booking_id,
            )

        if status == "processing":
            if _recover_stale_processing(booking, redis, dry_run=dry_run):
                return _response(
                    self,
                    200,
                    {
                        "status": "outcome_unconfirmed",
                        "booking_id": booking_id,
                        "error_code": "stale_processing_recovered",
                    },
                )
            return _response(
                self,
                200,
                {"status": "duplicate_ignored", "booking_id": booking_id},
            )
        if status != "scheduled":
            return _response(
                self,
                200,
                {"status": "duplicate_ignored", "booking_id": booking_id},
            )

        timezone = ZoneInfo(config.get("timezone", "Europe/Madrid"))
        now = datetime.now(timezone)
        window_minutes = int(config.get("window_minutes", 30))
        if not _is_booking_due(booking, timezone, now, window_minutes):
            return _response(
                self,
                409,
                {
                    "status": "outside_window",
                    "booking_id": booking_id,
                    "opens_at": booking["opens_at"],
                    "window_minutes": window_minutes,
                },
            )

        # Dynamic bookings in Preview are always simulations. This is a code-level
        # safety boundary: a mistaken DRY_RUN=false setting must never create a
        # real AimHarder reservation from a Preview deployment.
        # The lock includes person_id, so two independent accounts may book the
        # same class/time without suppressing each other's reservations.
        lock_token = redis.acquire(
            booking, dry_run=dry_run, ttl_seconds=PROCESSING_LOCK_TTL
        )
        if not lock_token:
            return _response(
                self,
                200,
                {"status": "duplicate_ignored", "booking_id": booking_id},
            )

        claimed = redis.update_booking_status(
            booking_id, "processing", expected_statuses=["scheduled"]
        )
        if claimed is None:
            redis.release(booking, lock_token, dry_run=dry_run)
            current = redis.get_booking(booking_id)
            if current and current.get("status") == "cancelled":
                return _response(self, 200, {"status": "cancelled_ignored"})
            return _response(
                self,
                200,
                {"status": "duplicate_ignored", "booking_id": booking_id},
            )

        def save_history(outcome):
            try:
                redis.record_booking_result(
                    {
                        "booking_id": booking_id,
                        "person_id": booking["person_id"],
                        "date": booking["date"],
                        "time": booking["time"],
                        "class": booking["name"],
                        "status": outcome,
                        "dry_run": dry_run,
                    }
                )
            except RedisUnavailable:
                logger.exception("Could not save dynamic booking history")

        def set_status(outcome, error_code=None):
            metadata = {"error_code": error_code} if error_code else None
            redis.update_booking_status(
                booking_id,
                outcome,
                expected_statuses=["processing"],
                metadata=metadata,
            )

        run_config = dict(config)
        run_config["bookings"] = [booking]
        run_config["dry_run"] = dry_run
        try:
            result = run(run_config, now=now, force_dry_run=dry_run)
        except IncorrectCredentials:
            outcome = "incorrect_credentials"
            set_status("booking_failed", outcome)
            redis.complete(
                booking,
                lock_token,
                outcome,
                dry_run=dry_run,
                ttl_seconds=AUTH_FAILURE_LOCK_TTL,
            )
            save_history(outcome)
            logger.error("AimHarder rejected credentials for a configured member")
            return _response(
                self,
                489,
                {
                    "status": outcome,
                    "booking_id": booking_id,
                    "person_id": booking["person_id"],
                    "dry_run": dry_run,
                },
                headers={"Upstash-NonRetryable-Error": "true"},
            )
        except TooManyWrongAttempts:
            try:
                retry_count = int(self.headers.get("Upstash-Retried", "0"))
            except (TypeError, ValueError):
                retry_count = 1
            opens_at = parse_local_datetime(booking["opens_at"], timezone)
            remaining = timedelta(minutes=window_minutes) - (
                datetime.now(timezone) - opens_at
            )
            if retry_count == 0 and remaining > timedelta(
                seconds=AUTH_RETRY_DELAY_SECONDS
            ):
                redis.release(booking, lock_token, dry_run=dry_run)
                redis.update_booking_status(
                    booking_id,
                    "scheduled",
                    expected_statuses=["processing"],
                )
                save_history("too_many_wrong_attempts_retry_scheduled")
                return _response(
                    self,
                    503,
                    {
                        "status": "too_many_wrong_attempts_retry_scheduled",
                        "booking_id": booking_id,
                    },
                    headers={"Retry-After": str(AUTH_RETRY_DELAY_SECONDS)},
                )
            outcome = "too_many_wrong_attempts"
            set_status("booking_failed", outcome)
            redis.complete(
                booking,
                lock_token,
                outcome,
                dry_run=dry_run,
                ttl_seconds=AUTH_FAILURE_LOCK_TTL,
            )
            save_history(outcome)
            return _response(
                self,
                489,
                {"status": outcome, "booking_id": booking_id},
                headers={"Upstash-NonRetryable-Error": "true"},
            )
        except AimHarderUnavailable:
            redis.release(booking, lock_token, dry_run=dry_run)
            redis.update_booking_status(
                booking_id, "scheduled", expected_statuses=["processing"]
            )
            logger.warning("AimHarder unavailable before booking; retry is safe")
            return _response(
                self, 503, {"error": "AimHarder unavailable before booking"}
            )
        except Exception:
            if dry_run:
                redis.release(booking, lock_token, dry_run=True)
                redis.update_booking_status(
                    booking_id, "scheduled", expected_statuses=["processing"]
                )
            else:
                set_status("outcome_unconfirmed", "unexpected_execution_error")
                redis.complete(
                    booking,
                    lock_token,
                    "outcome_unconfirmed",
                    dry_run=False,
                    ttl_seconds=LIVE_LOCK_TTL,
                )
            raise

        outcome = result.get("outcomes", [{}])[0].get("status", result["status"])
        if outcome == "booking_unconfirmed":
            outcome = "outcome_unconfirmed"
        elif outcome == "class_not_found":
            outcome = "booking_failed"

        set_status(outcome, outcome if outcome == "booking_failed" else None)
        if dry_run:
            redis.complete(booking, lock_token, outcome, dry_run=True, ttl_seconds=60)
        elif outcome in ("booking_confirmed", "already_booked"):
            redis.complete(
                booking,
                lock_token,
                outcome,
                dry_run=False,
                ttl_seconds=LIVE_LOCK_TTL,
            )
        elif outcome == "booking_failed":
            redis.release(booking, lock_token, dry_run=False)
        else:
            set_status("outcome_unconfirmed", "unrecognized_execution_result")
            redis.complete(
                booking,
                lock_token,
                "outcome_unconfirmed",
                dry_run=False,
                ttl_seconds=LIVE_LOCK_TTL,
            )
            outcome = "outcome_unconfirmed"

        save_history(outcome)
        return _response(
            self,
            200,
            {
                "status": outcome,
                "booking_id": booking_id,
                "person_id": booking["person_id"],
                "date": booking["date"],
                "time": booking["time"],
                "class": booking["name"],
                "dry_run": dry_run,
            },
        )

    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return _response(self, 400, {"error": "Invalid Content-Length"})

        if content_length <= 0 or content_length > 16_384:
            return _response(self, 413, {"error": "Invalid request body size"})

        raw_body = self.rfile.read(content_length)
        signature = self.headers.get("Upstash-Signature")
        try:
            valid = _verify_qstash(raw_body, signature)
        except RuntimeError as error:
            logger.error("%s", error)
            return _response(self, 503, {"error": "Webhook is not configured"})
        if not valid:
            return _response(self, 401, {"error": "Invalid signature"})

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _response(self, 400, {"error": "Body must be valid JSON"})

        try:
            # Isolated integration probe: signed QStash request -> Preview Vercel
            # -> real Upstash lock. It never loads reservations or calls AimHarder.
            if (
                isinstance(payload, dict)
                and payload.get("name") == INTEGRATION_TEST_NAME
            ):
                if os.environ.get("VERCEL_ENV") != "preview":
                    return _response(
                        self, 403, {"error": "Integration probe is Preview-only"}
                    )
                if os.environ.get("DRY_RUN", "true").lower() != "true":
                    return _response(
                        self, 403, {"error": "Integration probe requires DRY_RUN=true"}
                    )
                probe_booking = {
                    "date": str(payload.get("date", "")),
                    "time": str(payload.get("time", "")),
                    "name": INTEGRATION_TEST_NAME,
                }
                if not probe_booking["date"] or not probe_booking["time"]:
                    return _response(
                        self,
                        400,
                        {"error": "Integration probe requires date and time"},
                    )
                probe_id = payload.get("probe_id")
                step = payload.get("probe_step")
                if not probe_id or step not in ("first", "second"):
                    return _response(
                        self,
                        400,
                        {"error": "Integration probe requires probe_id and probe_step"},
                    )
                redis = UpstashRedis()
                acquired = redis.acquire(probe_booking, dry_run=True, ttl_seconds=300)
                status = (
                    "integration_test_lock_acquired"
                    if acquired
                    else "integration_test_duplicate"
                )
                redis.record_integration_result(probe_id, step, status)
                return _response(
                    self,
                    200,
                    {
                        "status": status,
                        "dry_run": True,
                        "environment": "preview",
                        "redis": "connected",
                        "evidence_key": redis.integration_result_key(probe_id, step),
                    },
                )

            config = load_config(ROOT / "reservas.json")
            if isinstance(payload, dict) and "booking_id" in payload:
                return handler._handle_dynamic_booking(self, payload, config)

            # Backward-compatible path for legacy static reservations.
            booking = _find_configured_booking(config, payload)
            if booking is None:
                return _response(
                    self, 404, {"error": "Booking is not in reservas.json"}
                )

            timezone = ZoneInfo(config.get("timezone", "Europe/Madrid"))
            now = datetime.now(timezone)
            window_minutes = int(config.get("window_minutes", 30))
            if not _is_booking_due(booking, timezone, now, window_minutes):
                return _response(
                    self,
                    409,
                    {
                        "status": "outside_window",
                        "date": booking["date"],
                        "time": booking["time"],
                        "opens_at": booking["opens_at"],
                        "window_minutes": window_minutes,
                    },
                )

            # Preview is always simulation-only, including the legacy static path.
            # Do not let a misconfigured DRY_RUN=false reach AimHarder from Preview.
            is_preview = os.environ.get("VERCEL_ENV") == "preview"
            dry_run = is_preview or os.environ.get("DRY_RUN", "true").lower() != "false"
            config["bookings"] = [booking]
            config["dry_run"] = dry_run
            redis = UpstashRedis()
            # Every invocation starts with a short processing lease. A confirmed
            # or uncertain live outcome is promoted to the long safety lock below.
            lock_token = redis.acquire(
                booking, dry_run=dry_run, ttl_seconds=PROCESSING_LOCK_TTL
            )
            if not lock_token:
                return _response(
                    self,
                    200,
                    {
                        "status": "duplicate_ignored",
                        "date": booking["date"],
                        "time": booking["time"],
                        "dry_run": dry_run,
                    },
                )

            try:
                result = run(config, now=now, force_dry_run=dry_run)
            except IncorrectCredentials:
                # Bad credentials will not recover by retrying this message.
                # Mark it non-retryable so QStash sends it to the DLQ for review.
                outcome = "incorrect_credentials"
                redis.complete(
                    booking,
                    lock_token,
                    outcome,
                    dry_run=dry_run,
                    ttl_seconds=AUTH_FAILURE_LOCK_TTL,
                )
                try:
                    redis.record_booking_result(
                        {
                            "date": booking["date"],
                            "time": booking["time"],
                            "class": booking["name"],
                            "status": outcome,
                            "dry_run": dry_run,
                        }
                    )
                except RedisUnavailable:
                    logger.exception("Could not save authentication failure history")
                logger.error("AimHarder rejected the configured credentials")
                return _response(
                    self,
                    489,
                    {
                        "status": outcome,
                        "date": booking["date"],
                        "time": booking["time"],
                        "class": booking["name"],
                        "dry_run": dry_run,
                    },
                    headers={"Upstash-NonRetryable-Error": "true"},
                )
            except TooManyWrongAttempts:
                # AimHarder may be temporarily rate-limiting login attempts. Allow
                # exactly one delayed retry, and only while the booking window
                # still has enough time. A second lock response is terminal.
                try:
                    retry_count = int(self.headers.get("Upstash-Retried", "0"))
                except (TypeError, ValueError):
                    retry_count = 1
                opens_at = parse_local_datetime(booking["opens_at"], timezone)
                retry_now = datetime.now(timezone)
                remaining = timedelta(minutes=window_minutes) - (retry_now - opens_at)
                retry_available = retry_count == 0
                window_has_time = remaining > timedelta(
                    seconds=AUTH_RETRY_DELAY_SECONDS
                )
                if retry_available and window_has_time:
                    redis.release(booking, lock_token, dry_run=dry_run)
                    outcome = "too_many_wrong_attempts_retry_scheduled"
                    try:
                        redis.record_booking_result(
                            {
                                "date": booking["date"],
                                "time": booking["time"],
                                "class": booking["name"],
                                "status": outcome,
                                "dry_run": dry_run,
                            }
                        )
                    except RedisUnavailable:
                        logger.exception("Could not save authentication retry history")
                    logger.warning(
                        "AimHarder temporarily blocked login; one retry scheduled"
                    )
                    return _response(
                        self,
                        503,
                        {
                            "status": outcome,
                            "date": booking["date"],
                            "time": booking["time"],
                            "class": booking["name"],
                            "dry_run": dry_run,
                        },
                        headers={"Retry-After": str(AUTH_RETRY_DELAY_SECONDS)},
                    )

                outcome = "too_many_wrong_attempts"
                redis.complete(
                    booking,
                    lock_token,
                    outcome,
                    dry_run=dry_run,
                    ttl_seconds=AUTH_FAILURE_LOCK_TTL,
                )
                try:
                    redis.record_booking_result(
                        {
                            "date": booking["date"],
                            "time": booking["time"],
                            "class": booking["name"],
                            "status": outcome,
                            "dry_run": dry_run,
                        }
                    )
                except RedisUnavailable:
                    logger.exception("Could not save authentication failure history")
                logger.error(
                    "AimHarder login remains blocked after the single retry window"
                )
                return _response(
                    self,
                    489,
                    {
                        "status": outcome,
                        "date": booking["date"],
                        "time": booking["time"],
                        "class": booking["name"],
                        "dry_run": dry_run,
                    },
                    headers={"Upstash-NonRetryable-Error": "true"},
                )
            except AimHarderUnavailable:
                # Login/classes failed before a booking POST was sent. It is safe
                # to release the lock and let QStash retry this delivery.
                redis.release(booking, lock_token, dry_run=dry_run)
                logger.warning("AimHarder unavailable before booking; retry is safe")
                return _response(
                    self, 503, {"error": "AimHarder unavailable before booking"}
                )
            except Exception:
                # An unexpected live-mode error may have happened after a booking
                # POST. Promote the short lease to a long safety lock rather than
                # risk a duplicate. Dry-run can safely release its lock.
                if dry_run:
                    redis.release(booking, lock_token, dry_run=True)
                else:
                    redis.complete(
                        booking,
                        lock_token,
                        "outcome_unconfirmed",
                        dry_run=False,
                        ttl_seconds=LIVE_LOCK_TTL,
                    )
                raise

            outcome = result.get("outcomes", [{}])[0].get("status", result["status"])
            if dry_run:
                redis.complete(
                    booking, lock_token, outcome, dry_run=True, ttl_seconds=60
                )
            elif outcome in ("booking_confirmed", "already_booked"):
                redis.complete(
                    booking,
                    lock_token,
                    outcome,
                    dry_run=False,
                    ttl_seconds=LIVE_LOCK_TTL,
                )
            elif outcome in ("class_not_found", "booking_failed"):
                # No successful booking was confirmed; allow a controlled retry.
                redis.release(booking, lock_token, dry_run=False)
            else:
                # Includes booking_unconfirmed: preserve the lock to prevent a
                # duplicate while the remote outcome is uncertain.
                redis.complete(
                    booking,
                    lock_token,
                    "outcome_unconfirmed",
                    dry_run=False,
                    ttl_seconds=LIVE_LOCK_TTL,
                )

            # Result history is best-effort: a history write must never change
            # the booking outcome or trigger a duplicate reservation.
            try:
                redis.record_booking_result(
                    {
                        "date": booking["date"],
                        "time": booking["time"],
                        "class": booking["name"],
                        "status": outcome,
                        "dry_run": dry_run,
                    }
                )
            except RedisUnavailable:
                logger.exception("Could not save booking result history")

            return _response(
                self,
                200,
                {
                    "status": outcome,
                    "date": booking["date"],
                    "time": booking["time"],
                    "class": booking["name"],
                    "dry_run": dry_run,
                },
            )
        except RedisUnavailable:
            logger.exception("Redis idempotency lock unavailable; failing closed")
            return _response(self, 503, {"error": "Idempotency store unavailable"})
        except Exception:
            logger.exception("Unexpected error while processing scheduled booking")
            return _response(self, 500, {"error": "Booking processing failed"})

    def do_GET(self):
        return _response(self, 405, {"error": "Method not allowed"})

    def log_message(self, format, *args):
        logger.info("Vercel request: " + format, *args)
