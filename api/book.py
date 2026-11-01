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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scheduled import load_config, parse_local_datetime, run  # noqa: E402

logger = logging.getLogger("fitbot.vercel")


def _response(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
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

    from qstash import Receiver

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


class handler(BaseHTTPRequestHandler):
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
            config = load_config(ROOT / "reservas.json")
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

            # Fail closed: only the explicit Vercel environment variable can
            # enable live bookings; the repository config cannot override it.
            config["bookings"] = [booking]
            config["dry_run"] = os.environ.get("DRY_RUN", "true").lower() != "false"
            result = run(config, now=now)
            outcome = result.get("outcomes", [{}])[0].get("status", result["status"])
            return _response(
                self,
                200,
                {
                    "status": outcome,
                    "date": booking["date"],
                    "time": booking["time"],
                    "class": booking["name"],
                    "dry_run": config["dry_run"],
                },
            )
        except Exception:
            logger.exception("Unexpected error while processing scheduled booking")
            return _response(self, 500, {"error": "Booking processing failed"})

    def do_GET(self):
        return _response(self, 405, {"error": "Method not allowed"})

    def log_message(self, format, *args):
        logger.info("Vercel request: " + format, *args)
