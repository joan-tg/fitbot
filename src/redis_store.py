"""Atomic idempotency locks and compact booking history in Upstash Redis."""

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone

import requests


class RedisUnavailable(RuntimeError):
    pass


class UpstashRedis:
    RESULTS_KEY = "fitbot:booking_results"
    RESULTS_TTL_SECONDS = 90 * 24 * 60 * 60
    RESULTS_MAX_ITEMS = 100

    def __init__(self, url=None, token=None, session=None):
        self.url = (url or os.environ.get("UPSTASH_REDIS_REST_URL", "")).rstrip("/")
        self.token = token or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
        self.session = session or requests
        if not self.url or not self.token:
            raise RedisUnavailable("Upstash Redis credentials are not configured")

    def _command(self, command):
        try:
            response = self.session.post(
                self.url,
                json=command,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=8,
            )
            response.raise_for_status()
            payload = response.json()
            if "error" in payload:
                raise RedisUnavailable("Upstash Redis command failed")
            return payload.get("result")
        except (requests.RequestException, ValueError, TypeError) as error:
            raise RedisUnavailable("Could not execute Redis command") from error

    @staticmethod
    def booking_key(booking, dry_run=False):
        identity = "|".join(
            str(booking[field]) for field in ("date", "time", "name")
        ).casefold()
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        namespace = "fitbot:dryrun" if dry_run else "fitbot:booking"
        return f"{namespace}:{digest}"

    def acquire(self, booking, dry_run=False, ttl_seconds=300):
        """Acquire a booking lock and return its unique owner token, or None."""
        token = uuid.uuid4().hex
        key = self.booking_key(booking, dry_run=dry_run)
        result = self._command(["SET", key, token, "NX", "EX", int(ttl_seconds)])
        return token if result == "OK" else None

    def complete(self, booking, token, status, dry_run=False, ttl_seconds=60):
        """Replace an owned processing lock with a terminal state atomically."""
        script = (
            "if redis.call('GET', KEYS[1]) == ARGV[1] then "
            "redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3]); "
            "return 1 else return 0 end"
        )
        result = self._command(
            [
                "EVAL",
                script,
                1,
                self.booking_key(booking, dry_run),
                token,
                str(status),
                int(ttl_seconds),
            ]
        )
        return result == 1

    def release(self, booking, token, dry_run=False):
        """Delete a lock only if it is still owned by this invocation."""
        script = (
            "if redis.call('GET', KEYS[1]) == ARGV[1] then "
            "return redis.call('DEL', KEYS[1]) else return 0 end"
        )
        result = self._command(
            ["EVAL", script, 1, self.booking_key(booking, dry_run), token]
        )
        return result == 1

    def record_booking_result(self, record):
        """Store a bounded, non-sensitive history record for later inspection."""
        item = dict(record)
        item["recorded_at"] = datetime.now(timezone.utc).isoformat()
        script = (
            "redis.call('LPUSH', KEYS[1], ARGV[1]); "
            "redis.call('LTRIM', KEYS[1], 0, ARGV[2]); "
            "redis.call('EXPIRE', KEYS[1], ARGV[3]); "
            "return redis.call('LLEN', KEYS[1])"
        )
        return self._command(
            [
                "EVAL",
                script,
                1,
                self.RESULTS_KEY,
                json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                self.RESULTS_MAX_ITEMS - 1,
                self.RESULTS_TTL_SECONDS,
            ]
        )

    def list_booking_results(self, limit=50):
        """Return the newest stored booking results, newest first."""
        count = max(1, min(int(limit), self.RESULTS_MAX_ITEMS))
        values = self._command(["LRANGE", self.RESULTS_KEY, 0, count - 1]) or []
        try:
            return [json.loads(value) for value in values]
        except (TypeError, json.JSONDecodeError) as error:
            raise RedisUnavailable("Stored booking history is invalid") from error

    @staticmethod
    def integration_result_key(probe_id, step):
        safe_id = re.sub(r"[^\\w-]", "", str(probe_id))
        if not safe_id or step not in ("first", "second"):
            raise ValueError("Invalid integration probe identifier")
        return f"fitbot:integration:{safe_id}:{step}"

    def record_integration_result(self, probe_id, step, result, ttl_seconds=3600):
        key = self.integration_result_key(probe_id, step)
        self._command(["SET", key, str(result), "EX", int(ttl_seconds)])
