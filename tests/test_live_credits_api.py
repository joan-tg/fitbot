"""Tests for diagnostic-only, read-only AimHarder credit lookup."""

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import src.live_credits as credits


def test_parses_committed_and_remaining():
    parsed = credits.parse_committed_credits(
        "CrossFit / Fuerza (12/17) <br>Open Box / Open Box (0/4) <br>"
    )
    assert parsed == {
        "classes": {"committed": 12, "total": 17, "remaining": 5},
        "open_box": {"committed": 0, "total": 4, "remaining": 4},
    }


def test_missing_category_remains_unknown():
    parsed = credits.parse_committed_credits("CrossFit (7/13)<br>")
    assert parsed["classes"]["committed"] == 7
    assert parsed["open_box"] is None


@pytest.mark.parametrize(
    "raw", ["", "CrossFit (18/17)", "CrossFit (3/4)<br>Fuerza (2/4)"]
)
def test_rejects_missing_invalid_or_ambiguous_balances(raw):
    with pytest.raises(ValueError):
        credits.parse_committed_credits(raw)


def test_queries_separate_member_accounts_without_writes(monkeypatch):
    monkeypatch.setenv("PERSON_ONE_EMAIL", "one@example.test")
    monkeypatch.setenv("PERSON_ONE_PASSWORD", "onepass")
    monkeypatch.setenv("PERSON_TWO_EMAIL", "two@example.test")
    monkeypatch.setenv("PERSON_TWO_PASSWORD", "twopass")

    config = {
        "box_id": 9507,
        "box_name": "studiocrossfit",
        "family_id": None,
        "booking_policy": {"opening_lead_hours": 72},
        "activities": {"class": ["CrossFit"], "open_box": ["Open Box"]},
        "members": {
            "person_1": {
                "aimharder_credentials": {
                    "email_env": "PERSON_ONE_EMAIL",
                    "password_env": "PERSON_ONE_PASSWORD",
                }
            },
            "person_2": {
                "aimharder_credentials": {
                    "email_env": "PERSON_TWO_EMAIL",
                    "password_env": "PERSON_TWO_PASSWORD",
                }
            },
        },
    }
    calls = []

    class ReadOnlyClient:
        def __init__(self, email, **kwargs):
            self.email = email
            calls.append(("login", email))

        def get_classes_payload(self, day, family_id):
            calls.append(("get", self.email))
            committed = 12 if self.email == "one@example.test" else 9
            return {
                "bookings": [],
                "clasesDisp": f"CrossFit ({committed}/17)<br>Open Box (0/4)<br>",
            }

    response = credits.fetch_member_credits(
        config,
        client_factory=ReadOnlyClient,
        today=datetime(2026, 11, 17, tzinfo=ZoneInfo("Europe/Madrid")),
    )
    assert response["person_1"]["balances"]["classes"]["committed"] == 12
    assert response["person_2"]["balances"]["classes"]["committed"] == 9
    assert calls[0] == ("login", "one@example.test")
    assert calls[1:6] == [("get", "one@example.test")] * 5
    assert calls[6] == ("login", "two@example.test")
    assert calls[7:] == [("get", "two@example.test")] * 5
    assert all(item["error"] is None for item in response.values())


def test_future_bookings_are_subtracted_from_committed():
    now = datetime(2026, 11, 17, 16, 0, tzinfo=ZoneInfo("Europe/Madrid"))
    days = []

    class FakeClient:
        def get_classes_payload(self, target_day, family_id):
            days.append(target_day.date())
            if target_day.day == 18:
                return {
                    "bookings": [
                        {
                            "className": "CrossFit",
                            "bookState": 1,
                            "timeid": "1800_60",
                        },
                        {
                            "className": "Open Box",
                            "bookState": 1,
                            "timeid": "1900_60",
                        },
                    ]
                }
            return {"bookings": []}

    initial = {
        "bookings": [
            {"className": "CrossFit", "bookState": 1, "timeid": "1500_60"},
            {"className": "CrossFit", "bookState": 1, "timeid": "1800_60"},
            {"className": "CrossFit", "bookState": None, "timeid": "1900_60"},
        ]
    }
    future = credits.count_future_bookings(
        FakeClient(),
        {
            "activities": {"class": ["CrossFit"], "open_box": ["Open Box"]},
            "family_id": None,
            "booking_policy": {"opening_lead_hours": 72},
        },
        now,
        initial,
    )
    assert future == {"classes": 2, "open_box": 1}
    assert days[0].day == 18
    assert days[-1].day == 21
    balances = credits.parse_committed_credits("CrossFit (12/17)<br>Open Box (3/4)<br>")
    result = credits.separate_consumed_from_future(balances, future)
    assert result["classes"] == {
        "committed": 12,
        "total": 17,
        "remaining": 5,
        "upcoming_reserved": 2,
        "consumed": 10,
        "verified": True,
    }
    assert result["open_box"]["consumed"] == 2


def test_inconsistent_future_count_does_not_invent_consumption():
    balances = credits.parse_committed_credits("CrossFit (1/17)<br>")
    result = credits.separate_consumed_from_future(
        balances, {"classes": 3, "open_box": 0}
    )
    assert result["classes"]["consumed"] is None
    assert result["classes"]["verified"] is False
