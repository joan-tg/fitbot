"""Regression checks for the calendar navigation and grouped event markup."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def require_static_assets():
    if not (ROOT / "public" / "index.html").exists():
        pytest.skip("Frontend assets are excluded from the Docker test image")


def test_both_agenda_panels_have_expected_ids():
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    assert 'id="calendar-panel"' in html
    assert 'id="history-panel"' in html
    assert 'id="show-calendar"' in html
    assert 'id="show-history"' in html


def test_calendar_groups_matching_classes_without_collapsing_history():
    script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
    assert "groupCalendarEvents(monthRecords)" in script
    assert "JSON.stringify([item.date,item.time,item.name])" in script
    assert "group.participants.includes(item.person_id)" in script
    assert "calendarPeople(item)" in script
    assert "calendarEventStatus(item)" in script
    assert "const filtered=all.filter(b=>" in script
    assert 'setAgendaView("calendar")' in script
    assert 'setAgendaView("history")' in script
