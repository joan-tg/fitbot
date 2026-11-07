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
