"""Google Calendar integration: Joan only, confirmed real bookings only."""

from datetime import datetime
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import google_calendar as calendar


BOOKING = {
    "id": "booking-joan-001",
    "person_id": "person_1",
    "date": "2026-11-23",
    "time": "18:15",
    "name": "Strength Conditioning",
}


def test_payload_has_requested_title_location_color_reminder_and_timezone():
    event = calendar._event_payload(BOOKING)
    assert event["summary"] == "Strength & Conditioning"
    assert event["location"] == "Studio CrossFit"
    assert event["colorId"] == "2"
    assert event["reminders"] == {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": 30}],
    }
    start = datetime.fromisoformat(event["start"]["dateTime"])
    end = datetime.fromisoformat(event["end"]["dateTime"])
    assert start.isoformat() == "2026-11-23T18:15:00+01:00"
    assert (end - start).total_seconds() == 3600
    assert event["start"]["timeZone"] == "Europe/Madrid"
    assert event["end"]["timeZone"] == "Europe/Madrid"


def test_only_strength_conditioning_is_renamed():
    event = calendar._event_payload({**BOOKING, "name": "Weightlifting"})
    assert event["summary"] == "Weightlifting"


def test_stable_event_id_avoids_duplicate_events():
    first = calendar._event_payload(BOOKING)["id"]
    assert first == calendar._event_payload(BOOKING)["id"]
    assert first != calendar._event_payload({**BOOKING, "id": "another"})["id"]


@pytest.mark.parametrize(
    ("person", "dry_run", "environment"),
    [
        ("person_2", False, "production"),
        ("person_1", True, "production"),
        ("person_1", False, "preview"),
    ],
)
def test_other_users_simulation_and_preview_do_not_contact_google(
    monkeypatch, person, dry_run, environment
):
    monkeypatch.setenv("GOOGLE_CALENDAR_ID", "test@example.com")
    monkeypatch.setenv("GOOGLE_CALENDAR_SERVICE_ACCOUNT_JSON", "invalid")
    assert (
        calendar.create_joan_calendar_event(
            {**BOOKING, "person_id": person},
            dry_run=dry_run,
            environment=environment,
        )
        == "skipped"
    )


def test_missing_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("GOOGLE_CALENDAR_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CALENDAR_SERVICE_ACCOUNT_JSON", raising=False)
    assert (
        calendar.create_joan_calendar_event(BOOKING, environment="production")
        == "not_configured"
    )


def test_insert_is_idempotent_and_handles_existing_event(monkeypatch):
    import json
    # The Docker test image does not include the optional Google SDK.
    # Supply fake modules to exercise the integration without credentials.
    google = ModuleType("google")
    auth = ModuleType("google.auth")
    transport = ModuleType("google.auth.transport")
    transport_requests = ModuleType("google.auth.transport.requests")
    oauth2 = ModuleType("google.oauth2")
    service_account = ModuleType("google.oauth2.service_account")
    transport_requests.Request = lambda: object()
    oauth2.service_account = service_account
    google.auth = auth
    google.oauth2 = oauth2
    auth.transport = transport
    transport.requests = transport_requests
    modules = {
        "google": google,
        "google.auth": auth,
        "google.auth.transport": transport,
        "google.auth.transport.requests": transport_requests,
        "google.oauth2": oauth2,
        "google.oauth2.service_account": service_account,
    }
    for key, value in modules.items():
        monkeypatch.setitem(sys.modules, key, value)

    class FakeCredentials:
        token = "token"

        def refresh(self, request):
            pass

    monkeypatch.setenv("GOOGLE_CALENDAR_ID", "calendar@example.com")
    monkeypatch.setenv(
        "GOOGLE_CALENDAR_SERVICE_ACCOUNT_JSON", json.dumps({"type": "service_account"})
    )
    service_account.Credentials = SimpleNamespace(
        from_service_account_info=lambda *args, **kwargs: FakeCredentials()
    )
    calls = []

    def post(url, headers, json, timeout):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return SimpleNamespace(status_code=409, raise_for_status=Mock())

    monkeypatch.setattr(calendar.requests, "post", post)
    assert (
        calendar.create_joan_calendar_event(BOOKING, environment="production")
        == "already_exists"
    )
    assert len(calls) == 1
    assert calls[0]["json"]["colorId"] == "2"
    assert calls[0]["url"].endswith("/calendar%40example.com/events")
