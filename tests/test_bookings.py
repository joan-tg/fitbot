import io
import json

import pytest

from api.bookings import handler


class FakeHandler:
    def __init__(self, path="/api/bookings", token="admin-secret"):
        self.path = path
        self.headers = {"Authorization": f"Bearer {token}" if token else ""}
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def get(self):
        handler.do_GET(self)
        return json.loads(self.wfile.getvalue())


def test_get_bookings_requires_admin_token(monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setattr(
        "api.bookings.UpstashRedis",
        lambda: pytest.fail("Redis must not be touched without authorization"),
    )

    request = FakeHandler(token="wrong-token")
    response = request.get()

    assert request.status == 401
    assert response == {"error": "Unauthorized"}


def test_get_bookings_returns_only_allowlisted_operational_fields(monkeypatch):
    record = {
        "id": "a" * 32,
        "person_id": "person_1",
        "date": "2026-11-11",
        "time": "08:00",
        "name": "Strength Conditioning",
        "activity_type": "class",
        "status": "scheduled",
        "created_at": "2026-11-07T23:00:00+01:00",
        "updated_at": "2026-11-07T23:01:00+01:00",
        "opens_at": "2026-11-08T08:00:00+01:00",
        "message_id": "qstash-message-id",
        "scheduled_at": "2026-11-08T08:00:00+01:00",
        "error_code": None,
        "email": "must-not-leak@example.com",
        "password": "must-not-leak",
        "idempotency_key": "must-not-leak",
    }
    calls = []

    class FakeRedis:
        def list_bookings(self, limit):
            calls.append(limit)
            return [record]

    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setattr("api.bookings.UpstashRedis", FakeRedis)

    request = FakeHandler(path="/api/bookings?limit=12")
    response = request.get()

    assert request.status == 200
    assert calls == [12]
    assert response["count"] == 1
    assert response["bookings"] == [
        {
            "id": "a" * 32,
            "person_id": "person_1",
            "date": "2026-11-11",
            "time": "08:00",
            "name": "Strength Conditioning",
            "activity_type": "class",
            "status": "scheduled",
            "created_at": "2026-11-07T23:00:00+01:00",
            "updated_at": "2026-11-07T23:01:00+01:00",
            "opens_at": "2026-11-08T08:00:00+01:00",
            "message_id": "qstash-message-id",
            "scheduled_at": "2026-11-08T08:00:00+01:00",
            "error_code": None,
        }
    ]
    serialized = json.dumps(response)
    assert "must-not-leak" not in serialized
    assert "example.com" not in serialized
    assert request.response_headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "path",
    [
        "/api/bookings?limit=0",
        "/api/bookings?limit=101",
        "/api/bookings?limit=abc",
        "/api/bookings?limit=10&limit=20",
        "/api/bookings?status=processing",
    ],
)
def test_get_bookings_rejects_invalid_query_parameters(monkeypatch, path):
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setattr(
        "api.bookings.UpstashRedis",
        lambda: pytest.fail("Redis must not be touched for invalid query parameters"),
    )

    request = FakeHandler(path=path)
    response = request.get()

    assert request.status == 400
    assert "error" in response


def test_get_bookings_is_limited_to_vercel_environments(monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "development")
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setattr(
        "api.bookings.UpstashRedis",
        lambda: pytest.fail("Redis must not be touched outside Vercel"),
    )

    request = FakeHandler()
    response = request.get()

    assert request.status == 403
    assert response == {"error": "Vercel Preview or Production environment only"}


def test_get_bookings_returns_service_unavailable_when_redis_fails(monkeypatch):
    from redis_store import RedisUnavailable

    def fail_redis():
        raise RedisUnavailable("private connection details")

    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setattr("api.bookings.UpstashRedis", fail_redis)

    request = FakeHandler()
    response = request.get()

    assert request.status == 503
    assert response == {"error": "Booking store unavailable"}
