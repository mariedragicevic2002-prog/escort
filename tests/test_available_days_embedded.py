"""Regression: admin dashboard embeds working days after the comma in ``available_hours``."""

from __future__ import annotations

from datetime import date, datetime

import pytest


def test_resolve_prefers_comma_suffix_after_hours():
    from handlers.booking_coll._shared import resolve_available_days_for_checks

    assert resolve_available_days_for_checks(
        "11am-4am, Monday only",
        "7 days a week",
    ) == "Monday only"


def test_resolve_falls_back_when_hours_have_no_day_suffix():
    from handlers.booking_coll._shared import resolve_available_days_for_checks

    assert resolve_available_days_for_checks(
        "11am-4am",
        "Wednesday",
    ) == "Wednesday"


def test_check_within_hours_uses_embedded_days_not_legacy_setting():
    from handlers.booking_coll._shared import check_within_available_hours_and_days

    # Tuesday 5 May 2026
    tuesday = date(2026, 5, 5)
    ok, reason = check_within_available_hours_and_days(
        tuesday,
        (14, 0),
        "11am-4am, Monday",
        "7 days a week",
    )
    assert ok is False
    assert reason == "outside available days"


@pytest.fixture
def clear_hours_cache(monkeypatch):
    import utils.availability_slots as avs

    avs._HOURS_CACHE_KEY = None
    avs._HOURS_CACHE = None
    yield
    avs._HOURS_CACHE_KEY = None
    avs._HOURS_CACHE = None


def test_slots_skip_calendar_days_not_in_embedded_schedule(monkeypatch, clear_hours_cache):
    import utils.availability_slots as avs

    def _fake_get(k: str, default=None):
        return {
            "available_hours": "11am-4am, Monday",
            "available_days": "7 days a week",
        }.get(k, default)

    monkeypatch.setattr("core.settings_manager.get_setting", _fake_get)

    # Tuesday afternoon — not a working day per embedded suffix
    now = datetime(2026, 5, 5, 14, 0, 0)
    slots = avs.get_next_available_time_slots(
        now,
        num_slots=2,
        check_calendar=False,
    )
    assert slots
    for dt, _label in slots:
        assert dt.weekday() == 0, f"expected Monday-only slots, got {dt} ({_label})"
