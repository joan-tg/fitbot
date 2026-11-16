"""Admin-only, read-only lookup of FitBot's known QStash one-off message IDs."""

import hmac
import json
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from redis_store import RedisUnavailable, UpstashRedis  # noqa: E402

logger = logging.getLogger("fitbot.qstash_messages")
QSTASH_API = "https://qstash.upstash.io/v2/messages"
MAX_MESSAGES = 25
MESSAGE_ID_RE = re.compile(r"^msg_[A-Za-z0-9]{1,200}$")


def _lookup_one(item, token):
    message_id = item["message_id"]
    result = {
        "booking_id": item.get("id"),
        "booking_status": item.get("status"),
        "date": item.get("date"),
        "time": item.get("time"),
        "message_id": message_id,
    }
    try:
        response = requests.get(
            f"{QSTASH_API}/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=4,
        )
    except requests.RequestException:
        result["lookup"] = "unavailable"
        return result
    if response.status_code == 404:
        result["lookup"] = "not_found"
        return result
    if response.status_code != 200:
        result["lookup"] = "unavailable"
        return result
    try:
        payload = response.json()
    except ValueError:
        result["lookup"] = "unavailable"
        return result
    if not isinstance(payload, dict) or payload.get("messageId") != message_id:
        result["lookup"] = "unavailable"
        return result
    result["lookup"] = "present"
    result["not_before"] = payload.get("notBefore")
    result["created_at"] = payload.get("createdAt")
    # Never export message body, destination URL, forwarded headers or credentials.
    return result


def collect_inventory():
    token = os.environ.get("QSTASH_TOKEN")
    if not token:
        raise RuntimeError("QStash is not configured")
    store = UpstashRedis()
    bookings = store.list_bookings(limit=UpstashRedis.BOOKINGS_MAX_ITEMS)
    if not isinstance(bookings, list):
        raise ValueError("Unexpected FitBot bookings")
    unique = []
    seen = set()
    invalid = 0
    for item in bookings:
        if not isinstance(item, dict):
            continue
        mid = item.get("message_id")
        if not mid:
            continue
        if not isinstance(mid, str) or not MESSAGE_ID_RE.fullmatch(mid):
            invalid += 1
            continue
        if mid in seen:
            continue
        seen.add(mid)
        unique.append(item)
    truncated = len(unique) > MAX_MESSAGES
    selected = unique[:MAX_MESSAGES]
    with ThreadPoolExecutor(max_workers=5) as pool:
        message_checks = list(pool.map(lambda item: _lookup_one(item, token), selected))
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "bookings_scanned": len(bookings),
        "unique_message_ids": len(unique),
        "invalid_message_ids": invalid,
        "checked_count": len(message_checks),
        "truncated": truncated,
        "message_checks": message_checks,
        "limitations": [
            "404 means not currently retrievable, not proof that delivery succeeded.",
            "QStash removes messages shortly after delivery.",
            "Only message IDs already saved in FitBot were queried.",
            "No messages, bookings, or schedules were modified.",
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
        except (RedisUnavailable, RuntimeError, ValueError):
            logger.exception("Could not inspect stored QStash message IDs")
            return self._respond(503, {"error": "QStash message lookup unavailable"})

    def _respond(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.info("FitBot QStash message inspection: " + format, *args)
