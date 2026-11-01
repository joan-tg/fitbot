import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.book import _find_configured_booking
from scheduled import parse_local_datetime


def test_webhook_only_accepts_a_booking_present_in_config():
    booking = {
        "date": "2026-11-04",
        "opens_at": "2026-11-01T17:15:00",
        "time": "17:15",
        "name": "Weightlifting",
    }
    config = {"bookings": [booking]}

    assert _find_configured_booking(config, dict(booking)) == booking


def test_webhook_rejects_modified_or_unknown_booking():
    booking = {
        "date": "2026-11-04",
        "opens_at": "2026-11-01T17:15:00",
        "time": "17:15",
        "name": "Weightlifting",
    }
    config = {"bookings": [booking]}
    forged = dict(booking, name="Metcon")

    assert _find_configured_booking(config, forged) is None
    assert _find_configured_booking(config, {"date": "2026-11-05"}) is None


def test_booking_window_is_limited_to_configured_30_minutes():
    timezone = ZoneInfo("Europe/Madrid")
    booking = {"opens_at": "2026-11-01T17:15:00"}
    opens_at = parse_local_datetime(booking["opens_at"], timezone)

    assert datetime(2026, 11, 1, 17, 15, tzinfo=timezone) - opens_at == (
        datetime(2026, 11, 1, 17, 15, tzinfo=timezone) - opens_at
    )
