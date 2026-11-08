"""Protected endpoint for inspecting recent FitBot booking results."""

import hmac
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from redis_store import RedisUnavailable, UpstashRedis  # noqa: E402

logger = logging.getLogger("fitbot.results")


def _response(handler, status, payload):
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


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not _authorized(self):
            return _response(self, 401, {"error": "Unauthorized"})
        try:
            query = parse_qs(urlsplit(self.path).query)
            limit = int(query.get("limit", ["50"])[0])
            if limit < 1 or limit > 100:
                return _response(
                    self, 400, {"error": "limit must be between 1 and 100"}
                )
            results = UpstashRedis().list_booking_results(limit=limit)
            return _response(self, 200, {"count": len(results), "results": results})
        except ValueError:
            return _response(self, 400, {"error": "Invalid limit"})
        except RedisUnavailable:
            logger.exception("Could not read booking result history")
            return _response(self, 503, {"error": "Result history unavailable"})

    def do_DELETE(self):
        if not _authorized(self):
            return _response(self, 401, {"error": "Unauthorized"})
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > 4096:
                return _response(self, 413, {"error": "Invalid request body size"})
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {"recorded_at"}:
                return _response(
                    self, 400, {"error": "Body must contain only recorded_at"}
                )
            timestamps = payload["recorded_at"]
            if (
                not isinstance(timestamps, list)
                or not 1 <= len(timestamps) <= 10
                or any(not isinstance(value, str) for value in timestamps)
            ):
                return _response(
                    self,
                    400,
                    {"error": "recorded_at must be a list of 1 to 10 timestamps"},
                )
            for value in timestamps:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    return _response(
                        self, 400, {"error": "Timestamps must include a timezone"}
                    )
            deleted = UpstashRedis().delete_confirmed_booking_results(timestamps)
            return _response(self, 200, {"deleted": deleted})
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return _response(self, 400, {"error": "Invalid deletion request"})
        except RedisUnavailable:
            logger.exception("Could not delete booking result history")
            return _response(self, 503, {"error": "Result history unavailable"})

    def do_POST(self):
        return _response(self, 405, {"error": "Method not allowed"})

    def log_message(self, format, *args):
        logger.info("FitBot results request: " + format, *args)
