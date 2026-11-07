import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeHandler:
    def __init__(self, payload):
        body = json.dumps(payload).encode()
        self.headers = {
            "Content-Length": str(len(body)),
            "Upstash-Signature": "valid",
        }
        self.rfile = io.BytesIO(body)
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
        from api.book import handler

        handler.do_POST(self)
        return json.loads(self.wfile.getvalue())


def _processing_booking(updated_at):
    now = datetime.now(timezone.utc)
    return {
        "id": "c" * 32,
        "person_id": "person_1",
        "date": now.date().isoformat(),
        "time": "10:00",
        "name": "Strength Conditioning",
        "activity_type": "class",
        "opens_at": (now - timedelta(minutes=10)).isoformat(),
        "status": "processing",
        "created_at": (now - timedelta(minutes=20)).isoformat(),
        "updated_at": updated_at.isoformat(),
    }


def _configure(monkeypatch, book_api, booking, redis_class):
    monkeypatch.setenv("VERCEL_ENV", "preview")
    # Preview must remain dry-run even if the environment variable is wrong.
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setattr(book_api, "_verify_qstash", lambda raw, signature: True)
    monkeypatch.setattr(
        book_api,
        "load_config",
        lambda path: {"timezone": "Europe/Madrid", "window_minutes": 30},
    )
    monkeypatch.setattr(book_api, "UpstashRedis", redis_class)


def test_stale_dynamic_processing_is_marked_unconfirmed_without_retry(monkeypatch):
    import api.book as book_api

    stale_at = datetime.now(timezone.utc) - timedelta(
        seconds=book_api.PROCESSING_LOCK_TTL + 1
    )
    booking = _processing_booking(stale_at)
    events = []

    class FakeRedis:
        def get_booking(self, booking_id):
            assert booking_id == booking["id"]
            return dict(booking)

        def update_booking_status(
            self, booking_id, status, expected_statuses=None, metadata=None
        ):
            assert booking_id == booking["id"]
            assert expected_statuses == ["processing"]
            booking.update(metadata or {})
            booking["status"] = status
            events.append(("status", status))
            return dict(booking)

        def acquire(self, record, dry_run, ttl_seconds):
            assert dry_run is True
            assert ttl_seconds == book_api.PROCESSING_LOCK_TTL
            events.append(("acquire", dry_run))
            return "recovery-lock"

        def complete(self, record, token, status, dry_run=False, ttl_seconds=60):
            events.append(("complete", status, dry_run, ttl_seconds))
            return True

    _configure(monkeypatch, book_api, booking, FakeRedis)

    def fail_if_retried(*args, **kwargs):
        raise AssertionError("stale bookings must never be retried")

    monkeypatch.setattr(book_api, "run", fail_if_retried)

    request = FakeHandler({"booking_id": booking["id"]})
    response = request.post()

    assert request.status == 200
    assert response["status"] == "outcome_unconfirmed"
    assert response["error_code"] == "stale_processing_recovered"
    assert booking["status"] == "outcome_unconfirmed"
    assert booking["error_code"] == "stale_processing_recovered"
    assert events == [
        ("status", "outcome_unconfirmed"),
        ("acquire", True),
        ("complete", "outcome_unconfirmed", True, 60),
    ]


def test_recent_processing_is_left_alone(monkeypatch):
    import api.book as book_api

    booking = _processing_booking(datetime.now(timezone.utc))

    class FakeRedis:
        def get_booking(self, booking_id):
            return dict(booking)

        def update_booking_status(self, *args, **kwargs):
            raise AssertionError("recent processing must not be recovered")

        def acquire(self, *args, **kwargs):
            raise AssertionError("recent processing must not acquire another lock")

    _configure(monkeypatch, book_api, booking, FakeRedis)
    request = FakeHandler({"booking_id": booking["id"]})
    response = request.post()

    assert request.status == 200
    assert response["status"] == "duplicate_ignored"
    assert booking["status"] == "processing"


def test_processing_with_invalid_timestamp_fails_closed():
    from api.book import _recover_stale_processing

    booking = {"id": "d" * 32, "updated_at": "not-a-timestamp"}

    class FakeRedis:
        def update_booking_status(self, *args, **kwargs):
            raise AssertionError("invalid timestamp must not be recovered")

    assert _recover_stale_processing(booking, FakeRedis(), dry_run=True) is False
