import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api.reconcile as reconcile_api

TZ = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=TZ)


class FakeRedis:
    def __init__(self, records):
        self.records = {record["id"]: dict(record) for record in records}
        self.updates = []
        self.history = []

    def list_bookings(self, limit):
        assert limit == 500
        return [dict(record) for record in self.records.values()]

    def update_booking_status(
        self, booking_id, status, expected_statuses=None, metadata=None
    ):
        record = self.records[booking_id]
        if expected_statuses and record["status"] not in expected_statuses:
            return None
        record.update(metadata or {})
        record["status"] = status
        record["updated_at"] = NOW.isoformat()
        self.updates.append((booking_id, status, list(expected_statuses or [])))
        return dict(record)

    def record_booking_result(self, record):
        self.history.append(dict(record))


def booking(status, updated_at=None, opens_at=None, booking_id=None):
    return {
        "id": booking_id or ("a" * 32),
        "person_id": "person_1",
        "date": "2026-11-12",
        "time": "12:00",
        "name": "Strength Conditioning",
        "activity_type": "class",
        "opens_at": opens_at or (NOW + timedelta(hours=2)).isoformat(),
        "status": status,
        "created_at": (NOW - timedelta(minutes=10)).isoformat(),
        "updated_at": updated_at or (NOW - timedelta(minutes=10)).isoformat(),
    }


def test_stale_scheduling_record_is_republished_once(monkeypatch):
    record = booking("scheduling", NOW - timedelta(minutes=3))
    redis = FakeRedis([record])
    published = []

    def fake_publish(item, store):
        assert store is redis
        assert item["status"] == "schedule_failed"
        published.append(item["id"])
        return dict(item, status="scheduled"), True

    monkeypatch.setattr(reconcile_api, "_publish_booking", fake_publish)
    counts = reconcile_api._reconcile(
        redis, [record], NOW, TZ, 30, True
    )

    assert counts["republished"] == 1
    assert published == [record["id"]]
    assert redis.updates == [
        (record["id"], "schedule_failed", ["scheduling"])
    ]


def test_recent_scheduling_record_is_left_alone(monkeypatch):
    record = booking("scheduling", NOW - timedelta(seconds=20))
    redis = FakeRedis([record])
    monkeypatch.setattr(
        reconcile_api,
        "_publish_booking",
        lambda *args: pytest.fail("recent scheduling must not be republished"),
    )

    counts = reconcile_api._reconcile(redis, [record], NOW, TZ, 30, True)

    assert counts["republished"] == 0
    assert counts["skipped"] == 0
    assert redis.updates == []


def test_expired_scheduled_booking_is_marked_missed(monkeypatch):
    record = booking(
        "scheduled",
        NOW - timedelta(minutes=40),
        NOW - timedelta(minutes=31),
    )
    redis = FakeRedis([record])

    counts = reconcile_api._reconcile(redis, [record], NOW, TZ, 30, True)

    assert counts["missed"] == 1
    assert redis.records[record["id"]]["status"] == "booking_missed"
    assert redis.records[record["id"]]["error_code"] == "execution_window_expired"
    assert redis.history[0]["status"] == "booking_missed"


def test_stale_processing_becomes_uncertain_but_is_never_retried(monkeypatch):
    record = booking("processing", NOW - timedelta(minutes=8))
    redis = FakeRedis([record])
    monkeypatch.setattr(
        reconcile_api,
        "_publish_booking",
        lambda *args: pytest.fail("processing must never be republished"),
    )

    counts = reconcile_api._reconcile(redis, [record], NOW, TZ, 30, False)

    assert counts["outcome_unconfirmed"] == 1
    assert redis.records[record["id"]]["status"] == "outcome_unconfirmed"
    assert redis.history[0]["error_code"] == "stale_processing_recovered"


def test_schedule_failed_is_republished_only_after_recovery_delay(monkeypatch):
    record = booking("schedule_failed", NOW - timedelta(minutes=2))
    redis = FakeRedis([record])
    calls = []
    monkeypatch.setattr(
        reconcile_api,
        "_publish_booking",
        lambda item, store: (calls.append(item["id"]) or (item, True)),
    )

    counts = reconcile_api._reconcile(redis, [record], NOW, TZ, 30, True)

    assert counts["republished"] == 1
    assert calls == [record["id"]]


class FakeHandler:
    def __init__(self, token="admin-token"):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.wfile = __import__("io").BytesIO()
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        pass

    def end_headers(self):
        pass

    def get(self):
        reconcile_api.handler.do_GET(self)
        return json.loads(self.wfile.getvalue())


def test_reconcile_endpoint_accepts_cron_secret(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-token")
    monkeypatch.setenv("VERCEL_ENV", "preview")
    request = FakeHandler(token="cron-token")
    monkeypatch.setattr(reconcile_api, "load_config", lambda path: {
        "timezone": "Europe/Madrid",
        "window_minutes": 30,
    })
    monkeypatch.setattr(reconcile_api, "UpstashRedis", lambda: FakeRedis([]))

    response = request.get()

    assert request.status == 200
    assert response["status"] == "completed"
    assert response["scanned"] == 0


def test_reconcile_endpoint_rejects_unauthorized_request(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin-token")
    monkeypatch.setenv("VERCEL_ENV", "preview")
    request = FakeHandler(token="wrong-token")

    response = request.get()

    assert request.status == 401
    assert response["error"] == "Unauthorized"
