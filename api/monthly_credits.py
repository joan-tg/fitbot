"""Admin-only monthly AimHarder credit counts entered from the official app.

This endpoint deliberately does not infer consumed credits from booking records.
Production can store explicit observed amounts; Preview is read-only.
"""

import hmac
import json
import logging
import os
import re
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from redis_store import RedisUnavailable, UpstashRedis  # noqa: E402
from live_credits import fetch_member_credits  # noqa: E402
from scheduled import load_config  # noqa: E402

logger = logging.getLogger("fitbot.monthly_credits")
MONTH_PATTERN = re.compile(r"^20[0-9]{2}-(0[1-9]|1[0-2])$")
MAX_BODY = 1024
PERSON_IDS = ("person_1", "person_2")
CATEGORIES = ("classes", "open_box")


def _current_month():
    return datetime.now(ZoneInfo("Europe/Madrid")).strftime("%Y-%m")


def _key(month):
    if not isinstance(month, str) or not MONTH_PATTERN.fullmatch(month):
        raise ValueError("Invalid month")
    return "fitbot:monthly_credits:" + month


def _validate_counts(counts):
    if not isinstance(counts, dict) or set(counts) != set(PERSON_IDS):
        raise ValueError("Both members must be present")
    for person_id in PERSON_IDS:
        person = counts[person_id]
        if not isinstance(person, dict) or set(person) != set(CATEGORIES):
            raise ValueError("Both credit categories must be present")
        for category in CATEGORIES:
            value = person[category]
            if value is not None and (
                type(value) is not int or value < 0 or value > 100
            ):
                raise ValueError("Credit counts must be integers from 0 to 100 or null")


def _read(store, month):
    raw = store._command(["GET", _key(month)])
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        _validate_counts(data["counts"])
        if data.get("source") != "manual_aimharder":
            raise ValueError("Invalid data source")
    except (KeyError, TypeError, ValueError) as error:
        raise RedisUnavailable("Invalid stored monthly credit counts") from error
    return data


class handler(BaseHTTPRequestHandler):
    def _authorized(self):
        expected = os.environ.get("FITBOT_ADMIN_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        return bool(
            expected
            and supplied.startswith("Bearer ")
            and hmac.compare_digest(supplied[7:], expected)
        )

    def do_GET(self):
        if not self._authorized():
            return self._respond(401, {"error": "Unauthorized"})
        if os.environ.get("VERCEL_ENV") not in ("production", "preview"):
            return self._respond(403, {"error": "Vercel only"})
        if getattr(self, "path", "").partition("?")[2] == "source=live":
            try:
                members = fetch_member_credits(load_config())
                return self._respond(
                    200,
                    {
                        "month": _current_month(),
                        "source": "aimharder_clasesDisp",
                        "interpretation": "committed_over_total",
                        "consumed_formula": "committed_minus_future_reserved_72h_window",
                        "members": members,
                        "read_only": True,
                    },
                )
            except Exception:
                logger.exception("Live credit lookup failed")
                return self._respond(503, {"error": "Live credits unavailable"})
        try:
            month = _current_month()
            record = _read(UpstashRedis(), month)
            return self._respond(
                200,
                {
                    "month": month,
                    "counts": record["counts"] if record else None,
                    "updated_at": record.get("updated_at") if record else None,
                    "source": "manual_aimharder" if record else None,
                    "read_only": os.environ.get("VERCEL_ENV") == "preview",
                },
            )
        except RedisUnavailable:
            logger.exception("Could not read monthly credits")
            return self._respond(503, {"error": "Monthly credits unavailable"})

    def do_POST(self):
        if not self._authorized():
            return self._respond(401, {"error": "Unauthorized"})
        if os.environ.get("VERCEL_ENV") != "production":
            return self._respond(403, {"error": "Production only"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= MAX_BODY:
                raise ValueError("Invalid body length")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or set(payload) != {"month", "counts"}:
                raise ValueError("Unexpected fields")
            month = _current_month()
            if payload["month"] != month:
                raise ValueError("Month changed; reload before saving")
            _validate_counts(payload["counts"])
            record = {
                "counts": payload["counts"],
                "source": "manual_aimharder",
                "updated_at": datetime.now(ZoneInfo("Europe/Madrid")).isoformat(),
            }
            UpstashRedis()._command(
                [
                    "SET",
                    _key(month),
                    json.dumps(record, separators=(",", ":")),
                    "EX",
                    110 * 86400,
                ]
            )
            return self._respond(
                200,
                {
                    "month": month,
                    "counts": record["counts"],
                    "updated_at": record["updated_at"],
                },
            )
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            return self._respond(400, {"error": str(error)})
        except RedisUnavailable:
            logger.exception("Could not store monthly credits")
            return self._respond(503, {"error": "Monthly credits unavailable"})

    def _respond(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.info("Monthly credit count request: " + format, *args)
