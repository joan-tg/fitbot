"""Atomic idempotency locks and integration evidence backed by Upstash Redis REST API."""

import hashlib
import os
import re

import requests


class RedisUnavailable(RuntimeError):
    pass


class UpstashRedis:
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

    def acquire(self, booking, dry_run=False, ttl_seconds=60):
        key = self.booking_key(booking, dry_run=dry_run)
        result = self._command(
            ["SET", key, "processing", "NX", "EX", int(ttl_seconds)]
        )
        return result == "OK"

    @staticmethod
    def integration_result_key(probe_id, step):
        safe_id = re.sub(r"[^\w-]", "", str(probe_id))
        if not safe_id or step not in ("first", "second"):
            raise ValueError("Invalid integration probe identifier")
        return f"fitbot:integration:{safe_id}:{step}"

    def record_integration_result(self, probe_id, step, result, ttl_seconds=3600):
        key = self.integration_result_key(probe_id, step)
        self._command(["SET", key, str(result), "EX", int(ttl_seconds)])
