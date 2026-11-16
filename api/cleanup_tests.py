"""One-time, narrowly scoped cleanup of November 2026 FitBot test records.

Only the exact known historical test records may be removed. A live snapshot
fingerprint, explicit confirmation, and atomic Redis CAS protect other data.
"""

import hashlib
import hmac
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from redis_store import RedisUnavailable, UpstashRedis  # noqa: E402

logger = logging.getLogger("fitbot.cleanup_tests")
MAX_BODY_BYTES = 512
PRESERVED_ID = "b5d9810563494f4aa402ac59b015802a"
TEST_STATUSES = {
    "5ee5642ffe26451185338858fe873253": "dry_run",
    "85f5d313baed48a5957300bda7381a0b": "dry_run",
    "2213d42ce60146f39443aa0953672671": "replaced",
    "7c93555d7c6f4f9086bf2255db7eb7f8": "cancelled",
    "c18599973ee64bda823029710fe86ec4": "cancelled",
    "a74be42cbdcc408084c51d7367db43b9": "cancelled",
    "ea3c86d95d0041fb98af42eaebf9bd71": "dry_run",
    "f4c73e1e9dcc4b509fe217d7bac93ffe": "dry_run",
    "b049e571ec224ee1bd522f0e356a564a": "dry_run",
}
EXPECTED_RESULT_IDS = set(TEST_STATUSES) - {
    "2213d42ce60146f39443aa0953672671",
    "7c93555d7c6f4f9086bf2255db7eb7f8",
    "c18599973ee64bda823029710fe86ec4",
    "a74be42cbdcc408084c51d7367db43b9",
}
# Four of five dry-run entries are included above plus the 12:15 Open Box entry.


def _snapshot(store):
    ids = store._command(["ZRANGE", store.BOOKINGS_INDEX_KEY, 0, -1]) or []
    results_raw = store._command(["LRANGE", store.RESULTS_KEY, 0, -1]) or []
    if not isinstance(ids, list) or not isinstance(results_raw, list):
        raise RedisUnavailable("Unexpected inventory")
    raw = (
        store._command(
            ["MGET", *(store.booking_record_key(booking_id) for booking_id in ids)]
        )
        if ids
        else []
    )
    if not isinstance(raw, list) or len(raw) != len(ids) or any(v is None for v in raw):
        raise RedisUnavailable("Incomplete inventory")
    try:
        bookings = [json.loads(value) for value in raw]
        results = [json.loads(value) for value in results_raw]
    except (TypeError, ValueError) as error:
        raise RedisUnavailable("Invalid inventory JSON") from error
    expected_ids = set(TEST_STATUSES) | {PRESERVED_ID}
    if (
        len(ids) != 10
        or set(ids) != expected_ids
        or len(results) != 6
        or len(raw) != 10
        or any(not isinstance(x, dict) for x in bookings + results)
    ):
        raise ValueError(
            "Live inventory differs from the agreed ten bookings and six results"
        )
    by_id = {b.get("id"): b for b in bookings}
    if len(by_id) != 10 or by_id[PRESERVED_ID].get("status") != "cancelled":
        raise ValueError("Preserved real cancellation is missing or changed")
    if (
        by_id[PRESERVED_ID].get("error_code") != "cancelled_in_aimharder"
        or by_id[PRESERVED_ID].get("date") != "2026-11-16"
        or by_id[PRESERVED_ID].get("time") != "13:15"
    ):
        raise ValueError("Preserved booking no longer matches known real cancellation")
    for booking_id, status in TEST_STATUSES.items():
        booking = by_id.get(booking_id)
        if (
            booking is None
            or booking.get("status") != status
            or booking.get("date", "") > "2026-11-16"
        ):
            raise ValueError("A test booking changed; refusing cleanup")
    confirmed = [
        r
        for r in results
        if r.get("booking_id") == PRESERVED_ID
        and r.get("status") == "booking_confirmed"
        and r.get("dry_run") is False
    ]
    simulation = [
        r
        for r in results
        if r.get("booking_id") in EXPECTED_RESULT_IDS
        and r.get("status") == "dry_run"
        and r.get("dry_run") is True
    ]
    if len(confirmed) != 1 or len(simulation) != 5:
        raise ValueError(
            "Results no longer match the agreed preserved and simulated history"
        )
    fingerprint = hashlib.sha256(
        json.dumps([ids, raw, results_raw], separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ids, raw, results_raw, fingerprint


def _cleanup(store, snapshot):
    ids, raw, results_raw, current = _snapshot(store)
    if not hmac.compare_digest(snapshot, current):
        raise ValueError("Inventory changed since preview; export again before cleanup")
    keys = [
        store.BOOKINGS_INDEX_KEY,
        store.RESULTS_KEY,
        *(store.booking_record_key(booking_id) for booking_id in ids),
    ]
    # Entire operation is atomic. Any changed key, index or history aborts.
    script = """
local ids = cjson.decode(ARGV[1])
local raw = cjson.decode(ARGV[2])
local history = cjson.decode(ARGV[3])
local removals = cjson.decode(ARGV[4])
local to_remove_results = cjson.decode(ARGV[5])
local live_ids = redis.call('ZRANGE', KEYS[1], 0, -1)
if #live_ids ~= #ids then return {0, 'index_changed'} end
for i=1,#ids do
  if live_ids[i] ~= ids[i] then return {0, 'index_changed'} end
  if redis.call('GET', KEYS[i+2]) ~= raw[i] then return {0, 'booking_changed'} end
end
local live_results = redis.call('LRANGE', KEYS[2], 0, -1)
if #live_results ~= #history then return {0, 'history_changed'} end
for i=1,#history do
  if live_results[i] ~= history[i] then return {0, 'history_changed'} end
end
local removed = 0
for i=1,#ids do
  if removals[ids[i]] then
    removed = removed + redis.call('DEL', KEYS[i+2])
    redis.call('ZREM', KEYS[1], ids[i])
  end
end
local removed_history = 0
for i=1,#history do
  if to_remove_results[i] then
    removed_history = removed_history + redis.call('LREM', KEYS[2], 1, history[i])
  end
end
return {removed, removed_history}
"""
    to_remove_results = [
        result.get("booking_id") in EXPECTED_RESULT_IDS
        for result in (json.loads(value) for value in results_raw)
    ]
    command = [
        "EVAL",
        script,
        len(keys),
        *keys,
        json.dumps(ids),
        json.dumps(raw),
        json.dumps(results_raw),
        json.dumps({booking_id: True for booking_id in TEST_STATUSES}),
        json.dumps(to_remove_results),
    ]
    result = store._command(command)
    if result != [9, 5]:
        raise RedisUnavailable("Cleanup aborted or produced unexpected counts")
    return {"removed_bookings": 9, "removed_simulation_results": 5}


class handler(BaseHTTPRequestHandler):
    def _authorized(self):
        token = os.environ.get("FITBOT_ADMIN_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        return bool(
            token
            and supplied.startswith("Bearer ")
            and hmac.compare_digest(supplied[7:], token)
            and os.environ.get("VERCEL_ENV") == "production"
        )

    def do_GET(self):
        if not self._authorized():
            return self._respond(403, {"error": "Not authorized in Production"})
        try:
            store = UpstashRedis()
            _, _, _, fingerprint = _snapshot(store)
            return self._respond(200, {
                "ready": True,
                "snapshot": fingerprint,
                "expected_removed_bookings": 9,
                "expected_removed_results": 5,
                "preserved_real_booking_id": PRESERVED_ID,
                "backup_required": True,
                "note": "No data has been changed. Export a fresh inventory before confirming.",
            })
        except (RedisUnavailable, ValueError):
            logger.exception("Cleanup preflight failed")
            return self._respond(409, {"error": "Inventory changed; cleanup blocked"})

    def do_POST(self):
        if not self._authorized():
            return self._respond(403, {"error": "Not authorized in Production"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            if (
                not isinstance(payload, dict)
                or set(payload) != {"confirm_cleanup", "snapshot", "backup_saved"}
                or payload.get("confirm_cleanup") is not True
                or payload.get("backup_saved") is not True
                or not isinstance(payload.get("snapshot"), str)
                or len(payload["snapshot"]) != 64
            ):
                raise ValueError("Confirmation, backup and snapshot required")
            result = _cleanup(UpstashRedis(), payload["snapshot"])
            return self._respond(200, result)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            return self._respond(409, {"error": str(error)})
        except RedisUnavailable:
            logger.exception("Cleanup failed")
            return self._respond(503, {"error": "Cleanup unavailable or aborted"})

    def _respond(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.info("Controlled test data cleanup: " + format, *args)
