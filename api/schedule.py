"""Protected Preview-only endpoint for one-off QStash schedules (DRY_RUN only)."""

import hmac
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from scheduled import load_config, parse_local_datetime  # noqa: E402

logger = logging.getLogger("fitbot.schedule")
MAX_SCHEDULE_HORIZON = timedelta(days=7)
MAX_BODY_BYTES = 16_384


def _respond(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
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


def _schedule_booking(booking):
    if os.environ.get("VERCEL_ENV") != "preview":
        raise PermissionError("Administrative scheduling is Preview-only")
    if os.environ.get("DRY_RUN", "true").lower() != "true":
        raise PermissionError("Scheduling requires DRY_RUN=true")

    config = load_config(ROOT / "reservas.json")
    fields = ("date", "opens_at", "time", "name")
    matches = [
        item
        for item in config.get("bookings", [])
        if all(booking.get(field) == item.get(field) for field in fields)
    ]
    if len(matches) != 1:
        raise ValueError("Booking must exactly match one entry in reservas.json")

    timezone_name = config.get("timezone", "Europe/Madrid")
    local_tz = ZoneInfo(timezone_name)
    opens_at = parse_local_datetime(matches[0]["opens_at"], local_tz)
    now = datetime.now(local_tz)
    if opens_at <= now:
        raise ValueError("Booking opening time must be in the future")
    if opens_at - now > MAX_SCHEDULE_HORIZON:
        raise ValueError("Booking opening time is more than 7 days away")

    qstash_token = os.environ.get("QSTASH_TOKEN")
    webhook_url = os.environ.get("FITBOT_WEBHOOK_URL")
    vercel_bypass_secret = os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET")
    if not qstash_token or not webhook_url or not vercel_bypass_secret:
        raise RuntimeError("QStash publishing or Vercel bypass is not configured")

    not_before = int(opens_at.astimezone(timezone.utc).timestamp())
    destination = quote(webhook_url, safe="")
    response = requests.post(
        f"https://qstash.upstash.io/v2/publish/{quote(destination, safe='')}",
        json=matches[0],
        headers={
            "Authorization": f"Bearer {qstash_token}",
            "Content-Type": "application/json",
            "Upstash-Not-Before": str(not_before),
            "Upstash-Forward-x-vercel-protection-bypass": vercel_bypass_secret,
        },
        timeout=10,
    )
    print("QSTASH STATUS:", response.status_code)
    print("QSTASH RESPONSE:", response.text)
    response.raise_for_status()
    result = response.json()
    message_id = result.get("messageId")
    if not message_id:
        raise RuntimeError("QStash did not return a message ID")
    return {
        "status": "scheduled",
        "message_id": message_id,
        "scheduled_at": opens_at.isoformat(),
        "timezone": timezone_name,
        "dry_run": True,
        "booking": matches[0],
    }


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if not _authorized(self):
            return _respond(self, 401, {"error": "Unauthorized"})
        if os.environ.get("VERCEL_ENV") != "preview":
            return _respond(self, 403, {"error": "Preview environment only"})
        if os.environ.get("DRY_RUN", "true").lower() != "true":
            return _respond(self, 403, {"error": "DRY_RUN=true is required"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return _respond(self, 400, {"error": "Invalid Content-Length"})
        if length <= 0 or length > MAX_BODY_BYTES:
            return _respond(self, 413, {"error": "Invalid request body size"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Body must be a JSON object")
            result = _schedule_booking(payload)
            return _respond(self, 201, result)
        except PermissionError as error:
            return _respond(self, 403, {"error": str(error)})
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            return _respond(self, 400, {"error": str(error)})
        except requests.RequestException:
            logger.exception("Could not create QStash schedule")
            return _respond(self, 502, {"error": "QStash scheduling failed"})
        except Exception:
            logger.exception("Unexpected error creating QStash schedule")
            return _respond(self, 503, {"error": "Scheduling service unavailable"})

    def do_GET(self):
        return _respond(self, 405, {"error": "Method not allowed"})

    def log_message(self, format, *args):
        logger.info("Vercel schedule request: " + format, *args)
