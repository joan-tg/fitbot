"""Read-only QStash inventory for authenticated FitBot administrators."""

import hmac
import json
import logging
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

import requests

logger = logging.getLogger("fitbot.qstash_inventory")
BASE_URL = "https://qstash.upstash.io/v2"
TIMEOUT = 8
MAX_LOGS = 100
MAX_LOG_PAGES = 5


def _destination_info(destination):
    """Expose only a recognized FitBot environment/endpoint, never a raw URL."""
    if not isinstance(destination, str):
        return {"environment": "unknown", "endpoint": "unknown"}
    parsed = urlsplit(destination)
    host = (parsed.hostname or "").lower()
    if host == "fitbot-theta.vercel.app":
        environment = "production"
    elif host.startswith("fitbot-git-") and host.endswith(".vercel.app"):
        environment = "preview"
    else:
        environment = "unknown"
    endpoint = (
        parsed.path if parsed.path in ("/api/reconcile", "/api/book") else "other"
    )
    return {"environment": environment, "endpoint": endpoint}


def collect_inventory():
    token = os.environ.get("QSTASH_TOKEN")
    if not token:
        raise RuntimeError("QStash is not configured")
    headers = {"Authorization": f"Bearer {token}"}
    # QStash one-off delayed publishes are messages, NOT recurring schedules.
    # Logs provide recent message activity; this is not a complete pending queue listing.
    logs = []
    cursor = None
    seen_cursors = set()
    pages_scanned = 0
    logs_status = "complete"
    # Logs must never prevent independent recurring-schedule inspection.
    while pages_scanned < MAX_LOG_PAGES:
        params = {"count": MAX_LOGS}
        if cursor is not None:
            params["cursor"] = cursor
        try:
            logs_response = requests.get(
                f"{BASE_URL}/logs", headers=headers, params=params, timeout=TIMEOUT
            )
            logs_response.raise_for_status()
            logs_payload = logs_response.json()
        except (requests.RequestException, ValueError):
            logger.warning("QStash logs unavailable; showing schedules only")
            logs_status = "unavailable" if pages_scanned == 0 else "partial"
            break
        pages_scanned += 1
        if not isinstance(logs_payload, dict) or not isinstance(
            logs_payload.get("logs"), list
        ):
            logger.warning("QStash logs returned unexpected response shape")
            logs_status = "unavailable" if not logs else "partial"
            break
        page = logs_payload["logs"]
        logs.extend(page)
        next_cursor = logs_payload.get("cursor")
        if not next_cursor:
            cursor = None
            break
        if not isinstance(next_cursor, (str, int)):
            logs_status = "partial"
            break
        next_cursor = str(next_cursor)
        if not page or next_cursor in seen_cursors:
            # Guard against empty/stuck pages without pretending logs are complete.
            logs_status = "partial"
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        if cursor:
            logs_status = "partial"
    schedules_response = requests.get(
        f"{BASE_URL}/schedules", headers=headers, timeout=TIMEOUT
    )
    schedules_response.raise_for_status()
    schedules_payload = schedules_response.json()
    if not isinstance(schedules_payload, list):
        raise ValueError("Unexpected QStash schedules")
    # Never return forwarded headers, bodies, credentials or arbitrary destination URLs.
    safe_logs = [
        {
            "message_id": item.get("messageId"),
            "state": item.get("state"),
            "time": item.get("time"),
            "next_delivery_time": item.get("nextDeliveryTime"),
            "schedule_id": item.get("scheduleId"),
            "destination": _destination_info(item.get("url")),
        }
        for item in logs
        if isinstance(item, dict)
    ]
    safe_schedules = [
        {
            "schedule_id": item.get("scheduleId"),
            "cron": item.get("cron"),
            "is_paused": item.get("isPaused"),
            "next_schedule_time": item.get("nextScheduleTime"),
            "destination": _destination_info(item.get("destination")),
        }
        for item in schedules_payload
        if isinstance(item, dict)
    ]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "recent_logs": safe_logs,
        "recurring_schedules": safe_schedules,
        "has_more_logs": bool(cursor) or logs_status != "complete",
        "logs_status": logs_status,
        "log_pages_scanned": pages_scanned,
        "log_entries_scanned": len(logs),
        "log_scan_limit_reached": pages_scanned >= MAX_LOG_PAGES and bool(cursor),
        "limitations": [
            "Logs are bounded and are not a complete inventory of pending one-off messages.",
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
