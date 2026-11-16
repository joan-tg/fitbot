"""Read-only QStash inventory for authenticated FitBot administrators."""

import hmac
import json
import logging
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

import requests

logger = logging.getLogger("fitbot.qstash_inventory")
BASE_URL = "https://qstash.upstash.io/v2"
TIMEOUT = 8
MAX_LOGS = 100


def collect_inventory():
    token = os.environ.get("QSTASH_TOKEN")
    if not token:
        raise RuntimeError("QStash is not configured")
    headers = {"Authorization": f"Bearer {token}"}
    # QStash one-off delayed publishes are messages, NOT recurring schedules.
    # Logs provide recent message activity; this is not a complete pending queue listing.
    logs_response = requests.get(
        f"{BASE_URL}/logs", headers=headers, params={"count": MAX_LOGS}, timeout=TIMEOUT
    )
    logs_response.raise_for_status()
    logs_payload = logs_response.json()
    schedules_response = requests.get(
        f"{BASE_URL}/schedules", headers=headers, timeout=TIMEOUT
    )
    schedules_response.raise_for_status()
    schedules_payload = schedules_response.json()
    if not isinstance(logs_payload, dict) or not isinstance(schedules_payload, list):
        raise ValueError("Unexpected QStash response")
    logs = logs_payload.get("logs", [])
    if not isinstance(logs, list):
        raise ValueError("Unexpected QStash logs")
    # Never return forwarded headers, bodies, credentials or arbitrary destination URLs.
    safe_logs = [
        {
            "message_id": item.get("messageId"),
            "state": item.get("state"),
            "time": item.get("time"),
            "next_delivery_time": item.get("nextDeliveryTime"),
        }
        for item in logs[:MAX_LOGS]
        if isinstance(item, dict)
    ]
    safe_schedules = [
        {
            "schedule_id": item.get("scheduleId"),
            "cron": item.get("cron"),
            "is_paused": item.get("isPaused"),
            "next_schedule_time": item.get("nextScheduleTime"),
        }
        for item in schedules_payload
        if isinstance(item, dict)
    ]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "recent_logs": safe_logs,
        "recurring_schedules": safe_schedules,
        "has_more_logs": bool(logs_payload.get("cursor")),
        "limitations": [
            "Recent logs are not a complete inventory of pending one-off messages.",
            "Recurring schedules are separate from delayed one-off messages.",
            "No messages were created, changed or cancelled.",
        ],
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        expected = os.environ.get("FITBOT_ADMIN_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        if (
            not expected
            or not supplied.startswith("Bearer ")
            or not hmac.compare_digest(supplied[7:], expected)
        ):
            return self._respond(401, {"error": "Unauthorized"})
        if os.environ.get("VERCEL_ENV") != "production":
            return self._respond(403, {"error": "Production only"})
        try:
            return self._respond(200, collect_inventory())
        except (requests.RequestException, ValueError, RuntimeError):
            logger.exception("Could not read QStash inventory")
            return self._respond(503, {"error": "QStash inventory unavailable"})

    def _respond(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.info("QStash inventory request: " + format, *args)
