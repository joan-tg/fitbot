"""Tests for the read-only known-message QStash lookup."""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api.qstash_messages as api


class FakeHandler:
    def __init__(self, token="admin"):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.wfile = io.BytesIO()
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def _respond(self, status, payload):
        api.handler._respond(self, status, payload)

    def get(self):
        api.handler.do_GET(self)
        return json.loads(self.wfile.getvalue())


def test_requires_admin_and_production(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin")
    monkeypatch.setenv("VERCEL_ENV", "production")
    assert FakeHandler(token="wrong").get() == {"error": "Unauthorized"}
    monkeypatch.setenv("VERCEL_ENV", "preview")
    assert FakeHandler().get() == {"error": "Production only"}


def test_known_messages_only_and_sensitive_details_excluded(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin")
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("QSTASH_TOKEN", "private-secret")

    class FakeRedis:
        BOOKINGS_MAX_ITEMS = 500

        def list_bookings(self, limit):
            assert limit == 500
            return [
                {
                    "id": "b1",
                    "message_id": "msg_ABC123",
                    "status": "cancelled",
                    "date": "2026-11-16",
                    "time": "13:15",
                },
                {"id": "b2", "message_id": "msg_XYZ999", "status": "dry_run"},
                {"id": "b3", "message_id": "msg_BAD/../../private"},
                {"id": "b4", "message_id": "msg_ABC123"},
            ]

    class Response:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self.payload = payload

        def json(self):
            return self.payload

    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        assert kwargs["headers"]["Authorization"] == "Bearer private-secret"
        if url.endswith("msg_ABC123"):
            return Response(
                200,
                {
                    "messageId": "msg_ABC123",
                    "notBefore": 1794852300000,
                    "body": "sensitive",
                    "header": {"Authorization": "secret"},
                    "url": "https://example.com/?secret=foo",
                },
            )
        return Response(404)

    monkeypatch.setattr(api, "UpstashRedis", FakeRedis)
    monkeypatch.setattr(api.requests, "get", fake_get)
    handler = FakeHandler()
    data = handler.get()
    assert handler.status == 200
    assert data["read_only"] is True
    assert data["checked_count"] == 2
    assert data["invalid_message_ids"] == 1
    assert [x["lookup"] for x in data["message_checks"]] == [
        "present",
        "not_found",
    ]
    assert len(calls) == 2
    assert "sensitive" not in json.dumps(data)
    assert "private-secret" not in json.dumps(data)
    assert "example.com" not in json.dumps(data)


def test_qstash_errors_do_not_infer_delivery(monkeypatch):
    class Response:
        status_code = 503

    monkeypatch.setattr(api.requests, "get", lambda *args, **kwargs: Response())
    result = api._lookup_one({"id": "b1", "message_id": "msg_ABC123"}, "token")
    assert result["lookup"] == "unavailable"
