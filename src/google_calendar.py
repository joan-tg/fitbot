"""Create idempotent Joan-only events after verified real AimHarder bookings.

Requires a Google Calendar with writer access for a service account. Secrets are
read only from Vercel environment variables; absence disables this integration.
"""

import hashlib
import json
import os
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
SAGE_EVENT_COLOR = "2"
GOOGLE_CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars"


def _event_payload(booking, duration_minutes=60):
    """Build a Google Calendar event from the confirmed booking."""
    if not isinstance(duration_minutes, int) or not 1 <= duration_minutes <= 720:
        raise ValueError("Google Calendar duration must be between 1 and 720 minutes")
    zone = ZoneInfo("Europe/Madrid")
    start = datetime.fromisoformat(booking["date"] + "T" + booking["time"]).replace(
        tzinfo=zone
    )
    finish = start + timedelta(minutes=duration_minutes)
    title = (
        "Strength & Conditioning"
        if booking["name"] == "Strength Conditioning"
        else booking["name"]
    )
    digest = hashlib.sha256(("joan:" + booking["id"]).encode("utf-8")).hexdigest()
    event_id = "fitbot" + digest[:32]
    return {
        "id": event_id,
        "summary": title,
        "location": "Studio CrossFit",
        "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Madrid"},
        "end": {"dateTime": finish.isoformat(), "timeZone": "Europe/Madrid"},
        "colorId": SAGE_EVENT_COLOR,
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 30}],
        },
        "extendedProperties": {"private": {"fitbot_booking_id": booking["id"]}},
    }


def create_joan_calendar_event(booking, *, dry_run=False, environment=None):
    """Write only successful Joan bookings to the configured Google calendar.

    Uses a stable event ID: a repeated webhook cannot create a second event.
    Any calendar failure must be handled by the caller independently of booking.
    """
    if dry_run or environment != "production" or booking.get("person_id") != "person_1":
        return "skipped"
    raw_credentials = os.environ.get("GOOGLE_CALENDAR_SERVICE_ACCOUNT_JSON")
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    if not raw_credentials or not calendar_id:
        return "not_configured"

    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_info(
        json.loads(raw_credentials),
        scopes=[CALENDAR_SCOPE],
    )
    credentials.refresh(Request())
    duration = int(os.environ.get("GOOGLE_CALENDAR_EVENT_MINUTES", "60"))
    event = _event_payload(booking, duration_minutes=duration)
    response = requests.post(
        GOOGLE_CALENDAR_URL + "/" + quote(calendar_id, safe="") + "/events",
        headers={"Authorization": "Bearer " + credentials.token},
        json=event,
        timeout=10,
    )
    if response.status_code == 409:
        return "already_exists"
    response.raise_for_status()
    return "created"
