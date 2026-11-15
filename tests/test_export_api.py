import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api.export as export_api


class FakeHandler:
    def __init__(self, token="admin-test"):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.wfile = io.BytesIO()
        self.status = None
        self.headers_sent = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers_sent[key] = value

    def end_headers(self):
        pass

    def get(self):
        export_api.handler.do_GET(self)
        return json.loads(self.wfile.getvalue())


def test_export_requires_admin_authentication(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-test")
    request = FakeHandler(token="wrong")

    assert request.get() == {"error": "Unauthorized"}
    assert request.status == 401


def test_export_is_read_only_and_includes_full_bounded_stores(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-test")
    monkeypatch.setenv("VERCEL_ENV", "production")

    class FakeRedis:
        BOOKINGS_MAX_ITEMS = 500
        RESULTS_MAX_ITEMS = 100

        def list_bookings(self, limit):
            assert limit == 500
            return [{"id": "booking-1", "status": "dry_run"}]

        def list_booking_results(self, limit):
            assert limit == 100
            return [{"status": "booking_confirmed", "dry_run": True}]

    monkeypatch.setattr(export_api, "UpstashRedis", FakeRedis)
    request = FakeHandler()

    response = request.get()

    assert request.status == 200
    assert request.headers_sent["Cache-Control"] == "no-store"
    assert response["read_only"] is True
    assert response["bookings_count"] == 1
    assert response["results_count"] == 1
    assert response["bookings"][0]["id"] == "booking-1"
    assert "QStash schedules are not included" in response["notes"][1]


def test_export_rejects_non_vercel_environment(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-test")
    monkeypatch.setenv("VERCEL_ENV", "development")
    request = FakeHandler()

    assert request.get() == {"error": "Vercel Preview or Production environment only"}
    assert request.status == 403
