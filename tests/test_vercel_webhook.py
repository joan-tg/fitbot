import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.book import _find_configured_booking, _is_booking_due, handler


def test_webhook_only_accepts_a_booking_present_in_config():
    booking = {
        "date": "2026-11-04",
        "opens_at": "2026-11-01T17:15:00",
        "time": "17:15",
        "name": "Weightlifting",
    }
    assert _find_configured_booking({"bookings": [booking]}, dict(booking)) == booking


def test_webhook_rejects_modified_or_unknown_booking():
    booking = {
        "date": "2026-11-04",
        "opens_at": "2026-11-01T17:15:00",
        "time": "17:15",
        "name": "Weightlifting",
    }
    config = {"bookings": [booking]}
    assert _find_configured_booking(config, dict(booking, name="Metcon")) is None
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
    pass


class FakeHandler:
    def __init__(self, payload, signature="valid"):
        import io

        self.headers = FakeHeaders(
            {
                "Content-Length": str(len(payload)),
                "Upstash-Signature": signature,
            }
        )
        self.rfile = io.BytesIO(payload)
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def post(self):
        handler.do_POST(self)
        return json.loads(self.wfile.getvalue())


def test_preview_integration_probe_acquires_real_redis_lock_without_running_scheduler(
    monkeypatch,
):
    import api.book as book_api

    payload = json.dumps(
        {
            "date": "integration-run-123",
            "time": "attempt-1",
            "name": "FITBOT_QSTASH_REDIS_INTEGRATION_TEST",
            "probe_id": "integration-run-123",
            "probe_step": "first",
        }
    ).encode()
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setattr(book_api, "_verify_qstash", lambda raw, signature: True)

    class FakeRedis:
        def acquire(self, booking, dry_run, ttl_seconds):
            assert booking["name"] == "FITBOT_QSTASH_REDIS_INTEGRATION_TEST"
            assert dry_run is True
            assert ttl_seconds == 300
            return True

        def record_integration_result(self, probe_id, step, result):
            assert (probe_id, step, result) == (
                "integration-run-123",
                "first",
                "integration_test_lock_acquired",
            )

        @staticmethod
        def integration_result_key(probe_id, step):
            return f"fitbot:integration:{probe_id}:{step}"

    monkeypatch.setattr(book_api, "UpstashRedis", FakeRedis)
    monkeypatch.setattr(
        book_api,
        "run",
        lambda *a, **k: pytest.fail("scheduler must not run"),
    )
    request = FakeHandler(payload)
    response = request.post()

    assert request.status == 200
    assert response["status"] == "integration_test_lock_acquired"
    assert response["dry_run"] is True
    assert response["environment"] == "preview"


@pytest.mark.parametrize(
    ("vercel_env", "dry_run"),
    [("production", "true"), ("preview", "false")],
)
def test_integration_probe_is_blocked_outside_safe_preview(
    monkeypatch, vercel_env, dry_run
):
    import api.book as book_api

    payload = json.dumps(
        {
            "date": "integration-run-123",
            "time": "attempt-1",
            "name": "FITBOT_QSTASH_REDIS_INTEGRATION_TEST",
        }
    ).encode()
    monkeypatch.setenv("VERCEL_ENV", vercel_env)
    monkeypatch.setenv("DRY_RUN", dry_run)
    monkeypatch.setattr(book_api, "_verify_qstash", lambda raw, signature: True)
    monkeypatch.setattr(
        book_api,
        "UpstashRedis",
        lambda: pytest.fail("Redis must not be touched outside safe Preview"),
    )
    request = FakeHandler(payload)
    response = request.post()

    assert request.status == 403
    assert "error" in response
