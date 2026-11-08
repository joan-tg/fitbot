import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api.results as results_api


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

    def delete(self):
        results_api.handler.do_DELETE(self)
        return json.loads(self.wfile.getvalue())


def test_delete_results_requires_admin_authentication(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-test")
    request = FakeHandler(
        {"recorded_at": ["2026-11-07T08:00:08.140428+00:00"]},
        token="wrong",
    )

    response = request.delete()

    assert request.status == 401
    assert response == {"error": "Unauthorized"}


def test_delete_results_accepts_only_timezone_aware_timestamps(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-test")
    deleted_timestamps = []

    class FakeRedis:
        def delete_confirmed_booking_results(self, timestamps):
            deleted_timestamps.extend(timestamps)
            return 2

    monkeypatch.setattr(results_api, "UpstashRedis", FakeRedis)
    request = FakeHandler(
        {
            "recorded_at": [
                "2026-11-07T08:00:08.140428+00:00",
                "2026-11-06T17:15:02.501123+00:00",
            ]
        }
    )

    response = request.delete()

    assert request.status == 200
    assert response == {"deleted": 2}
    assert deleted_timestamps == [
        "2026-11-07T08:00:08.140428+00:00",
        "2026-11-06T17:15:02.501123+00:00",
    ]


def test_delete_results_rejects_timestamp_without_timezone(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-test")
    request = FakeHandler({"recorded_at": ["2026-11-07T08:00:08"]})

    response = request.delete()

    assert request.status == 400
    assert "timezone" in response["error"]
