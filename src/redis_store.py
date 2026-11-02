"""Atomic idempotency locks backed by Upstash Redis REST API."""
import hashlib
import os

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
        try:
            response = self.session.post(
                f"{self.url}/set/{key}/processing",
                params={"NX": "true", "EX": str(ttl_seconds)},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=8,
            )
            response.raise_for_status()
            result = response.json().get("result")
        except (requests.RequestException, ValueError, TypeError) as error:
            raise RedisUnavailable(
                "Could not acquire Redis idempotency lock"
            ) from error
        return result == "OK"
