import json
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from client import AimHarderClient
from exceptions import BookingFailed, BoxClosed, NoBookingGoal
from logger import logger
from fitbot_cli import get_class_to_book

CONFIG_PATH = Path(__file__).resolve().parents[1] / "reservas.json"
BOOKING_FIELDS = ("date", "opens_at", "time", "name")


def validate_config(config):
    """Reject ambiguous or malformed booking schedules before contacting AimHarder."""
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    timezone = ZoneInfo(config.get("timezone", "Europe/Madrid"))
    window_minutes = int(config.get("window_minutes", 30))
    if window_minutes <= 0:
        raise ValueError("window_minutes must be a positive integer")
    bookings = config.get("bookings")
    if not isinstance(bookings, list):
        raise ValueError("bookings must be a list")

    seen = set()
    for index, booking in enumerate(bookings):
        if not isinstance(booking, dict):
            raise ValueError(f"Booking {index} must be an object")
        missing = [field for field in BOOKING_FIELDS if not booking.get(field)]
        if missing:
            raise ValueError(f"Booking {index} is missing: {', '.join(missing)}")
        try:
            target_date = date.fromisoformat(booking["date"])
            datetime.strptime(booking["time"], "%H:%M")
            opens_at = parse_local_datetime(booking["opens_at"], timezone)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Booking {index} has an invalid date/time: {error}"
            ) from error

        class_starts_at = datetime.combine(
            target_date, time.fromisoformat(booking["time"]), tzinfo=timezone
        )
        if opens_at >= class_starts_at:
            raise ValueError(f"Booking {index}: opens_at must be before class start")
        identity = (
            booking["date"],
            booking["time"],
            booking["name"].strip().casefold(),
        )
        if identity in seen:
            raise ValueError(
                f"Duplicate booking configured: {booking['date']} "
                f"{booking['time']} {booking['name']}"
            )
        seen.add(identity)
    return True


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as config_file:
        config = json.load(config_file)
    validate_config(config)
    return config


def parse_local_datetime(value, timezone):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def run(config, now=None, client_class=None):
    validate_config(config)
    timezone = ZoneInfo(config.get("timezone", "Europe/Madrid"))
    now = now or datetime.now(timezone)
    if now.tzinfo is None:
        raise ValueError("The current time must include a timezone")
    now = now.astimezone(timezone)

    window_minutes = int(config.get("window_minutes", 30))
    due_bookings = []
    for booking in config["bookings"]:
        opens_at = parse_local_datetime(booking["opens_at"], timezone)
        elapsed = now - opens_at
        if timedelta(0) <= elapsed <= timedelta(minutes=window_minutes):
            due_bookings.append(booking)

    if not due_bookings:
        logger.info("No bookings are due in the current time window.")
        return {"status": "not_due", "processed": 0}

    email = os.environ.get("EMAIL")
    password = os.environ.get("PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "EMAIL and PASSWORD must be configured as environment variables."
        )

    client_class = client_class or AimHarderClient
    client = client_class(
        email=email,
        password=password,
        box_id=int(config["box_id"]),
        box_name=config["box_name"],
        proxy=os.environ.get("PROXY") or None,
    )
    # Fail closed: only the literal value "false" enables live bookings.
    dry_run_setting = os.environ.get("DRY_RUN", str(config.get("dry_run", True)))
    dry_run = dry_run_setting.lower() != "false"
    outcomes = []

    for booking in due_bookings:
        target_day = datetime.combine(
            date.fromisoformat(booking["date"]), time.min, tzinfo=timezone
        )
        target_time = booking["time"].replace(":", "")
        class_name = booking["name"]

        try:
            classes = client.get_classes(target_day, config.get("family_id"))
            selected_class = get_class_to_book(classes, target_time, class_name)
        except (BoxClosed, NoBookingGoal) as error:
            logger.error(
                "Could not identify %s on %s: %s",
                class_name,
                booking["date"],
                error,
            )
            outcomes.append({"status": "class_not_found", "booking": booking})
            continue

        if selected_class.get("bookState") == 1:
            logger.info(
                "Already booked: %s %s on %s",
                booking["time"],
                class_name,
                booking["date"],
            )
            outcomes.append({"status": "already_booked", "booking": booking})
            continue

        if dry_run:
            logger.info(
                "DRY RUN: would book %s %s on %s (class id %s)",
                booking["time"],
                class_name,
                booking["date"],
                selected_class["id"],
            )
            outcomes.append({"status": "dry_run", "booking": booking})
            continue

        try:
            client.book_class(
                target_day,
                selected_class["id"],
                config.get("family_id"),
            )
            logger.info(
                "Booking request sent: %s %s on %s",
                booking["time"],
                class_name,
                booking["date"],
            )
            outcomes.append({"status": "booking_requested", "booking": booking})
        except BookingFailed as error:
            logger.error(
                "Booking failed for %s on %s: %s",
                class_name,
                booking["date"],
                error,
            )
            outcomes.append({"status": "booking_failed", "booking": booking})

    return {
        "status": "completed",
        "processed": len(outcomes),
        "outcomes": outcomes,
    }


if __name__ == "__main__":
    run(load_config())
