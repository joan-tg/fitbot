"""Read-only QStash inventory endpoint tests."""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api.qstash_inventory as inventory


class FakeHandler:
    def __init__(self, token="test-admin"):
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
        inventory.handler._respond(self, status, payload)

    def get(self):
        inventory.handler.do_GET(self)
        return json.loads(self.wfile.getvalue())


def test_rejects_unauthorized(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "test-admin")
    assert FakeHandler(token="wrong").get() == {"error": "Unauthorized"}


def test_rejects_preview(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("VERCEL_ENV", "preview")
    request = FakeHandler()
    assert request.get() == {"error": "Production only"}
    assert request.status == 403


def test_read_only_and_sanitized(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("QSTASH_TOKEN", "private-secret")
    calls = []

    class Response:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/logs") and kwargs["params"].get("cursor") == "next":
            return Response({"logs": [{"messageId": "msg-2", "state": "DELIVERED"}]})
        if url.endswith("/logs"):
            return Response(
                {
                    "cursor": "next",
                    "logs": [
                        {
                            "messageId": "msg-1",
                            "state": "CREATED",
                            "url": "https://fitbot-theta.vercel.app/api/book?token=secret",
                            "header": {"Authorization": "secret"},
                            "body": "sensitive",
                        }
                    ],
                }
            )
        return Response(
            [
                {
                    "scheduleId": "sch-1",
                    "cron": "* * * * *",
                    "destination": "https://fitbot-git-somebranch-joan-tgs-projects.vercel.app/api/reconcile?secret=yes",
                    "header": {"Authorization": "secret"},
                    "body": "sensitive",
                }
            ]
        )

    monkeypatch.setattr(inventory.requests, "get", fake_get)
    request = FakeHandler()
    result = request.get()
    assert request.status == 200
    assert result["read_only"] is True
    assert result["has_more_logs"] is False
    assert result["log_pages_scanned"] == 2
    assert len(result["recent_logs"]) == 2
    assert result["recurring_schedules"][0]["destination"] == {
        "environment": "preview",
        "endpoint": "/api/reconcile",
    }
    assert result["recent_logs"][0]["destination"] == {
        "environment": "production",
        "endpoint": "/api/book",
    }
    assert result["recent_logs"][0]["message_id"] == "msg-1"
    assert result["recurring_schedules"][0]["schedule_id"] == "sch-1"
    assert "secret" not in json.dumps(result)
    assert all(url.startswith("https://qstash.upstash.io/v2/") for url, _ in calls)
    assert all(
        kwargs["headers"]["Authorization"] == "Bearer private-secret"
        for _, kwargs in calls
    )


def test_unknown_destinations_are_not_leaked():
    assert inventory._destination_info("https://unknown.example.com/secret?token=x") == {
        "environment": "unknown",
        "endpoint": "other",
    }
