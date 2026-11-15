"""Reconcile confirmed future bookings against AimHarder on dashboard entry."""

import hashlib
import hmac
import json
import logging
import re
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from client import AimHarderClient  # noqa: E402
from redis_store import RedisUnavailable, UpstashRedis  # noqa: E402
from scheduled import load_config  # noqa: E402

logger = logging.getLogger("fitbot.sync")
CONFIRMED_STATUSES = {"booking_confirmed", "already_booked"}


def _respond(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _authorized(handler):
    expected = os.environ.get("FITBOT_ADMIN_TOKEN", "")
    supplied = handler.headers.get("Authorization", "")
    return bool(
        expected
        and supplied.startswith("Bearer ")
        and hmac.compare_digest(supplied[7:], expected)
    )


def _resolve_exact_class(classes, target_time, class_name):
    """Resolve one exact class by normalized time and full name only."""
    requested_time = str(target_time).replace(":", "").strip()
    requested_name = " ".join(str(class_name).split()).casefold()
    matches = [
        item
        for item in classes
        if isinstance(item, dict)
        and str(item.get("timeid", "")).split("_", 1)[0] == requested_time
        and isinstance(item.get("className"), str)
        and " ".join(item["className"].split()).casefold() == requested_name
    ]
    if len(matches) != 1:
        raise ValueError("Class is missing or ambiguous")
    return matches[0]


def _check_confirmed_bookings(
    config, records, now, client_factory=None, booking_store=None
):
    """Reconcile confirmed future bookings against AimHarder, preserving history."""
    client_factory = client_factory or AimHarderClient
    timezone = ZoneInfo(config.get("timezone", "Europe/Madrid"))
    grouped = defaultdict(list)
    for record in records:
        if record.get("status") not in CONFIRMED_STATUSES:
            continue
        if record.get("person_id") not in config.get("members", {}):
            continue
        try:
            target_date = date.fromisoformat(record["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if target_date < now.astimezone(timezone).date():
            continue
        grouped[(record["person_id"], target_date)].append(record)

    discrepancies = []
    unverified = []
    checked = 0
    clients = {}

    for (person_id, target_date), bookings in sorted(
        grouped.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        member = config["members"][person_id]
        credentials = member.get("aimharder_credentials", {})
        email_env = credentials.get("email_env")
        password_env = credentials.get("password_env")
        email = os.environ.get(email_env) if email_env else None
        password = os.environ.get(password_env) if password_env else None
        if not email or not password:
            unverified.extend(
                {
                    "booking_id": booking.get("id"),
                    "person_id": person_id,
                    "date": booking.get("date"),
                    "time": booking.get("time"),
                    "name": booking.get("name"),
                    "reason": "credentials_unavailable",
                }
                for booking in bookings
            )
            continue

        if person_id not in clients:
            try:
                clients[person_id] = client_factory(
                    email=email,
                    password=password,
                    box_id=int(config["box_id"]),
                    box_name=config["box_name"],
                    proxy=os.environ.get("PROXY") or None,
                )
            except Exception:
                logger.exception(
                    "AimHarder login failed during dashboard sync for %s", person_id
                )
                clients[person_id] = None
        client = clients[person_id]
        if client is None:
            unverified.extend(
                {
                    "booking_id": booking.get("id"),
                    "person_id": person_id,
                    "date": booking.get("date"),
                    "time": booking.get("time"),
                    "name": booking.get("name"),
                    "reason": "account_unavailable",
                }
                for booking in bookings
            )
            continue

        target_day = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone)
        try:
            classes = client.get_classes(target_day, config.get("family_id"))
        except Exception:
            logger.exception(
                "AimHarder classes lookup failed for %s on %s", person_id, target_date
            )
            unverified.extend(
                {
                    "booking_id": booking.get("id"),
                    "person_id": person_id,
                    "date": booking.get("date"),
                    "time": booking.get("time"),
                    "name": booking.get("name"),
                    "reason": "classes_unavailable",
                }
                for booking in bookings
            )
            continue

        for booking in bookings:
            try:
                selected = _resolve_exact_class(
                    classes, booking["time"], booking["name"]
                )
            except Exception:
                # Missing/renamed/ambiguous classes are not proof of cancellation.
                unverified.append(
                    {
                        "booking_id": booking.get("id"),
                        "person_id": person_id,
                        "date": booking.get("date"),
                        "time": booking.get("time"),
                        "name": booking.get("name"),
                        "reason": "class_not_resolved",
                    }
                )
                continue

            book_state = selected.get("bookState")
            if book_state == 1:
                checked += 1
            elif book_state == 0 or book_state is None:
                checked += 1
                discrepancy = {
                    "booking_id": booking.get("id"),
                    "person_id": person_id,
                    "date": booking.get("date"),
                    "time": booking.get("time"),
                    "name": booking.get("name"),
                    "reason": "not_booked_in_aimharder",
                    "observed_book_state": book_state,
                }
                if booking_store is not None:
                    try:
                        updated = booking_store.update_booking_status(
                            booking["id"],
                            "cancelled",
                            expected_statuses=sorted(CONFIRMED_STATUSES),
                            metadata={
                                "cancelled_at": now.isoformat(),
                                "error_code": "cancelled_in_aimharder",
                            },
                        )
                        discrepancy["fitbot_status_updated"] = updated is not None
                    except Exception:
                        logger.exception(
                            "Could not synchronize external cancellation for "
                            "booking %s",
                            booking.get("id"),
                        )
                        discrepancy["fitbot_status_updated"] = False
                        unverified.append(
                            {
                                "booking_id": booking.get("id"),
                                "person_id": person_id,
                                "date": booking.get("date"),
                                "time": booking.get("time"),
                                "name": booking.get("name"),
                                "reason": "fitbot_status_update_failed",
                            }
                        )
                discrepancies.append(discrepancy)
            else:
                raw_state = selected.get("bookState")
                # Expose only this single API field for diagnosis; never log the
                # full AimHarder class payload or account information.
                state_value = (
                    raw_state
                    if raw_state is None or isinstance(raw_state, (int, float, bool))
                    else str(raw_state)[:40]
                )
                unverified.append(
                    {
                        "booking_id": booking.get("id"),
                        "person_id": person_id,
                        "date": booking.get("date"),
                        "time": booking.get("time"),
                        "name": booking.get("name"),
                        "reason": "unknown_booking_state",
                        "observed_book_state": state_value,
                        "observed_book_state_type": type(raw_state).__name__,
                    }
                )

    return {
        "status": "completed",
        "checked": checked,
        "discrepancies": discrepancies,
        "unverified": unverified,
        "fitbot_updates_enabled": booking_store is not None,
    }


DISCOVERY_DAYS = 7
MAX_IMPORT_BODY_BYTES = 1024


def _booking_identity(person_id, target_date, target_time, name):
    return (
        person_id,
        (
            target_date.isoformat()
            if hasattr(target_date, "isoformat")
            else str(target_date)
        ),
        str(target_time).strip(),
        " ".join(str(name).split()).casefold(),
    )


def _discover_external_bookings(
    config, records, now, client_factory=None, booking_store=None
):
    """Discover future AimHarder bookings and import them without scheduling."""
    client_factory = client_factory or AimHarderClient
    timezone = ZoneInfo(config.get("timezone", "Europe/Madrid"))
    existing = {
        _booking_identity(
            item.get("person_id"), item.get("date"), item.get("time"), item.get("name")
        )
        for item in records
        if isinstance(item, dict)
        and item.get("person_id")
        and item.get("date")
        and item.get("time")
        and item.get("name")
    }
    activity_names = {}
    for activity_type in ("class", "open_box"):
        for name in config.get("activities", {}).get(activity_type, []):
            activity_names[" ".join(str(name).split()).casefold()] = (
                " ".join(str(name).split()),
                activity_type,
            )

    detected = []
    imported = []
    already_tracked = 0
    unverified = []

    for person_id, member in config.get("members", {}).items():
        credentials = member.get("aimharder_credentials", {})
        email_env = credentials.get("email_env")
        password_env = credentials.get("password_env")
        email = os.environ.get(email_env) if email_env else None
        password = os.environ.get(password_env) if password_env else None
        if not email or not password:
            unverified.append(
                {"person_id": person_id, "reason": "credentials_unavailable"}
            )
            continue
        try:
            client = client_factory(
                email=email,
                password=password,
                box_id=int(config["box_id"]),
                box_name=config["box_name"],
                proxy=os.environ.get("PROXY") or None,
            )
        except Exception:
            logger.exception("AimHarder login failed during external booking discovery")
            unverified.append({"person_id": person_id, "reason": "account_unavailable"})
            continue

        for offset in range(DISCOVERY_DAYS + 1):
            target_date = now.astimezone(timezone).date() + timedelta(days=offset)
            target_day = datetime.combine(
                target_date, datetime.min.time(), tzinfo=timezone
            )
            try:
                classes = client.get_classes(target_day, config.get("family_id"))
            except Exception:
                logger.exception(
                    "AimHarder discovery lookup failed for %s on %s",
                    person_id,
                    target_date,
                )
                unverified.append(
                    {
                        "person_id": person_id,
                        "date": target_date.isoformat(),
                        "reason": "classes_unavailable",
                    }
                )
                continue

            grouped = defaultdict(list)
            for item in classes:
                if not isinstance(item, dict) or item.get("bookState") != 1:
                    continue
                raw_name = item.get("className")
                if not isinstance(raw_name, str):
                    continue
                configured = activity_names.get(" ".join(raw_name.split()).casefold())
                if configured is None:
                    continue
                raw_time = str(item.get("timeid", "")).split("_", 1)[0]
                if len(raw_time) != 4 or not raw_time.isdigit():
                    continue
                try:
                    class_time = datetime.strptime(raw_time, "%H%M").time()
                except ValueError:
                    continue
                class_start = datetime.combine(target_date, class_time, tzinfo=timezone)
                if class_start <= now.astimezone(timezone):
                    continue
                canonical_name, activity_type = configured
                identity = _booking_identity(
                    person_id,
                    target_date,
                    class_time.strftime("%H:%M"),
                    canonical_name,
                )
                grouped[identity].append((class_start, canonical_name, activity_type))

            for identity, matches in grouped.items():
                if len(matches) != 1:
                    unverified.append(
                        {
                            "person_id": person_id,
                            "date": target_date.isoformat(),
                            "time": identity[2],
                            "name": identity[3],
                            "reason": "ambiguous_external_class",
                        }
                    )
                    continue
                if identity in existing:
                    already_tracked += 1
                    continue

                class_start, canonical_name, activity_type = matches[0]
                booking_id = hashlib.sha256(
                    "|".join(identity).encode("utf-8")
                ).hexdigest()[:32]
                booking = {
                    "id": booking_id,
                    "person_id": person_id,
                    "date": target_date.isoformat(),
                    "time": class_start.strftime("%H:%M"),
                    "name": canonical_name,
                    "activity_type": activity_type,
                    "opens_at": class_start.isoformat(timespec="seconds"),
                    "status": "already_booked",
                    "created_at": now.astimezone(timezone).isoformat(
                        timespec="seconds"
                    ),
                }
                candidate = {
                    "person_id": person_id,
                    "date": booking["date"],
                    "time": booking["time"],
                    "name": booking["name"],
                }
                detected.append(candidate)
                if booking_store is not None:
                    try:
                        if booking_store.create_booking(booking):
                            imported.append(candidate)
                            existing.add(identity)
                        else:
                            already_tracked += 1
                            existing.add(identity)
                    except Exception:
                        logger.exception(
                            "Could not import external booking for %s on %s",
                            person_id,
                            target_date,
                        )
                        unverified.append(
                            {**candidate, "reason": "fitbot_import_failed"}
                        )

    return {
        "external_bookings": detected,
        "imported": len(imported),
        "imported_bookings": imported,
        "already_tracked": already_tracked,
        "discovery_unverified": unverified,
        "import_writes_enabled": booking_store is not None,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not _authorized(self):
            return _respond(self, 401, {"error": "Unauthorized"})
        if os.environ.get("VERCEL_ENV") not in ("preview", "production"):
            return _respond(
                self,
                403,
                {"error": "Vercel Preview or Production environment only"},
            )

        try:
            config = load_config(ROOT / "reservas.json")
            timezone = ZoneInfo(config.get("timezone", "Europe/Madrid"))
            now = datetime.now(timezone)
            store = UpstashRedis()
            records = store.list_bookings(limit=UpstashRedis.BOOKINGS_MAX_ITEMS)
            is_preview = os.environ.get("VERCEL_ENV") == "preview"
            # Preview may share Production Redis on the free Upstash tier.
            # It can inspect bookings and AimHarder, but must never write to Redis.
            result = _check_confirmed_bookings(
                config, records, now, booking_store=None if is_preview else store
            )
            discovery = _discover_external_bookings(
                config,
                records,
                now,
                # Discovery is always read-only. A separate, explicitly
                # authorized action will perform any Production import.
                booking_store=None,
            )
            result["unverified"].extend(discovery.pop("discovery_unverified"))
            result.update(discovery)
            result["read_only"] = is_preview
            reason_counts = defaultdict(int)
            for item in result["unverified"]:
                reason_counts[item.get("reason", "unknown")] += 1
            logger.info(
                "Booking reconciliation completed: checked=%s "
                "discrepancies=%s unverified=%s reasons=%s",
                result["checked"],
                len(result["discrepancies"]),
                len(result["unverified"]),
                dict(reason_counts),
            )
            return _respond(self, 200, result)
        except RedisUnavailable:
            logger.exception("Booking store unavailable during dashboard sync")
            return _respond(self, 503, {"error": "Booking store unavailable"})
        except Exception:
            logger.exception("Unexpected dashboard booking sync failure")
            return _respond(self, 503, {"error": "Booking sync unavailable"})

    def do_POST(self):
        if not _authorized(self):
            return _respond(self, 401, {"error": "Unauthorized"})
        if os.environ.get("VERCEL_ENV") != "production":
            return _respond(
                self,
                403,
                {"error": "External booking import is Production-only"},
            )

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_IMPORT_BODY_BYTES:
                return _respond(self, 400, {"error": "Invalid request body size"})
            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict) or payload != {"confirm_import": True}:
                return _respond(
                    self,
                    400,
                    {"error": "Explicit confirmation is required"},
                )

            config = load_config(ROOT / "reservas.json")
            timezone = ZoneInfo(config.get("timezone", "Europe/Madrid"))
            now = datetime.now(timezone)
            store = UpstashRedis()
            records = store.list_bookings(limit=UpstashRedis.BOOKINGS_MAX_ITEMS)
            # Re-query AimHarder immediately before writing. The operation only
            # creates local already_booked records; it never books/cancels in
            # AimHarder or publishes work to QStash.
            result = _discover_external_bookings(
                config, records, now, booking_store=store
            )
            result["read_only"] = False
            logger.info(
                "Explicit external booking import completed: imported=%s "
                "already_tracked=%s unverified=%s",
                result["imported"],
                result["already_tracked"],
                len(result["discovery_unverified"]),
            )
            return _respond(self, 200, result)
        except (TypeError, ValueError, json.JSONDecodeError):
            return _respond(self, 400, {"error": "Invalid import confirmation"})
        except RedisUnavailable:
            logger.exception("Booking store unavailable during external import")
            return _respond(self, 503, {"error": "Booking store unavailable"})
        except Exception:
            logger.exception("Unexpected external booking import failure")
            return _respond(self, 503, {"error": "External booking import unavailable"})

    def log_message(self, format, *args):
        logger.info("FitBot sync request: " + format, *args)
