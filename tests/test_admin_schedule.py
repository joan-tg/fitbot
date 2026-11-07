import io
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api.schedule as schedule_api


class FakeHandler:
    def __init__(self, payload, token="admin-test"):
        raw = json.dumps(payload).encode()
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Length": str(len(raw)),
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
        schedule_api.handler.do_POST(self)
        return json.loads(self.wfile.getvalue())


def test_admin_schedule_rejects_missing_or_wrong_token(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-test")
    request = FakeHandler({}, token="wrong")
    schedule_api.handler.do_POST(request)
    assert request.status == 401


def test_schedule_rejects_local_environment(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-test")
    monkeypatch.setenv("VERCEL_ENV", "development")
    request = FakeHandler({})
    response = request.post()
    assert request.status == 403
    assert response["error"] == "Vercel Preview or Production environment only"


def test_schedule_allows_production_with_admin_token(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-test")
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setattr(
        schedule_api,
        "_schedule_booking",
        lambda payload: {"status": "scheduled"},
    )
    request = FakeHandler({"date": "2099-01-01"})
    response = request.post()
    assert request.status == 201
    assert response["status"] == "scheduled"


def test_schedule_allows_live_mode_in_preview(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-test")
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setattr(
        schedule_api,
        "_schedule_booking",
        lambda payload: {"status": "scheduled"},
    )
    request = FakeHandler({"date": "2099-01-01"})
    response = request.post()
    assert request.status == 201


def test_schedule_rejects_unknown_booking(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-test")
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setattr(
        schedule_api,
        "_schedule_booking",
        lambda payload: (_ for _ in ()).throw(ValueError("unknown")),
    )
    request = FakeHandler({"date": "2099-01-01"})
    response = request.post()
    assert request.status == 400
    assert response["error"] == "unknown"

def test_production_schedule_does_not_require_preview_bypass(monkeypatch):
    timezone = ZoneInfo("Europe/Madrid")
    opens_at = (
        datetime.now(timezone) + timedelta(hours=1)
    ).replace(tzinfo=None).isoformat(timespec="seconds")
    booking = {
        "date": "2099-01-01",
        "opens_at": opens_at,
        "time": "18:15",
        "name": "Strength Conditioning",
    }
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("QSTASH_TOKEN", "qstash-test")
    monkeypatch.setenv("FITBOT_WEBHOOK_URL", "https://fitbot.example.com/api/book")
    monkeypatch.delenv("VERCEL_AUTOMATION_BYPASS_SECRET", raising=False)
    monkeypatch.setattr(
        schedule_api,
        "load_config",
        lambda path: {"timezone": "Europe/Madrid", "bookings": [booking]},
    )

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"messageId": "qstash-message-id"}

    published = {}

    def fake_post(url, **kwargs):
        published["url"] = url
        published.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(schedule_api.requests, "post", fake_post)

    result = schedule_api._schedule_booking(booking)

    assert result["status"] == "scheduled"
    assert result["execution_mode"] == "determined_when_webhook_runs"
    assert published["url"].endswith("/https://fitbot.example.com/api/book")
    assert (
        "Upstash-Forward-x-vercel-protection-bypass"
        not in published["headers"]
    )
