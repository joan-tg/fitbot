"""Read-only synchronization check for confirmed future bookings on dashboard entry."""

import hmac
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from client import AimHarderClient  # noqa: E402
from exceptions import AimHarderUnavailable  # noqa: E402
from fitbot_cli import get_class_to_book  # noqa: E402
from redis_store import RedisUnavailable, UpstashRedis  # noqa: E402
from scheduled import load_config  # noqa: E402

logger = logging.getLogger("fitbot.sync")
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
    expected = os.environ.get("FITBOT_ADMIN_TOKEN", "")
    supplied = handler.headers.get("Authorization", "")
    return bool(
        expected
        and supplied.startswith("Bearer ")
        and hmac.compare_digest(supplied[7:], expected)
    )


def _check_confirmed_bookings(config, records, now, client_factory=None):
    """Compare only previously confirmed future bookings; never write to Redis."""
    client_factory = client_factory or AimHarderClient
    timezone = ZoneInfo(config.get("timezone", "Europe/Madrid"))
    grouped = defaultdict(list)
    for record in records:
        if record.get("status") not in CONFIRMED_STATUSES:
            continue
        if record.get("person_id") not in config.get("members", {}):
            continue
        try:
            target_date = date.fromisoformat(record["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if target_date < now.astimezone(timezone).date():
            continue
        grouped[(record["person_id"], target_date)].append(record)

    discrepancies = []
    unverified = []
    checked = 0
    clients = {}

    for (person_id, target_date), bookings in sorted(
        grouped.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        member = config["members"][person_id]
        credentials = member.get("aimharder_credentials", {})
        email_env = credentials.get("email_env")
        password_env = credentials.get("password_env")
        email = os.environ.get(email_env) if email_env else None
        password = os.environ.get(password_env) if password_env else None
        if not email or not password:
            unverified.extend(
                {
                    "booking_id": booking.get("id"),
                    "person_id": person_id,
                    "date": booking.get("date"),
                    "time": booking.get("time"),
                    "name": booking.get("name"),
                    "reason": "credentials_unavailable",
                }
                for booking in bookings
            )
            continue

        if person_id not in clients:
            try:
                clients[person_id] = client_factory(
                    email=email,
                    password=password,
                    box_id=int(config["box_id"]),
                    box_name=config["box_name"],
                    proxy=os.environ.get("PROXY") or None,
                )
            except Exception:
                logger.exception("AimHarder login failed during read-only sync for %s", person_id)
                clients[person_id] = None
        client = clients[person_id]
        if client is None:
            unverified.extend(
                {
                    "booking_id": booking.get("id"),
                    "person_id": person_id,
                    "date": booking.get("date"),
                    "time": booking.get("time"),
                    "name": booking.get("name"),
                    "reason": "account_unavailable",
                }
                for booking in bookings
            )
            continue

        target_day = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone)
        try:
            classes = client.get_classes(target_day, config.get("family_id"))
        except Exception:
            logger.exception("AimHarder classes lookup failed for %s on %s", person_id, target_date)
            unverified.extend(
                {
                    "booking_id": booking.get("id"),
                    "person_id": person_id,
                    "date": booking.get("date"),
                    "time": booking.get("time"),
                    "name": booking.get("name"),
                    "reason": "classes_unavailable",
                }
                for booking in bookings
            )
            continue

        for booking in bookings:
            try:
                selected = get_class_to_book(
                    classes,
                    booking["time"].replace(":", ""),
                    booking["name"],
                )
            except Exception:
                # Missing/renamed/ambiguous classes are not proof of cancellation.
                unverified.append(
                    {
                        "booking_id": booking.get("id"),
                        "person_id": person_id,
                        "date": booking.get("date"),
                        "time": booking.get("time"),
                        "name": booking.get("name"),
                        "reason": "class_not_resolved",
                    }
                )
                continue

            book_state = selected.get("bookState")
            if book_state == 1:
                checked += 1
            elif book_state == 0:
                checked += 1
                discrepancies.append(
                    {
                        "booking_id": booking.get("id"),
                        "person_id": person_id,
                        "date": booking.get("date"),
                        "time": booking.get("time"),
                        "name": booking.get("name"),
                        "reason": "not_booked_in_aimharder",
                    }
                )
            else:
                unverified.append(
                    {
                        "booking_id": booking.get("id"),
                        "person_id": person_id,
                        "date": booking.get("date"),
                        "time": booking.get("time"),
                        "name": booking.get("name"),
                        "reason": "unknown_booking_state",
                    }
                )

    return {
        "status": "completed",
        "checked": checked,
        "discrepancies": discrepancies,
        "unverified": unverified,
        "read_only": True,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not _authorized(self):
            return _respond(self, 401, {"error": "Unauthorized"})
        if os.environ.get("VERCEL_ENV") not in ("preview", "production"):
            return _respond(
                self,
                403,
                {"error": "Vercel Preview or Production environment only"},
            )

        try:
            config = load_config(ROOT / "reservas.json")
            timezone = ZoneInfo(config.get("timezone", "Europe/Madrid"))
            now = datetime.now(timezone)
            records = UpstashRedis().list_bookings(
                limit=UpstashRedis.BOOKINGS_MAX_ITEMS
            )
            result = _check_confirmed_bookings(config, records, now)
            logger.info(
                "Read-only booking sync completed: checked=%s discrepancies=%s unverified=%s",
                result["checked"],
                len(result["discrepancies"]),
                len(result["unverified"]),
            )
            return _respond(self, 200, result)
        except RedisUnavailable:
            logger.exception("Booking store unavailable during read-only sync")
            return _respond(self, 503, {"error": "Booking store unavailable"})
        except Exception:
            logger.exception("Unexpected read-only booking sync failure")
            return _respond(self, 503, {"error": "Booking sync unavailable"})

    def do_POST(self):
        return _respond(self, 405, {"error": "Method not allowed"})

    def log_message(self, format, *args):
        logger.info("FitBot sync request: " + format, *args)
