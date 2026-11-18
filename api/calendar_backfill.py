"""Explicit, authenticated, one-time recovery of two confirmed Joan calendar events.

Never books or cancels AimHarder classes, never publishes QStash messages and
never changes FitBot booking records. Repeated requests are idempotent because
Google Calendar event IDs derive from the immutable booking IDs.
"""

import hmac
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from google_calendar import create_joan_calendar_event  # noqa: E402
from redis_store import RedisUnavailable, UpstashRedis  # noqa: E402

logger = logging.getLogger("fitbot.calendar_backfill")

# Scope this recovery to the two confirmed Nov 21 classes that failed
# solely because google-auth was absent from the uv.lock deployment.
TARGETS = {
    "ea704a49bb73449d9974ca007f50e14b": {
        "person_id": "person_1",
        "date": "2026-11-21",
        "time": "12:00",
        "name": "Strength Conditioning",
    },
    "dd39cd88b67d4cc59b9607460bd7d45e": {
        "person_id": "person_1",
        "date": "2026-11-21",
        "time": "13:00",
        "name": "Open Box",
    },
}
CONFIRMED_STATUSES = {"booking_confirmed", "already_booked"}


def _respond(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _authorized(handler):
    token = os.environ.get("FITBOT_ADMIN_TOKEN", "")
    supplied = handler.headers.get("Authorization", "")
    return bool(
        token
        and supplied.startswith("Bearer ")
        and hmac.compare_digest(supplied[7:], token)
    )


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if not _authorized(self):
            return _respond(self, 401, {"error": "Unauthorized"})
        if (
            os.environ.get("VERCEL_ENV") != "production"
            or os.environ.get("DRY_RUN", "true").lower() != "false"
        ):
            return _respond(self, 403, {"error": "Solo disponible en Production real"})

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return _respond(self, 400, {"error": "Invalid Content-Length"})
        if length <= 0 or length > 2048:
            return _respond(self, 400, {"error": "Invalid request size"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return _respond(self, 400, {"error": "Invalid JSON"})
        if (
            not isinstance(payload, dict)
            or set(payload) != {"booking_ids"}
            or not isinstance(payload["booking_ids"], list)
            or len(payload["booking_ids"]) != len(TARGETS)
            or any(not isinstance(value, str) for value in payload["booking_ids"])
            or set(payload["booking_ids"]) != set(TARGETS)
        ):
            return _respond(
                self,
                400,
                {"error": "Selecciona exactamente las dos reservas previstas"},
            )

        try:
            redis = UpstashRedis()
            bookings = []
            for booking_id, expected in TARGETS.items():
                booking = redis.get_booking(booking_id)
                if not booking or booking.get("status") not in CONFIRMED_STATUSES:
                    return _respond(
                        self,
                        409,
                        {
                            "error": "Una de las reservas aún no está confirmada en FitBot"
                        },
                    )
                if any(booking.get(key) != value for key, value in expected.items()):
                    return _respond(
                        self,
                        409,
                        {
                            "error": "Los datos de la reserva no coinciden con los esperados"
                        },
                    )
                bookings.append(booking)
        except RedisUnavailable:
            logger.error("Google Calendar recovery could not read Redis")
            return _respond(self, 503, {"error": "No se pudo verificar las reservas"})

        results = []
        for booking in bookings:
            try:
                outcome = create_joan_calendar_event(
                    booking, dry_run=False, environment="production"
                )
                if outcome not in ("created", "already_exists"):
                    results.append(
                        {
                            "booking_id": booking["id"],
                            "status": "failed",
                            "reason": outcome,
                        }
                    )
                else:
                    results.append(
                        {
                            "booking_id": booking["id"],
                            "status": outcome,
                        }
                    )
            except Exception as error:
                response = getattr(error, "response", None)
                status = getattr(response, "status_code", None)
                logger.error(
                    "Calendar recovery failed for booking %s: %s status=%s",
                    booking["id"],
                    type(error).__name__,
                    status,
                )
                results.append({"booking_id": booking["id"], "status": "failed"})

        return _respond(
            self,
            200,
            {
                "read_only_aimharder": True,
                "results": results,
                "created": sum(item["status"] == "created" for item in results),
                "already_exists": sum(
                    item["status"] == "already_exists" for item in results
                ),
                "failed": sum(item["status"] == "failed" for item in results),
            },
        )
