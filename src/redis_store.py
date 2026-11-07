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

    BOOKINGS_INDEX_KEY = "fitbot:bookings:index"
    BOOKINGS_TTL_SECONDS = 90 * 24 * 60 * 60
    BOOKINGS_MAX_ITEMS = 500
    BOOKING_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
    BOOKING_REQUIRED_FIELDS = (
        "id",
        "person_id",
        "date",
        "time",
        "name",
        "activity_type",
        "opens_at",
        "status",
        "created_at",
    )
    BOOKING_METADATA_FIELDS = {
        "message_id",
        "scheduled_at",
        "cancelled_at",
        "error_code",
    }

    @classmethod
    def booking_record_key(cls, booking_id):
        """Return the Redis key for a generated booking ID."""
        if not isinstance(booking_id, str) or not cls.BOOKING_ID_PATTERN.fullmatch(
            booking_id
        ):
            raise ValueError("Invalid booking ID")
        return f"fitbot:bookings:{booking_id}"

    def create_booking(self, record):
        """Atomically persist a booking and add its ID to the bounded index."""
        if not isinstance(record, dict):
            raise ValueError("Booking record must be an object")
        missing = [
            field for field in self.BOOKING_REQUIRED_FIELDS if not record.get(field)
        ]
        if missing:
            raise ValueError(f"Booking record is missing: {', '.join(missing)}")
        if record["person_id"] not in ("person_1", "person_2"):
            raise ValueError("Invalid person_id")
        if record["activity_type"] not in ("class", "open_box"):
            raise ValueError("Invalid activity_type")
        key = self.booking_record_key(record["id"])
        try:
            created_at = datetime.fromisoformat(record["created_at"])
            if created_at.tzinfo is None:
                raise ValueError("created_at must include a timezone")
            score = created_at.timestamp()
            datetime.fromisoformat(record["date"] + "T" + record["time"])
        except (TypeError, ValueError) as error:
            raise ValueError("Booking record has an invalid date/time") from error

        script = (
            "if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end; "
            "redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3]); "
            "redis.call('ZADD', KEYS[2], ARGV[2], ARGV[4]); "
            "redis.call('ZREMRANGEBYRANK', KEYS[2], 0, -"
            + str(self.BOOKINGS_MAX_ITEMS + 1)
            + "); "
            "redis.call('EXPIRE', KEYS[2], ARGV[3]); "
            "return 1"
        )
        result = self._command(
            [
                "EVAL",
                script,
                2,
                key,
                self.BOOKINGS_INDEX_KEY,
                json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                score,
                self.BOOKINGS_TTL_SECONDS,
                record["id"],
            ]
        )
        return result == 1

    def get_booking(self, booking_id):
        """Fetch one booking record by its opaque ID."""
        value = self._command(["GET", self.booking_record_key(booking_id)])
        if value is None:
            return None
        try:
            record = json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise RedisUnavailable("Stored booking record is invalid") from error
        if not isinstance(record, dict):
            raise RedisUnavailable("Stored booking record is invalid")
        return record

    def update_booking_status(
        self, booking_id, status, expected_statuses=None, metadata=None
    ):
        """Atomically change status, optionally requiring an allowed prior state."""
        if not isinstance(status, str) or not status:
            raise ValueError("Booking status must be a non-empty string")
        metadata = metadata or {}
        if not isinstance(metadata, dict) or not set(metadata).issubset(
            self.BOOKING_METADATA_FIELDS
        ):
            raise ValueError("Invalid booking metadata")
        expected_statuses = expected_statuses or []
        if not isinstance(expected_statuses, (list, tuple)) or any(
            not isinstance(item, str) for item in expected_statuses
        ):
            raise ValueError("expected_statuses must be a list of strings")
        script = (
            "local raw = redis.call('GET', KEYS[1]); "
            "if not raw then return {0, 'not_found'} end; "
            "local booking = cjson.decode(raw); "
            "local expected = cjson.decode(ARGV[3]); "
            "if #expected > 0 then local matched = false; "
            "for _, value in ipairs(expected) do "
            "if booking.status == value then matched = true end end; "
            "if not matched then return {0, 'status_conflict'} end end; "
            "booking.status = ARGV[1]; booking.updated_at = ARGV[2]; "
            "local metadata = cjson.decode(ARGV[4]); "
            "for key, value in pairs(metadata) do booking[key] = value end; "
            "local ttl = redis.call('TTL', KEYS[1]); "
            "if ttl > 0 then redis.call('SET', KEYS[1], cjson.encode(booking), 'EX', ttl) "
            "else redis.call('SET', KEYS[1], cjson.encode(booking), 'KEEPTTL') end; "
            "return {1, cjson.encode(booking)}"
        )
        result = self._command(
            [
                "EVAL",
                script,
                1,
                self.booking_record_key(booking_id),
                status,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(list(expected_statuses)),
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            ]
        )
        if not isinstance(result, list) or len(result) != 2:
            raise RedisUnavailable("Unexpected booking update response")
        if result[0] == 0:
            if result[1] == "not_found":
                raise KeyError("Booking not found")
            return None
        try:
            return json.loads(result[1])
        except (TypeError, json.JSONDecodeError) as error:
            raise RedisUnavailable("Updated booking record is invalid") from error

    def list_bookings(self, limit=100):
        """Return recent booking records, newest created first."""
        count = max(1, min(int(limit), self.BOOKINGS_MAX_ITEMS))
        booking_ids = (
            self._command(["ZREVRANGE", self.BOOKINGS_INDEX_KEY, 0, count - 1]) or []
        )
        if not booking_ids:
            return []
        keys = [self.booking_record_key(booking_id) for booking_id in booking_ids]
        values = self._command(["MGET", *keys]) or []
        records = []
        for value in values:
            if value is None:
                continue
            try:
                record = json.loads(value)
            except (TypeError, json.JSONDecodeError) as error:
                raise RedisUnavailable("Stored booking record is invalid") from error
            if not isinstance(record, dict):
                raise RedisUnavailable("Stored booking record is invalid")
            records.append(record)
        return records


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
        safe_id = re.sub(r"[^\w-]", "", str(probe_id))
        if not safe_id or step not in ("first", "second"):
            raise ValueError("Invalid integration probe identifier")
        return f"fitbot:integration:{safe_id}:{step}"

    def record_integration_result(self, probe_id, step, result, ttl_seconds=3600):
        key = self.integration_result_key(probe_id, step)
        self._command(["SET", key, str(result), "EX", int(ttl_seconds)])
