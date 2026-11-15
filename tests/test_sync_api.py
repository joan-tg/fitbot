import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api.sync as sync_api

TZ = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=TZ)


def record(status="booking_confirmed", person_id="person_1", day="2026-11-16"):
    return {
        "id": "a" * 32,
        "person_id": person_id,
        "date": day,
        "time": "13:15",
        "name": "Open Box",
        "status": status,
    }


def config():
    return {
        "timezone": "Europe/Madrid",
        "box_name": "studiocrossfit",
        "box_id": 9507,
        "family_id": None,
        "members": {
            "person_1": {
                "aimharder_credentials": {
                    "email_env": "AIMHARDER_PERSON_1_EMAIL",
                    "password_env": "AIMHARDER_PERSON_1_PASSWORD",
                }
            },
            "person_2": {
                "aimharder_credentials": {
                    "email_env": "AIMHARDER_PERSON_2_EMAIL",
                    "password_env": "AIMHARDER_PERSON_2_PASSWORD",
                }
            },
        },
    }


class FakeClient:
    def __init__(self, book_state=1, classes=None, **kwargs):
        self.book_state = book_state
        self.classes = classes
        self.days = []

    def get_classes(self, target_day, family_id=None):
        self.days.append(target_day.date())
        if self.classes is not None:
            return self.classes
        return [
            {
                "timeid": "1315_60",
                "className": "Open Box",
                "bookState": self.book_state,
                "id": "class-1",
            }
        ]


def test_sync_checks_confirmed_future_bookings_without_writes(monkeypatch):
    monkeypatch.setenv("AIMHARDER_PERSON_1_EMAIL", "one@example.test")
    monkeypatch.setenv("AIMHARDER_PERSON_1_PASSWORD", "password")
    clients = []

    def factory(**kwargs):
        client = FakeClient(**kwargs)
        clients.append(client)
        return client

    result = sync_api._check_confirmed_bookings(
        config(),
        [record(), record(status="scheduled", day="2026-11-17")],
        NOW,
        client_factory=factory,
    )

    assert result["checked"] == 1
    assert result["discrepancies"] == []
    assert result["unverified"] == []
    assert result["fitbot_updates_enabled"] is False
    assert len(clients) == 1
    assert clients[0].days == [NOW.date().replace(day=16)]


def test_sync_reports_absent_booking_as_discrepancy_not_cancellation(monkeypatch):
    monkeypatch.setenv("AIMHARDER_PERSON_1_EMAIL", "one@example.test")
    monkeypatch.setenv("AIMHARDER_PERSON_1_PASSWORD", "password")

    result = sync_api._check_confirmed_bookings(
        config(),
        [record()],
        NOW,
        client_factory=lambda **kwargs: FakeClient(book_state=0, **kwargs),
    )

    assert len(result["discrepancies"]) == 1
    assert result["discrepancies"][0]["reason"] == "not_booked_in_aimharder"
    assert result["read_only"] is True


def test_sync_does_not_treat_unresolved_class_as_cancellation(monkeypatch):
    monkeypatch.setenv("AIMHARDER_PERSON_1_EMAIL", "one@example.test")
    monkeypatch.setenv("AIMHARDER_PERSON_1_PASSWORD", "password")

    class MissingClassClient(FakeClient):
        def get_classes(self, target_day, family_id=None):
            return []

    result = sync_api._check_confirmed_bookings(
        config(),
        [record()],
        NOW,
        client_factory=lambda **kwargs: MissingClassClient(**kwargs),
    )

    assert result["discrepancies"] == []
    assert result["unverified"][0]["reason"] == "class_not_resolved"


def test_sync_checks_each_account_separately(monkeypatch):
    for key in (
        "AIMHARDER_PERSON_1_EMAIL",
        "AIMHARDER_PERSON_1_PASSWORD",
        "AIMHARDER_PERSON_2_EMAIL",
        "AIMHARDER_PERSON_2_PASSWORD",
    ):
        monkeypatch.setenv(key, key)
    calls = []

    def factory(**kwargs):
        calls.append(kwargs["email"])
        return FakeClient(**kwargs)

    result = sync_api._check_confirmed_bookings(
        config(),
        [record(person_id="person_1"), record(person_id="person_2")],
        NOW,
        client_factory=factory,
    )

    assert result["checked"] == 2
    assert len(calls) == 2
    assert set(calls) == {"AIMHARDER_PERSON_1_EMAIL", "AIMHARDER_PERSON_2_EMAIL"}


def test_sync_marks_missing_credentials_unverified(monkeypatch):
    monkeypatch.delenv("AIMHARDER_PERSON_1_EMAIL", raising=False)
    monkeypatch.delenv("AIMHARDER_PERSON_1_PASSWORD", raising=False)

    result = sync_api._check_confirmed_bookings(config(), [record()], NOW)

    assert result["checked"] == 0
    assert result["discrepancies"] == []
    assert result["unverified"][0]["reason"] == "credentials_unavailable"


def test_sync_ignores_past_and_unconfirmed_records(monkeypatch):
    result = sync_api._check_confirmed_bookings(
        config(),
        [
            record(day="2026-11-14"),
            record(status="scheduled", day="2026-11-16"),
            record(status="outcome_unconfirmed", day="2026-11-16"),
        ],
        NOW,
    )

    assert result["checked"] == 0
    assert result["discrepancies"] == []
    assert result["unverified"] == []


def test_sync_marks_zero_and_null_as_cancelled_in_fitbot(monkeypatch):
    monkeypatch.setenv("AIMHARDER_PERSON_1_EMAIL", "one@example.test")
    monkeypatch.setenv("AIMHARDER_PERSON_1_PASSWORD", "password")

    class FakeStore:
        def __init__(self):
            self.updates = []

        def update_booking_status(
            self, booking_id, status, expected_statuses, metadata
        ):
            self.updates.append(
                {
                    "booking_id": booking_id,
                    "status": status,
                    "expected_statuses": expected_statuses,
                    "metadata": metadata,
                }
            )
            return {"id": booking_id, "status": status}

    for state in (0, None):
        store = FakeStore()
        result = sync_api._check_confirmed_bookings(
            config(),
            [record()],
            NOW,
            client_factory=lambda **kwargs: FakeClient(book_state=state, **kwargs),
            booking_store=store,
        )

        assert result["checked"] == 1
        assert result["fitbot_updates_enabled"] is True
        assert len(result["discrepancies"]) == 1
        assert result["discrepancies"][0]["fitbot_status_updated"] is True
        assert len(store.updates) == 1
        assert store.updates[0]["status"] == "cancelled"
        assert store.updates[0]["expected_statuses"] == sorted(
            sync_api.CONFIRMED_STATUSES
        )
        assert store.updates[0]["metadata"]["error_code"] == "cancelled_in_aimharder"


def test_sync_requires_exact_class_name_match(monkeypatch):
    monkeypatch.setenv("AIMHARDER_PERSON_1_EMAIL", "one@example.test")
    monkeypatch.setenv("AIMHARDER_PERSON_1_PASSWORD", "password")

    class SimilarClassClient(FakeClient):
        def get_classes(self, target_day, family_id=None):
            return [
                {
                    "timeid": "1315_60",
                    "className": "Open Box Strength",
                    "bookState": 0,
                    "id": "wrong-class",
                }
            ]

    result = sync_api._check_confirmed_bookings(
        config(),
        [record()],
        NOW,
        client_factory=lambda **kwargs: SimilarClassClient(**kwargs),
    )

    assert result["discrepancies"] == []
    assert result["unverified"][0]["reason"] == "class_not_resolved"


def test_sync_accepts_case_and_whitespace_normalization_for_exact_name(monkeypatch):
    monkeypatch.setenv("AIMHARDER_PERSON_1_EMAIL", "one@example.test")
    monkeypatch.setenv("AIMHARDER_PERSON_1_PASSWORD", "password")

    class NormalizedExactClient(FakeClient):
        def get_classes(self, target_day, family_id=None):
            return [
                {
                    "timeid": "1315_60",
                    "className": "  OPEN   BOX ",
                    "bookState": 1,
                    "id": "class-1",
                }
            ]

    result = sync_api._check_confirmed_bookings(
        config(),
        [record()],
        NOW,
        client_factory=lambda **kwargs: NormalizedExactClient(**kwargs),
    )

    assert result["checked"] == 1
    assert result["discrepancies"] == []
    assert result["unverified"] == []


def test_sync_treats_duplicate_exact_matches_as_unverified(monkeypatch):
    monkeypatch.setenv("AIMHARDER_PERSON_1_EMAIL", "one@example.test")
    monkeypatch.setenv("AIMHARDER_PERSON_1_PASSWORD", "password")

    class DuplicateClassClient(FakeClient):
        def get_classes(self, target_day, family_id=None):
            return [
                {
                    "timeid": "1315_60",
                    "className": "Open Box",
                    "bookState": 0,
                    "id": "class-1",
                },
                {
                    "timeid": "1315_45",
                    "className": "Open Box",
                    "bookState": 1,
                    "id": "class-2",
                },
            ]

    result = sync_api._check_confirmed_bookings(
        config(),
        [record()],
        NOW,
        client_factory=lambda **kwargs: DuplicateClassClient(**kwargs),
    )

    assert result["discrepancies"] == []
    assert result["unverified"][0]["reason"] == "class_not_resolved"
