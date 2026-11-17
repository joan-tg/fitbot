"""Read-only AimHarder committed, future-reserved and elapsed credit totals.

No booking or cancellation requests and no Redis writes.
"""

import hmac
import html
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from client import AimHarderClient  # noqa: E402
from scheduled import load_config  # noqa: E402

logger = logging.getLogger("fitbot.live_credits")
CREDIT_PAIR = re.compile(r"\(\s*(\d+)\s*/\s*(\d+)\s*\)")
PERSON_IDS = ("person_1", "person_2")


def parse_committed_credits(value):
    """Parse committed/total counts; never invent absent categories."""
    if not isinstance(value, str):
        raise ValueError("Missing clasesDisp")
    result = {"classes": None, "open_box": None}
    for segment in re.split(r"<br\s*/?>", value, flags=re.IGNORECASE):
        segment = html.unescape(segment).strip()
        if not segment:
            continue
        match = CREDIT_PAIR.search(segment)
        if not match:
            continue
        committed, total = map(int, match.groups())
        if total > 1000 or committed > total:
            raise ValueError("Invalid AimHarder credit numbers")
        category = "open_box" if "open box" in segment.casefold() else "classes"
        if result[category] is not None:
            raise ValueError("Ambiguous AimHarder credit category")
        result[category] = {
            "committed": committed,
            "total": total,
            "remaining": total - committed,
        }
    if all(value is None for value in result.values()):
        raise ValueError("No recognizable credit balances")
    return result


def count_future_bookings(client, config, now, first_payload):
    """Count upcoming bookings within the configured 72h advance-booking window."""
    if config.get("booking_policy", {}).get("opening_lead_hours") != 72:
        raise ValueError("Unknown booking advance policy")
    zone = now.tzinfo
    allowed = config.get("activities", {})
    classes = set(allowed.get("class", []))
    open_box = set(allowed.get("open_box", []))
    future = {"classes": 0, "open_box": 0}
    day = now.date()
    # Four calendar days ahead also covers daylight-saving changes.
    last_day = day + timedelta(days=4)
    while day <= last_day and day.year == now.year and day.month == now.month:
        local_day = datetime.combine(day, datetime.min.time(), tzinfo=zone)
        payload = (
            first_payload
            if day == now.date()
            else client.get_classes_payload(local_day, config.get("family_id"))
        )
        rows = payload.get("bookings") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("Bookings are missing for a calendar day")
        for booking in rows:
            if not isinstance(booking, dict) or booking.get("bookState") != 1:
                continue
            name = booking.get("className")
            category = (
                "open_box"
                if name in open_box
                else "classes"
                if name in classes
                else None
            )
            if category is None:
                raise ValueError("Unrecognized booked activity")
            time_id = str(booking.get("timeid", "")).split("_", 1)[0]
            if not re.fullmatch(r"(?:[01][0-9]|2[0-3])[0-5][0-9]", time_id):
                raise ValueError("Unrecognized booked time")
            start = datetime.combine(
                day, datetime.strptime(time_id, "%H%M").time(), tzinfo=zone
            )
            if start > now:
                future[category] += 1
        day += timedelta(days=1)
    return future


def separate_consumed_from_future(balances, future):
    result = {}
    for category, item in balances.items():
        if item is None:
            result[category] = None
        else:
            upcoming = future[category]
            committed = item["committed"]
            result[category] = {
                **item,
                "upcoming_reserved": upcoming,
                "consumed": committed - upcoming if upcoming <= committed else None,
                "verified": upcoming <= committed,
            }
    return result


def fetch_member_credits(config, client_factory=AimHarderClient, today=None):
    """Use separate account sessions; gracefully report per-member failures."""
    zone = ZoneInfo(config.get("timezone", "Europe/Madrid"))
    today = today or datetime.now(zone)
    records = {}
    for person_id in PERSON_IDS:
        member = config.get("members", {}).get(person_id, {})
        credential_names = member.get("aimharder_credentials", {})
        email_key = credential_names.get("email_env")
        password_key = credential_names.get("password_env")
        email = os.environ.get(email_key) if email_key else None
        password = os.environ.get(password_key) if password_key else None
        if not email or not password:
            records[person_id] = {
                "balances": None,
                "error": "credentials_unavailable",
            }
            continue
        try:
            client = client_factory(
                email=email,
                password=password,
                box_id=int(config["box_id"]),
                box_name=config["box_name"],
                proxy=os.environ.get("PROXY") or None,
            )
            payload = client.get_classes_payload(today, config.get("family_id"))
            if not isinstance(payload, dict):
                raise ValueError("Invalid AimHarder response")
            balances = parse_committed_credits(payload.get("clasesDisp"))
            try:
                future = count_future_bookings(client, config, today, payload)
                balances = separate_consumed_from_future(balances, future)
                records[person_id] = {"balances": balances, "error": None}
            except Exception:
                logger.exception("Could not verify future bookings for %s", person_id)
                records[person_id] = {
                    "balances": balances,
                    "error": "future_bookings_unavailable",
                }
        except Exception:
            logger.exception("Could not read AimHarder credits for %s", person_id)
            records[person_id] = {
                "balances": None,
                "error": "credits_unavailable",
            }
    return records


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        expected = os.environ.get("FITBOT_ADMIN_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        if not (
            expected
            and supplied.startswith("Bearer ")
            and hmac.compare_digest(supplied[7:], expected)
        ):
            return self._respond(401, {"error": "Unauthorized"})
        if os.environ.get("VERCEL_ENV") not in ("production", "preview"):
            return self._respond(403, {"error": "Vercel only"})
        try:
            config = load_config()
            balances = fetch_member_credits(config)
            return self._respond(
                200,
                {
                    "source": "aimharder_clasesDisp",
                    "interpretation": "committed_over_total",
                    "consumed_formula": "committed_minus_future_reserved_72h_window",
                    "members": balances,
                    "read_only": True,
                },
            )
        except Exception:
            logger.exception("Live credit lookup failed")
            return self._respond(503, {"error": "Live credits unavailable"})

    def _respond(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
