"""Authenticated, read-only export of FitBot's stored booking data."""

import hmac
import json
import logging
import os
import sys
from pathlib import Path
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from redis_store import RedisUnavailable, UpstashRedis

logger = logging.getLogger("fitbot.export")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        expected = os.environ.get("FITBOT_ADMIN_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        if not expected or not supplied.startswith("Bearer ") or not hmac.compare_digest(supplied[7:], expected):
            return self._respond(401, {"error": "Unauthorized"})

        if os.environ.get("VERCEL_ENV") not in ("preview", "production"):
            return self._respond(403, {"error": "Vercel Preview or Production environment only"})

        try:
            store = UpstashRedis()
            bookings = store.list_bookings(limit=UpstashRedis.BOOKINGS_MAX_ITEMS)
            results = store.list_booking_results(limit=UpstashRedis.RESULTS_MAX_ITEMS)
            payload = {
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "read_only": True,
                "bookings_count": len(bookings),
                "results_count": len(results),
                "bookings": bookings,
                "results": results,
                "notes": [
                    "This export is read-only and does not modify Redis or QStash.",
                    "QStash schedules are not included in this export."
                ],
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", 'attachment; filename="fitbot-inventory.json"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except RedisUnavailable:
            logger.exception("Could not export FitBot booking data")
            self._respond(503, {"error": "Booking data unavailable"})

    def _respond(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.info("FitBot export request: " + format, *args)
