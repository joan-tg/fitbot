"""Tests for explicit AimHarder monthly credit accounting."""

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api.monthly_credits as credits


class FakeHandler:
    def __init__(self, payload=None, token="admin"):
        self.body = json.dumps(payload or {}).encode("utf-8")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Length": str(len(self.body)),
        }
        self.rfile = io.BytesIO(self.body)
        self.wfile = io.BytesIO()
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        pass

    def end_headers(self):
        pass

    def _authorized(self):
        return credits.handler._authorized(self)

    def _respond(self, status, payload):
        credits.handler._respond(self, status, payload)

    def call(self, method):
        getattr(credits.handler, method)(self)
        return json.loads(self.wfile.getvalue())


def _counts():
    return {
        "person_1": {"classes": 8, "open_box": 2},
        "person_2": {"classes": 5, "open_box": 1},
    }


class FakeStore:
    def __init__(self):
        self.data = {}
        self.calls = []

    def _command(self, command):
        self.calls.append(command)
        if command[0] == "GET":
            return self.data.get(command[1])
        if command[0] == "SET":
            self.data[command[1]] = command[2]
            return "OK"
        raise AssertionError("Unexpected Redis operation")


def test_rejects_untrusted_counts():
    for bad in (
        {"person_1": {"classes": True, "open_box": 0}},
        {"person_1": {"classes": 2, "open_box": 0}, "person_2": {"classes": -1, "open_box": 0}},
    ):
        with pytest.raises(ValueError):
            credits._validate_counts(bad)


def test_get_and_save_manual_counts(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin")
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setattr(credits, "_current_month", lambda: "2026-11")
    store = FakeStore()
    monkeypatch.setattr(credits, "UpstashRedis", lambda: store)

    before = FakeHandler()
    data = before.call("do_GET")
    assert before.status == 200
    assert data["counts"] is None

    submitted = FakeHandler({"month": "2026-11", "counts": _counts()})
    result = submitted.call("do_POST")
    assert submitted.status == 200
    assert result["counts"] == _counts()

    after = FakeHandler()
    data = after.call("do_GET")
    assert data["counts"] == _counts()
    assert data["source"] == "manual_aimharder"
    assert [c[0] for c in store.calls] == ["GET", "SET", "GET"]


def test_preview_cannot_write_even_with_admin(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin")
    monkeypatch.setenv("VERCEL_ENV", "preview")
    store = FakeStore()
    monkeypatch.setattr(credits, "UpstashRedis", lambda: store)
    request = FakeHandler({"month": "2026-11", "counts": _counts()})
    assert request.call("do_POST") == {"error": "Production only"}
    assert request.status == 403
    assert store.calls == []


def test_wrong_month_and_auth_block_writes(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin")
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setattr(credits, "_current_month", lambda: "2026-11")
    store = FakeStore()
    monkeypatch.setattr(credits, "UpstashRedis", lambda: store)
    wrong_month = FakeHandler({"month": "2026-10", "counts": _counts()})
    assert wrong_month.call("do_POST")["error"] == "Month changed; reload before saving"
    unauthorized = FakeHandler({"month": "2026-11", "counts": _counts()}, token="wrong")
    assert unauthorized.call("do_POST") == {"error": "Unauthorized"}
    assert store.calls == []


def test_zero_is_valid_but_missing_is_unknown():
    counts = _counts()
    counts["person_1"]["classes"] = 0
    counts["person_2"]["open_box"] = None
    credits._validate_counts(counts)
