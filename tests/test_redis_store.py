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


def test_acquire_uses_upstash_json_set_nx_ex_command():
    session = FakeSession(["OK"])
    redis = UpstashRedis(
        url="https://example.upstash.io/",
        token="test-token",
        session=session,
    )

    assert redis.acquire(BOOKING, dry_run=True, ttl_seconds=60) is True

    url, kwargs = session.calls[0]
    assert url == "https://example.upstash.io"
    assert kwargs["json"] == [
        "SET",
        redis.booking_key(BOOKING, dry_run=True),
        "processing",
        "NX",
        "EX",
        60,
    ]
    assert kwargs["headers"] == {"Authorization": "Bearer test-token"}
    assert kwargs["timeout"] == 8


def test_acquire_returns_false_when_lock_already_exists():
    session = FakeSession([None])
    redis = UpstashRedis(
        url="https://example.upstash.io",
        token="test-token",
        session=session,
    )

    assert redis.acquire(BOOKING, dry_run=True) is False


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
