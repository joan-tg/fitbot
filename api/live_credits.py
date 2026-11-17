"""Read-only live AimHarder credit availability for each configured member.

This endpoint never books classes and never writes to Redis.
AimHarder's "clasesDisp" is labeled "Clases disponibles"; therefore the
first number is treated as available, not as used. Verify with the gym.
"""
import hmac
import html
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
from client import AimHarderClient  # noqa: E402
from scheduled import load_config  # noqa: E402

logger = logging.getLogger("fitbot.live_credits")
CREDIT_PAIR = re.compile(r"\(\s*(\d+)\s*/\s*(\d+)\s*\)")
PERSON_IDS = ("person_1", "person_2")


def parse_available_credits(value):
    """Parse displayed available/total pairs; never invent absent categories."""
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
        available, total = map(int, match.groups())
        if total > 1000 or available > total:
            raise ValueError("Invalid AimHarder credit numbers")
        category = "open_box" if "open box" in segment.casefold() else "classes"
        if result[category] is not None:
            raise ValueError("Ambiguous AimHarder credit category")
        result[category] = {
            "available": available,
            "total": total,
            "used_inferred": total - available,
        }
    if all(value is None for value in result.values()):
        raise ValueError("No recognizable credit balances")
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
            records[person_id] = {"balances": None, "error": "credentials_unavailable"}
            continue
        try:
            client = client_factory(
                email=email, password=password,
                box_id=int(config["box_id"]), box_name=config["box_name"],
                proxy=os.environ.get("PROXY") or None,
            )
            payload = client.get_classes_payload(today, config.get("family_id"))
            if not isinstance(payload, dict):
                raise ValueError("Invalid AimHarder response")
            balances = parse_available_credits(payload.get("clasesDisp"))
            records[person_id] = {"balances": balances, "error": None}
        except Exception:
            logger.exception("Could not read AimHarder credits for %s", person_id)
            records[person_id] = {"balances": None, "error": "credits_unavailable"}
    return records


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        expected = os.environ.get("FITBOT_ADMIN_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        if not (expected and supplied.startswith("Bearer ") and
                hmac.compare_digest(supplied[7:], expected)):
            return self._respond(401, {"error": "Unauthorized"})
        if os.environ.get("VERCEL_ENV") not in ("production", "preview"):
            return self._respond(403, {"error": "Vercel only"})
        try:
            config = load_config()
            balances = fetch_member_credits(config)
            return self._respond(200, {
                "source": "aimharder_clasesDisp",
                "interpretation": "available_over_total",
                "verified_as_consumed": False,
                "members": balances,
                "read_only": True,
            })
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
