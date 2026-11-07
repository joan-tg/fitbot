import io
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api.bookings as bookings_api


def config():
    return {
        "timezone": "Europe/Madrid",
        "box_name": "studiocrossfit",
        "box_id": 9507,
        "window_minutes": 30,
        "booking_policy": {
            "opening_lead_hours": 72,
            "horizon": {
                "type": "calendar_month_plus_days",
                "next_month_days": 3,
            },
        },
        "members": {
            "person_1": {"label": "Persona 1"},
            "person_2": {"label": "Persona 2"},
        },
        "activities": {
            "class": [
                "Weightlifting",
                "Strength Conditioning",
                "CrossFit",
                "Fuerza",
                "Metcon",
                "Gymnastic",
                "Handstand & Flex",
            ],
            "open_box": ["Open Box"],
        },
    }


NOW = datetime(2026, 11, 7, 12, 0, tzinfo=ZoneInfo("Europe/Madrid"))
VALID_PAYLOAD = {
    "person_id": "person_1",
    "date": "2026-11-10",
    "time": "13:00",
    "name": "Strength Conditioning",
}


def test_build_booking_calculates_exact_72_hour_opening():
    booking = bookings_api._build_booking(VALID_PAYLOAD, config(), now=NOW)

    assert booking["person_id"] == "person_1"
    assert booking["activity_type"] == "class"
    assert booking["opens_at"] == "2026-11-07T13:00:00+01:00"
    assert booking["status"] == "pending_schedule"
    assert booking["id"]


def test_build_booking_classifies_open_box_separately():
    payload = dict(VALID_PAYLOAD, name="Open Box")
    booking = bookings_api._build_booking(payload, config(), now=NOW)

    assert booking["activity_type"] == "open_box"


@pytest.mark.parametrize(
    "payload",
    [
        dict(VALID_PAYLOAD, person_id="person_3"),
        dict(VALID_PAYLOAD, name="Yoga"),
        dict(VALID_PAYLOAD, extra="not-allowed"),
        dict(VALID_PAYLOAD, time="9:00"),
        dict(VALID_PAYLOAD, date="2026-12-04"),
        dict(VALID_PAYLOAD, date="2026-11-06"),
    ],
)
def test_build_booking_rejects_invalid_or_out_of_horizon_requests(payload):
    with pytest.raises(ValueError):
        bookings_api._build_booking(payload, config(), now=NOW)


def test_build_booking_uses_elapsed_utc_hours_across_dst():
    dst_now = datetime(2026, 3, 27, 11, 0, tzinfo=ZoneInfo("Europe/Madrid"))
    payload = {
        "person_id": "person_2",
        "date": "2026-03-30",
        "time": "13:00",
        "name": "Open Box",
    }
    booking = bookings_api._build_booking(payload, config(), now=dst_now)

    opening = datetime.fromisoformat(booking["opens_at"])
    class_start = datetime.fromisoformat("2026-03-30T13:00:00+02:00")
    class_start_utc = class_start.astimezone(ZoneInfo("UTC"))
    opening_utc = opening.astimezone(ZoneInfo("UTC"))
    elapsed = (class_start_utc - opening_utc).total_seconds()
    assert elapsed == 72 * 3600


def test_create_booking_persists_then_publishes_only_booking_id(monkeypatch):
    stored = {}
    calls = {}

    class FakeRedis:
        def create_booking_idempotent(self, record, idempotency_key):
            assert idempotency_key == "client-request-0001"
            stored.update(record)
            return True, record["id"]

        def update_booking_status(
            self, booking_id, status, expected_statuses, metadata=None
        ):
            assert booking_id == stored["id"]
            if status == "scheduling":
                assert expected_statuses == ["pending_schedule", "schedule_failed"]
                stored["status"] = status
                return dict(stored)
            assert status == "scheduled"
            stored.update(metadata)
            stored["status"] = status
            return dict(stored)

        def get_booking(self, booking_id):
            return dict(stored)

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"messageId": "qstash-test-message"}

    def fake_post(url, **kwargs):
        calls["url"] = url
        calls.update(kwargs)
        return FakeResponse()

    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("QSTASH_TOKEN", "qstash-test-token")
    monkeypatch.setenv("FITBOT_WEBHOOK_URL", "https://preview.example/api/book")
    monkeypatch.setenv("VERCEL_AUTOMATION_BYPASS_SECRET", "preview-bypass")
    monkeypatch.setattr(bookings_api, "load_config", lambda path: config())
    monkeypatch.setattr(bookings_api, "UpstashRedis", FakeRedis)
    monkeypatch.setattr(bookings_api.requests, "post", fake_post)

    payload = dict(VALID_PAYLOAD, date="2026-11-11")
    result, published = bookings_api._create_booking(payload, "client-request-0001")

    assert published is True
    assert result["status"] == "scheduled"
    assert result["message_id"] == "qstash-test-message"
    assert calls["json"] == {"booking_id": stored["id"]}
    assert calls["headers"]["Upstash-Not-Before"] == str(
        int(datetime.fromisoformat(stored["opens_at"]).timestamp())
    )
    assert (
        calls["headers"]["Upstash-Forward-x-vercel-protection-bypass"]
        == "preview-bypass"
    )


def test_idempotent_repeat_of_scheduled_booking_does_not_publish_again(monkeypatch):
    record = {
        "id": "a" * 32,
        "person_id": "person_1",
        "date": "2026-11-11",
        "time": "13:00",
        "name": "Strength Conditioning",
        "activity_type": "class",
        "opens_at": "2026-11-08T13:00:00+01:00",
        "status": "scheduled",
        "created_at": NOW.isoformat(),
        "message_id": "existing-message",
    }

    class FakeRedis:
        def create_booking_idempotent(self, booking, key):
            return False, record["id"]

        def get_booking(self, booking_id):
            return dict(record)

    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("QSTASH_TOKEN", "qstash-test-token")
    monkeypatch.setenv("FITBOT_WEBHOOK_URL", "https://preview.example/api/book")
    monkeypatch.setenv("VERCEL_AUTOMATION_BYPASS_SECRET", "preview-bypass")
    monkeypatch.setattr(bookings_api, "load_config", lambda path: config())
    monkeypatch.setattr(bookings_api, "UpstashRedis", FakeRedis)
    monkeypatch.setattr(
        bookings_api.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("duplicate must not publish"),
    )

    payload = dict(VALID_PAYLOAD, date="2026-11-11")
    result, published = bookings_api._create_booking(payload, "client-request-0001")

    assert published is False
    assert result["status"] == "scheduled"
    assert result["message_id"] == "existing-message"


class FakeHandler:
    def __init__(
        self,
        payload,
        token="admin-test",
        idempotency_key="client-request-0001",
    ):

        raw = json.dumps(payload).encode()
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Length": str(len(raw)),
            "Idempotency-Key": idempotency_key,
        }
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def post(self):
        bookings_api.handler.do_POST(self)
        return json.loads(self.wfile.getvalue())


def test_endpoint_requires_admin_authentication(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-test")
    monkeypatch.setenv("VERCEL_ENV", "preview")
    request = FakeHandler(VALID_PAYLOAD, token="wrong")

    response = request.post()

    assert request.status == 401
    assert response["error"] == "Unauthorized"


def test_endpoint_rejects_missing_idempotency_key(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-test")
    monkeypatch.setenv("VERCEL_ENV", "preview")
    request = FakeHandler(VALID_PAYLOAD, idempotency_key="short")

    response = request.post()

    assert request.status == 400
    assert "Idempotency-Key" in response["error"]
