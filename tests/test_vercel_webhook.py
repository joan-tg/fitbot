import sys
import pytest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.book import _find_configured_booking, _is_booking_due


def test_webhook_only_accepts_a_booking_present_in_config():
    booking = {
        "date": "2026-11-04",
        "opens_at": "2026-11-01T17:15:00",
        "time": "17:15",
        "name": "Weightlifting",
    }
    config = {"bookings": [booking]}

    assert _find_configured_booking(config, dict(booking)) == booking


def test_webhook_rejects_modified_or_unknown_booking():
    booking = {
        "date": "2026-11-04",
        "opens_at": "2026-11-01T17:15:00",
        "time": "17:15",
        "name": "Weightlifting",
    }
    config = {"bookings": [booking]}
    forged = dict(booking, name="Metcon")

    assert _find_configured_booking(config, forged) is None
    assert _find_configured_booking(config, {"date": "2026-11-05"}) is None


def test_booking_window_starts_at_opening_and_ends_after_30_minutes():
    timezone = ZoneInfo("Europe/Madrid")
    booking = {"opens_at": "2026-11-01T17:15:00"}

    assert _is_booking_due(
        booking, timezone, datetime(2026, 11, 1, 17, 15, tzinfo=timezone), 30
    )
    assert _is_booking_due(
        booking, timezone, datetime(2026, 11, 1, 17, 45, tzinfo=timezone), 30
    )
    assert not _is_booking_due(
        booking, timezone, datetime(2026, 11, 1, 17, 14, 59, tzinfo=timezone), 30
    )
    assert not _is_booking_due(
        booking, timezone, datetime(2026, 11, 1, 17, 45, 1, tzinfo=timezone), 30
    )


class FakeHeaders(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class FakeHandler:
    def __init__(self, body, signature="valid"):
        import io

        from api.book import handler

        self._handler = handler.__new__(handler)
        self._handler.headers = FakeHeaders(
            {"Content-Length": str(len(body)), "Upstash-Signature": signature}
        )
        self._handler.rfile = io.BytesIO(body)
        self._handler.wfile = io.BytesIO()
        self.status = None
        self.response_headers = {}
        self._handler.send_response = self._send_response
        self._handler.send_header = self._send_header
        self._handler.end_headers = lambda: None
        self._handler.wfile = self._handler.wfile

    def _send_response(self, status):
        self.status = status

    def _send_header(self, key, value):
        self.response_headers[key] = value

    def post(self):
        from api.book import handler

        handler.do_POST(self._handler)
        import json

        return json.loads(self._handler.wfile.getvalue())


def test_post_with_valid_signature_runs_one_booking_in_dry_run(monkeypatch):
    import json
    import api.book as book_api

    booking = {
        "date": "2026-11-04",
        "opens_at": "2026-11-01T17:15:00",
        "time": "17:15",
        "name": "Weightlifting",
    }
    body = json.dumps(booking).encode("utf-8")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setattr(book_api, "_verify_qstash", lambda raw, signature: True)
    monkeypatch.setattr(
        book_api,
        "load_config",
        lambda path: {
            "timezone": "Europe/Madrid",
            "window_minutes": 30,
            "bookings": [booking],
        },
    )
    fixed_datetime = type(
        "FixedDateTime",
        (),
        {
            "now": staticmethod(
                lambda timezone: datetime(2026, 11, 1, 17, 20, tzinfo=timezone)
            )
        },
    )
    monkeypatch.setattr(book_api, "datetime", fixed_datetime)
    calls = []

    def fake_run(config, now):
        calls.append(config)
        return {
            "status": "completed",
            "processed": 1,
            "outcomes": [{"status": "dry_run", "booking": booking}],
        }

    monkeypatch.setattr(book_api, "run", fake_run)

    request = FakeHandler(body)
    response = request.post()

    assert request.status == 200
    assert response["status"] == "dry_run"
    assert response["dry_run"] is True
    assert len(calls) == 1
    assert calls[0]["bookings"] == [booking]
    assert calls[0]["dry_run"] is True


def test_post_rejects_invalid_signature_before_processing(monkeypatch):
    import json
    import api.book as book_api

    body = json.dumps({"date": "2026-11-04"}).encode("utf-8")
    monkeypatch.setattr(book_api, "_verify_qstash", lambda raw, signature: False)
    monkeypatch.setattr(book_api, "run", lambda *args, **kwargs: pytest.fail("must not run"))
    request = FakeHandler(body, signature="invalid")

    response = request.post()

    assert request.status == 401
    assert response["error"] == "Invalid signature"
