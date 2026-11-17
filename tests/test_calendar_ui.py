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


def test_monthly_quota_prefers_verified_live_and_retains_manual_fallback():
    script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
    assert "state.liveCredits?.month===monthKey" in script
    assert "b?.verified===true" in script
    assert "b.consumed+b.upcoming_reserved===b.committed" in script
    assert "b.committed+b.remaining===b.total" in script
    assert 'if(valid) return {used:b.consumed,limit:b.total,source:"live"' in script
    assert "return {used,limit,source:" in script
    assert 'Number.isInteger(used)?"manual":"unknown"};' in script
    assert 'quota("classes",member.classes)' in script
    assert 'quota("open_box",member.openBox)' in script


def test_simple_mode_keeps_credits_booking_and_calendar():
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    assert 'id="dashboard" data-view="simple"' in html
    assert 'id="quota-summary"' in html
    assert 'id="booking-form"' in html
    assert 'id="calendar-panel"' in html
    assert 'id="toggle-detail"' in html
    assert 'data-view="simple"' in css
    assert "#monthly-credits-form" in css
    assert "setView(savedDetailed)" in script
    assert 'setAgendaView(state.detailed?"history":"calendar")' in script


def test_dual_booking_uses_one_request_per_member_with_distinct_keys():
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
    assert '<option value="both">Joan y Paula</option>' in html
    assert '["person_1","person_2"]' in script
    assert "state.submissionKeys[person_id]" in script
    assert "failures.push({person_id,message:err.message})" in script
    assert "Al editar una reserva, selecciona solo a Joan o Paula." in script


def test_environment_is_reported_by_server_not_hardcoded():
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
    assert "PREVIEW · SIMULACIÓN" not in html
    assert "Las reservas reales están desactivadas" not in html
    assert 'id="environment-indicator"' in html
    assert 'id="booking-mode-title"' in html
    assert "sessionInfo.booking_mode" in script
    assert "bookingData.booking_mode" in script


def test_compact_credits_appear_in_simple_view_with_manual_fallback():
    script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    assert 'compactQuota("Clases",member.classesQuota)' in script
    assert 'compactQuota("Open Box",member.openBoxQuota)' in script
    assert "quota.free" in script
    assert "quota.upcoming" in script
    assert "Respaldo manual · libres sin verificar" in script
    assert "renderQuotaWarnings();" in script
    assert ".quota-compact" in css
    assert "grid-template-columns:1fr;gap:7px" in css
    assert " .quota-intro{display:none}" in css


def test_compact_monthly_credits_show_committed_over_total_and_free():
    script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    assert "quota.committed+'/'+quota.limit" in script
    assert "quota.free+' libres" in script
    assert "Contraseña de acceso" in html
    assert 'placeholder="FITBOT_ADMIN_TOKEN"' not in html
