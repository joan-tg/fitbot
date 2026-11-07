import json
import sys
from datetime import datetime, timedelta
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
    def __init__(self, payload, signature="valid", retried=None):
        import io

        headers = {
            "Content-Length": str(len(payload)),
            "Upstash-Signature": signature,
        }
        if retried is not None:
            headers["Upstash-Retried"] = str(retried)
        self.headers = FakeHeaders(headers)
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


def test_pre_booking_aimharder_outage_releases_live_lock_for_retry(monkeypatch):
    import api.book as book_api
    from exceptions import AimHarderUnavailable

    booking = {
        "date": "2026-11-06",
        "opens_at": "2026-11-03T10:00:00",
        "time": "10:00",
        "name": "Weightlifting",
    }
    payload = json.dumps(booking).encode()
    released = []

    class FakeRedis:
        def acquire(self, booking_data, dry_run, ttl_seconds):
            assert booking_data == booking
            assert dry_run is False
            return "owner-token"

        def release(self, booking_data, token, dry_run):
            released.append((booking_data, token, dry_run))
            return True

    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setattr(book_api, "_verify_qstash", lambda raw, signature: True)
    monkeypatch.setattr(book_api, "_is_booking_due", lambda *args: True)
    monkeypatch.setattr(
        book_api,
        "load_config",
        lambda path: {
            "timezone": "Europe/Madrid",
            "window_minutes": 30,
            "bookings": [booking],
        },
    )
    monkeypatch.setattr(book_api, "UpstashRedis", FakeRedis)

    def fail_before_booking(*args, **kwargs):
        raise AimHarderUnavailable("login timeout")

    monkeypatch.setattr(book_api, "run", fail_before_booking)
    request = FakeHandler(payload)
    response = request.post()

    assert request.status == 503
    assert response["error"] == "AimHarder unavailable before booking"
    assert released == [(booking, "owner-token", False)]


def _auth_test_booking():
    timezone = ZoneInfo("Europe/Madrid")
    now = datetime.now(timezone)
    return {
        "date": now.date().isoformat(),
        "opens_at": (now - timedelta(seconds=30)).isoformat(timespec="seconds"),
        "time": "10:00",
        "name": "Weightlifting",
    }


def _configure_auth_test(monkeypatch, book_api, booking, redis_class, retried=None):
    payload = json.dumps(booking).encode()
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setattr(book_api, "_verify_qstash", lambda raw, signature: True)
    monkeypatch.setattr(book_api, "_is_booking_due", lambda *args: True)
    monkeypatch.setattr(
        book_api,
        "load_config",
        lambda path: {
            "timezone": "Europe/Madrid",
            "window_minutes": 30,
            "bookings": [booking],
        },
    )
    monkeypatch.setattr(book_api, "UpstashRedis", redis_class)
    return FakeHandler(payload, retried=retried)


def test_incorrect_credentials_are_non_retryable_and_keep_short_lock(monkeypatch):
    import api.book as book_api
    from exceptions import IncorrectCredentials

    booking = _auth_test_booking()
    completed = []
    recorded = []

    class FakeRedis:
        def acquire(self, booking_data, dry_run, ttl_seconds):
            assert booking_data == booking
            assert dry_run is False
            assert ttl_seconds == book_api.PROCESSING_LOCK_TTL
            return "owner-token"

        def complete(self, booking_data, token, status, dry_run=False, ttl_seconds=60):
            completed.append((booking_data, token, status, dry_run, ttl_seconds))
            return True

        def record_booking_result(self, record):
            recorded.append(record)

    def fail_login(*args, **kwargs):
        raise IncorrectCredentials()

    monkeypatch.setattr(book_api, "run", fail_login)
    request = _configure_auth_test(monkeypatch, book_api, booking, FakeRedis)
    response = request.post()

    assert request.status == 489
    assert request.response_headers["Upstash-NonRetryable-Error"] == "true"
    assert response["status"] == "incorrect_credentials"
    assert completed == [
        (
            booking,
            "owner-token",
            "incorrect_credentials",
            False,
            book_api.AUTH_FAILURE_LOCK_TTL,
        )
    ]
    assert recorded[0]["status"] == "incorrect_credentials"


def test_too_many_wrong_attempts_schedules_only_one_delayed_retry(monkeypatch):
    import api.book as book_api
    from exceptions import TooManyWrongAttempts

    booking = _auth_test_booking()
    released = []
    recorded = []

    class FakeRedis:
        def acquire(self, booking_data, dry_run, ttl_seconds):
            assert ttl_seconds == book_api.PROCESSING_LOCK_TTL
            return "owner-token"

        def release(self, booking_data, token, dry_run):
            released.append((booking_data, token, dry_run))
            return True

        def record_booking_result(self, record):
            recorded.append(record)

    def fail_login(*args, **kwargs):
        raise TooManyWrongAttempts()

    monkeypatch.setattr(book_api, "run", fail_login)
    request = _configure_auth_test(monkeypatch, book_api, booking, FakeRedis, retried=0)
    response = request.post()

    assert request.status == 503
    assert request.response_headers["Retry-After"] == "60"
    assert response["status"] == "too_many_wrong_attempts_retry_scheduled"
    assert released == [(booking, "owner-token", False)]
    assert recorded[0]["status"] == "too_many_wrong_attempts_retry_scheduled"


def test_second_too_many_wrong_attempts_is_non_retryable(monkeypatch):
    import api.book as book_api
    from exceptions import TooManyWrongAttempts

    booking = _auth_test_booking()
    completed = []

    class FakeRedis:
        def acquire(self, booking_data, dry_run, ttl_seconds):
            assert ttl_seconds == book_api.PROCESSING_LOCK_TTL
            return "owner-token"

        def complete(self, booking_data, token, status, dry_run=False, ttl_seconds=60):
            completed.append((status, dry_run, ttl_seconds))
            return True

        def record_booking_result(self, record):
            pass

    def fail_login(*args, **kwargs):
        raise TooManyWrongAttempts()

    monkeypatch.setattr(book_api, "run", fail_login)
    request = _configure_auth_test(monkeypatch, book_api, booking, FakeRedis, retried=1)
    response = request.post()

    assert request.status == 489
    assert request.response_headers["Upstash-NonRetryable-Error"] == "true"
    assert response["status"] == "too_many_wrong_attempts"
    assert completed == [
        ("too_many_wrong_attempts", False, book_api.AUTH_FAILURE_LOCK_TTL)
    ]


def test_unexpected_live_error_promotes_processing_lock_to_long_safety_lock(
    monkeypatch,
):
    import api.book as book_api

    booking = _auth_test_booking()
    completed = []

    class FakeRedis:
        def acquire(self, booking_data, dry_run, ttl_seconds):
            assert ttl_seconds == book_api.PROCESSING_LOCK_TTL
            return "owner-token"

        def complete(self, booking_data, token, status, dry_run=False, ttl_seconds=60):
            completed.append((status, dry_run, ttl_seconds))
            return True

    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setattr(book_api, "_verify_qstash", lambda raw, signature: True)
    monkeypatch.setattr(book_api, "_is_booking_due", lambda *args: True)
    monkeypatch.setattr(
        book_api,
        "load_config",
        lambda path: {
            "timezone": "Europe/Madrid",
            "window_minutes": 30,
            "bookings": [booking],
        },
    )
    monkeypatch.setattr(book_api, "UpstashRedis", FakeRedis)

    def fail_unexpectedly(*args, **kwargs):
        raise RuntimeError("unexpected test failure")

    monkeypatch.setattr(book_api, "run", fail_unexpectedly)
    request = FakeHandler(json.dumps(booking).encode())
    response = request.post()

    assert request.status == 500
    assert response["error"] == "Booking processing failed"
    assert completed == [("outcome_unconfirmed", False, book_api.LIVE_LOCK_TTL)]
