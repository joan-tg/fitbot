import hashlib
import json

import requests
import pytest

from redis_store import RedisUnavailable, UpstashRedis


BOOKING = {
    "date": "2026-11-04",
    "time": "17:15",
    "name": "Weightlifting",
}


class FakeResponse:
    def __init__(self, result):
        self.payload = {"result": result}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(next(self.results))


def test_acquire_uses_atomic_set_nx_ex_and_returns_owner_token():
    session = FakeSession(["OK"])
    redis = UpstashRedis(
        url="https://example.upstash.io/",
        token="test-token",
        session=session,
    )

    token = redis.acquire(BOOKING, dry_run=True, ttl_seconds=300)

    assert isinstance(token, str) and len(token) == 32
    url, kwargs = session.calls[0]
    assert url == "https://example.upstash.io"
    assert kwargs["json"][:5] == [
        "SET",
        redis.booking_key(BOOKING, dry_run=True),
        token,
        "NX",
        "EX",
    ]
    assert kwargs["json"][5] == 300
    assert kwargs["headers"] == {"Authorization": "Bearer test-token"}
    assert kwargs["timeout"] == 8


def test_acquire_returns_none_when_lock_already_exists():
    session = FakeSession([None])
    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=session,
    )

    assert redis.acquire(BOOKING, dry_run=True) is None


def test_complete_updates_only_lock_owned_by_this_invocation():
    session = FakeSession([1])
    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=session,
    )

    assert redis.complete(BOOKING, "owner-token", "booking_confirmed") is True
    command = session.calls[0][1]["json"]
    assert command[0] == "EVAL"
    assert "redis.call('GET', KEYS[1]) == ARGV[1]" in command[1]
    assert command[3:] == [
        redis.booking_key(BOOKING),
        "owner-token",
        "booking_confirmed",
        60,
    ]


def test_release_deletes_only_lock_owned_by_this_invocation():
    session = FakeSession([1])
    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=session,
    )

    assert redis.release(BOOKING, "owner-token") is True
    command = session.calls[0][1]["json"]
    assert command[0] == "EVAL"
    assert "redis.call('DEL', KEYS[1])" in command[1]
    assert command[3:] == [redis.booking_key(BOOKING), "owner-token"]


def test_acquire_fails_closed_when_upstash_request_fails():
    class FailedSession:
        def post(self, url, **kwargs):
            response = requests.Response()
            response.status_code = 400
            raise requests.HTTPError("Bad Request", response=response)

    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=FailedSession(),
    )

    with pytest.raises(RedisUnavailable):
        redis.acquire(BOOKING, dry_run=True)


def test_missing_credentials_fail_closed(monkeypatch):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)

    with pytest.raises(RedisUnavailable):
        UpstashRedis()


def test_record_booking_result_uses_one_bounded_redis_command():
    session = FakeSession([1])
    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=session,
    )

    count = redis.record_booking_result(
        {
            "date": "2026-11-09",
            "time": "18:15",
            "class": "Strength Conditioning",
            "status": "booking_confirmed",
            "dry_run": False,
        }
    )

    assert count == 1
    command = session.calls[0][1]["json"]
    assert command[0] == "EVAL"
    assert "LPUSH" in command[1]
    assert "LTRIM" in command[1]
    assert "EXPIRE" in command[1]
    assert command[3] == redis.RESULTS_KEY
    assert command[5:] == [redis.RESULTS_MAX_ITEMS - 1, redis.RESULTS_TTL_SECONDS]


def test_list_booking_results_reads_and_decodes_json_records():
    record = '{"status":"class_not_found","class":"Strength Conditioning"}'
    session = FakeSession([[record]])
    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=session,
    )

    results = redis.list_booking_results(limit=10)

    assert results == [
        {
            "status": "class_not_found",
            "class": "Strength Conditioning",
        }
    ]
    assert session.calls[0][1]["json"] == [
        "LRANGE",
        redis.RESULTS_KEY,
        0,
        9,
    ]


def test_delete_confirmed_booking_results_matches_only_exact_live_confirmed():
    session = FakeSession([2])
    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=session,
    )

    deleted = redis.delete_confirmed_booking_results(
        ["2026-11-07T08:00:08.140428+00:00", "2026-11-06T17:15:02.501123+00:00"]
    )

    assert deleted == 2
    command = session.calls[0][1]["json"]
    assert command[0] == "EVAL"
    assert "item.status == 'booking_confirmed'" in command[1]
    assert "item.dry_run == false" in command[1]
    assert "item.recorded_at == timestamp" in command[1]
    assert command[3] == redis.RESULTS_KEY
    assert json.loads(command[4]) == [
        "2026-11-07T08:00:08.140428+00:00",
        "2026-11-06T17:15:02.501123+00:00",
    ]


def test_delete_confirmed_booking_results_rejects_empty_or_oversized_selection():
    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=FakeSession([]),
    )

    with pytest.raises(ValueError, match="1 and 10"):
        redis.delete_confirmed_booking_results([])
    with pytest.raises(ValueError, match="1 and 10"):
        redis.delete_confirmed_booking_results(["timestamp"] * 11)


def make_booking_record(**overrides):
    record = {
        "id": "a" * 32,
        "person_id": "person_1",
        "date": "2026-11-09",
        "time": "18:15",
        "name": "Strength Conditioning",
        "activity_type": "class",
        "opens_at": "2026-11-06T18:15:00+01:00",
        "status": "pending_schedule",
        "created_at": "2026-11-07T12:00:00+01:00",
    }
    record.update(overrides)
    return record


def test_create_booking_atomically_stores_record_and_indexes_id():
    session = FakeSession([1])
    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=session,
    )

    assert redis.create_booking(make_booking_record()) is True
    command = session.calls[0][1]["json"]
    assert command[0] == "EVAL"
    assert "ZADD" in command[1]
    assert "ZREMRANGEBYRANK" in command[1]
    assert command[3] == redis.booking_record_key("a" * 32)
    assert command[4] == redis.BOOKINGS_INDEX_KEY
    assert json.loads(command[5])["person_id"] == "person_1"
    assert command[7] == redis.BOOKINGS_TTL_SECONDS
    assert command[8] == "a" * 32


def test_create_booking_rejects_invalid_person_and_duplicate_id():
    session = FakeSession([0])
    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=session,
    )

    with pytest.raises(ValueError, match="person_id"):
        redis.create_booking(make_booking_record(person_id="person_3"))
    assert redis.create_booking(make_booking_record()) is False


def test_get_booking_reads_and_decodes_record():
    record = make_booking_record()
    session = FakeSession([json.dumps(record)])
    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=session,
    )

    assert redis.get_booking("a" * 32) == record
    assert session.calls[0][1]["json"] == ["GET", redis.booking_record_key("a" * 32)]


def test_update_booking_status_uses_atomic_expected_state_guard():
    updated = make_booking_record(status="scheduled", message_id="qstash-id")
    session = FakeSession([[1, json.dumps(updated)]])
    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=session,
    )

    result = redis.update_booking_status(
        "a" * 32,
        "scheduled",
        expected_statuses=["pending_schedule"],
        metadata={"message_id": "qstash-id"},
    )

    assert result == updated
    command = session.calls[0][1]["json"]
    assert command[0] == "EVAL"
    assert "status_conflict" in command[1]
    assert "KEEPTTL" in command[1]
    assert command[3] == redis.booking_record_key("a" * 32)
    assert command[6] == '["pending_schedule"]'
    assert json.loads(command[7]) == {"message_id": "qstash-id"}


def test_update_booking_status_reports_conflict_and_missing_record():
    session = FakeSession([[0, "status_conflict"], [0, "not_found"]])
    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=session,
    )

    conflict = redis.update_booking_status(
        "a" * 32,
        "cancelled",
        expected_statuses=["scheduled"],
    )
    assert conflict is None
    with pytest.raises(KeyError, match="not found"):
        redis.update_booking_status("b" * 32, "cancelled")


def test_list_bookings_reads_index_and_skips_expired_records():
    first = make_booking_record()
    session = FakeSession([["a" * 32, "b" * 32], [json.dumps(first), None]])
    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=session,
    )

    assert redis.list_bookings(limit=10) == [first]
    assert session.calls[0][1]["json"] == [
        "ZREVRANGE",
        redis.BOOKINGS_INDEX_KEY,
        0,
        9,
    ]
    assert session.calls[1][1]["json"] == [
        "MGET",
        redis.booking_record_key("a" * 32),
        redis.booking_record_key("b" * 32),
    ]


def test_booking_record_key_rejects_untrusted_identifiers():
    with pytest.raises(ValueError, match="booking ID"):
        UpstashRedis.booking_record_key("../booking")


def test_create_booking_idempotent_stores_request_fingerprint_atomically():
    record = make_booking_record(id="c" * 32)
    fingerprint_source = {
        "person_id": record["person_id"],
        "date": record["date"],
        "time": record["time"],
        "name": record["name"],
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_source, sort_keys=True).encode("utf-8")
    ).hexdigest()
    stored_request = json.dumps(
        {
            "booking_id": "c" * 32,
            "fingerprint": fingerprint,
        }
    )
    session = FakeSession([[1, stored_request]])
    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=session,
    )

    created, booking_id = redis.create_booking_idempotent(record, "client-request-0001")

    assert created is True
    assert booking_id == "c" * 32
    command = session.calls[0][1]["json"]
    assert command[0] == "EVAL"
    assert "booking_requests:" in command[5]
    assert "ZADD" in command[1]
    assert json.loads(command[8])["booking_id"] == "c" * 32


def test_create_booking_idempotent_returns_existing_booking_for_same_request():
    record = make_booking_record()
    fingerprint_source = {
        field: record[field] for field in ("person_id", "date", "time", "name")
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_source, sort_keys=True).encode("utf-8")
    ).hexdigest()
    existing = json.dumps({"booking_id": record["id"], "fingerprint": fingerprint})
    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=FakeSession([[0, existing]]),
    )

    assert redis.create_booking_idempotent(record, "client-request-0001") == (
        False,
        record["id"],
    )


def test_create_booking_idempotent_rejects_reused_key_for_different_request():
    existing_record = {
        "booking_id": "a" * 32,
        "fingerprint": "different-fingerprint",
    }
    existing = json.dumps(existing_record)
    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=FakeSession([[0, existing]]),
    )

    with pytest.raises(ValueError, match="different booking"):
        redis.create_booking_idempotent(make_booking_record(), "client-request-0001")


def test_create_booking_idempotent_rejects_invalid_key():
    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=FakeSession([]),
    )

    with pytest.raises(ValueError, match="Idempotency-Key"):
        redis.create_booking_idempotent(make_booking_record(), "short")


def test_booking_lock_identity_is_scoped_by_member_for_dynamic_bookings():
    first = dict(BOOKING, person_id="person_1")
    second = dict(BOOKING, person_id="person_2")

    assert UpstashRedis.booking_key(first) != UpstashRedis.booking_key(second)
    assert UpstashRedis.booking_key(BOOKING) == UpstashRedis.booking_key(dict(BOOKING))
