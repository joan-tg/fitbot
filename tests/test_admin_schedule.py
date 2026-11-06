import io
import json
import sys
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


def test_schedule_requires_preview_and_dry_run(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-test")
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("DRY_RUN", "true")
    request = FakeHandler({})
    response = request.post()
    assert request.status == 403
    assert response["error"] == "Preview environment only"


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
