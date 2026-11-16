"""Safety tests for the narrowly scoped November 2026 cleanup."""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api.cleanup_tests as api


class FakeHandler:
    def __init__(self, token="admin", body=None):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.wfile = io.BytesIO()
        self.status = None
        self.body = json.dumps(body or {}).encode("utf-8")
        self.rfile = io.BytesIO(self.body)
        self.headers["Content-Length"] = str(len(self.body))

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def _respond(self, code, payload):
        api.handler._respond(self, code, payload)

    def _authorized(self):
        return api.handler._authorized(self)

    def get(self):
        api.handler.do_GET(self)
        return json.loads(self.wfile.getvalue())

    def post(self):
        api.handler.do_POST(self)
        return json.loads(self.wfile.getvalue())


class FakeRedis:
    BOOKINGS_INDEX_KEY = "fitbot:bookings:index"
    RESULTS_KEY = "fitbot:booking_results"

    def __init__(self):
        self.calls = []
        self.bookings = {
            api.PRESERVED_ID: {
                "id": api.PRESERVED_ID,
                "date": "2026-11-16",
                "time": "13:15",
                "status": "cancelled",
                "error_code": "cancelled_in_aimharder",
            }
        }
        self.bookings.update(
            {
                key: {
                    "id": key,
                    "status": status,
                    "date": "2026-11-13",
                }
                for key, status in api.TEST_STATUSES.items()
            }
        )
        self.ids = sorted(self.bookings)
        self.history = [
            {
                "booking_id": api.PRESERVED_ID,
                "status": "booking_confirmed",
                "dry_run": False,
            }
        ] + [
            {
                "booking_id": key,
                "status": "dry_run",
                "dry_run": True,
            }
            for key in sorted(api.EXPECTED_RESULT_IDS)
        ]

    @staticmethod
    def booking_record_key(booking_id):
        return "fitbot:bookings:" + booking_id

    def _command(self, command):
        self.calls.append(command)
        if command[0] == "ZRANGE":
            return list(self.ids)
        if command[0] == "LRANGE":
            return [json.dumps(entry) for entry in self.history]
        if command[0] == "MGET":
            return [
                json.dumps(self.bookings[key.split(":")[-1]])
                for key in command[1:]
            ]
        if command[0] == "EVAL":
            return [9, 5]
        raise AssertionError("Unexpected Redis operation")


def test_preflight_read_only_and_preserves_real_booking(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin")
    monkeypatch.setenv("VERCEL_ENV", "production")
    store = FakeRedis()
    monkeypatch.setattr(api, "UpstashRedis", lambda: store)
    request = FakeHandler()
    result = request.get()
    assert request.status == 200
    assert result["ready"] is True
    assert result["expected_removed_bookings"] == 9
    assert result["expected_removed_results"] == 5
    assert result["preserved_real_booking_id"] == api.PRESERVED_ID
    assert [call[0] for call in store.calls] == ["ZRANGE", "LRANGE", "MGET"]


def test_cleanup_requires_confirmation_and_backup(monkeypatch):
    monkeypatch.setenv("FITBOT_ADMIN_TOKEN", "admin")
    monkeypatch.setenv("VERCEL_ENV", "production")
    request = FakeHandler(body={"confirm_cleanup": True})
    assert request.post()["error"].startswith("Confirmation")
    assert request.status == 409


def test_cleanup_snapshot_mismatch_never_writes(monkeypatch):
    store = FakeRedis()
    _, _, _, token = api._snapshot(store)
    store.bookings[next(iter(api.TEST_STATUSES))]["status"] = "scheduled"
    try:
        api._cleanup(store, token)
        assert False, "Changed record must block cleanup"
    except ValueError:
        pass
    assert "EVAL" not in [call[0] for call in store.calls]


def test_cleanup_passes_exact_expected_raw_values_to_atomic_script():
    store = FakeRedis()
    _, _, _, token = api._snapshot(store)
    result = api._cleanup(store, token)
    assert result == {"removed_bookings": 9, "removed_simulation_results": 5}
    eval_call = store.calls[-1]
    assert eval_call[0] == "EVAL"
    assert eval_call[2] == 12
    assert api.PRESERVED_ID not in json.loads(eval_call[-2])
    assert sum(json.loads(eval_call[-1])) == 5


def test_unexpected_new_booking_blocks_preview():
    store = FakeRedis()
    store.bookings["00000000000000000000000000000000"] = {
        "id": "00000000000000000000000000000000",
        "status": "scheduled",
    }
    store.ids.append("00000000000000000000000000000000")
    store.ids.sort()
    try:
        api._snapshot(store)
        assert False, "Extra booking must block cleanup"
    except ValueError:
        pass
