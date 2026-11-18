"""Targeted Google Calendar recovery: confirmed-only and no AimHarder side effects."""

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api.reconcile as backfill


def booking(booking_id):
    return {
        "id": booking_id,
        **backfill.TARGETS[booking_id],
        "status": "booking_confirmed",
    }


class Request:
    def __init__(self, payload=None, token="test-token"):
        data = json.dumps(payload or {"booking_ids": list(backfill.TARGETS)}).encode()
        self.headers = {
            "Authorization": "Bearer " + token,
            "Content-Length": str(len(data)),
        }
        self.rfile = io.BytesIO(data)
        self.wfile = io.BytesIO()
        self.status = None

    def send_response(self, code):
        self.status = code

    def send_header(self, *args):
        pass

    def end_headers(self):
        pass

    def run(self):
        backfill.handler.do_PUT(self)
        return json.loads(self.wfile.getvalue().decode())


def configure(monkeypatch, statuses=None):
    statuses = statuses or {}

    class Redis:
        def get_booking(self, booking_id):
            record = booking(booking_id)
            record["status"] = statuses.get(booking_id, "booking_confirmed")
            return record

    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "test-token")
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setattr(backfill, "UpstashRedis", Redis)


def test_only_explicit_two_confirmed_joan_bookings_are_recovered(monkeypatch):
    configure(monkeypatch)
    calls = []

    def create(record, **kwargs):
        calls.append((record["id"], kwargs))
        return "created"

    monkeypatch.setattr(backfill, "create_joan_calendar_event", create)
    request = Request()
    result = request.run()
    assert request.status == 200
    assert result["created"] == 2
    assert result["failed"] == 0
    assert result["read_only_aimharder"] is True
    assert {item[0] for item in calls} == set(backfill.TARGETS)
    assert all(
        options == {"dry_run": False, "environment": "production"}
        for _, options in calls
    )


def test_unconfirmed_booking_prevents_both_calendar_writes(monkeypatch):
    first = next(iter(backfill.TARGETS))
    configure(monkeypatch, statuses={first: "scheduled"})
    monkeypatch.setattr(
        backfill,
        "create_joan_calendar_event",
        lambda *args, **kwargs: pytest.fail("not confirmed, must not write"),
    )
    request = Request()
    result = request.run()
    assert request.status == 409
    assert "confirmada" in result["error"]


def test_cancelled_duplicate_is_never_eligible(monkeypatch):
    configure(monkeypatch)
    request = Request(
        payload={
            "booking_ids": [
                "95c09a6c64224815bbd65620beefcf61",
                next(iter(backfill.TARGETS)),
            ]
        }
    )
    request.run()
    assert request.status == 400


def test_backfill_rejects_preview_simulation_and_bad_credentials(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("VERCEL_ENV", "preview")
    request = Request()
    request.run()
    assert request.status == 403
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("DRY_RUN", "true")
    request = Request()
    request.run()
    assert request.status == 403
    monkeypatch.setenv("DRY_RUN", "false")
    request = Request(token="bad-token")
    request.run()
    assert request.status == 401


def test_idempotent_existing_event_is_not_reported_as_new(monkeypatch):
    configure(monkeypatch)

    def already_exists(record, **kwargs):
        return "already_exists"

    monkeypatch.setattr(backfill, "create_joan_calendar_event", already_exists)
    request = Request()
    result = request.run()
    assert request.status == 200
    assert result["created"] == 0
    assert result["already_exists"] == 2


def test_partial_calendar_failure_is_reported_without_rebooking(monkeypatch):
    configure(monkeypatch)
    first = next(iter(backfill.TARGETS))

    def create(record, **kwargs):
        if record["id"] == first:
            raise RuntimeError("failed")
        return "created"

    monkeypatch.setattr(backfill, "create_joan_calendar_event", create)
    request = Request()
    result = request.run()
    assert request.status == 200
    assert result["failed"] == 1
    assert result["created"] == 1
