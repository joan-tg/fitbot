"""Tests for diagnostic-only, read-only AimHarder credit lookup."""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api.live_credits as credits


def test_parses_available_numbers_not_consumed():
    parsed = credits.parse_available_credits(
        "CrossFit / Fuerza (12/17) <br>Open Box / Open Box (0/4) <br>"
    )
    assert parsed == {
        "classes": {"available": 12, "total": 17, "used_inferred": 5},
        "open_box": {"available": 0, "total": 4, "used_inferred": 4},
    }


def test_missing_category_remains_unknown():
    parsed = credits.parse_available_credits("CrossFit (7/13)<br>")
    assert parsed["classes"]["available"] == 7
    assert parsed["open_box"] is None


@pytest.mark.parametrize("raw", ["", "CrossFit (18/17)", "CrossFit (3/4)<br>Fuerza (2/4)"])
def test_rejects_missing_invalid_or_ambiguous_balances(raw):
    with pytest.raises(ValueError):
        credits.parse_available_credits(raw)


def test_queries_separate_member_accounts_without_writes(monkeypatch):
    monkeypatch.setenv("PERSON_ONE_EMAIL", "one@example.test")
    monkeypatch.setenv("PERSON_ONE_PASSWORD", "onepass")
    monkeypatch.setenv("PERSON_TWO_EMAIL", "two@example.test")
    monkeypatch.setenv("PERSON_TWO_PASSWORD", "twopass")

    config = {
        "box_id": 9507, "box_name": "studiocrossfit", "family_id": None,
        "members": {
            "person_1": {"aimharder_credentials": {
                "email_env": "PERSON_ONE_EMAIL", "password_env": "PERSON_ONE_PASSWORD"}},
            "person_2": {"aimharder_credentials": {
                "email_env": "PERSON_TWO_EMAIL", "password_env": "PERSON_TWO_PASSWORD"}},
        },
    }
    calls = []

    class ReadOnlyClient:
        def __init__(self, email, **kwargs):
            self.email = email
            calls.append(("login", email))

        def get_classes_payload(self, day, family_id):
            calls.append(("get", self.email))
            remaining = 12 if self.email == "one@example.test" else 9
            return {"bookings": [], "clasesDisp": f"CrossFit ({remaining}/17)<br>Open Box (0/4)<br>"}

    response = credits.fetch_member_credits(
        config,
        client_factory=ReadOnlyClient,
        today=datetime(2026, 11, 17, tzinfo=ZoneInfo("Europe/Madrid")),
    )
    assert response["person_1"]["balances"]["classes"]["available"] == 12
    assert response["person_2"]["balances"]["classes"]["available"] == 9
    assert calls == [
        ("login", "one@example.test"), ("get", "one@example.test"),
        ("login", "two@example.test"), ("get", "two@example.test"),
    ]
    assert all(item["error"] is None for item in response.values())
