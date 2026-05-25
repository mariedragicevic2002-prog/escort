"""Webform slot blocking: list_events heuristics, outcall cached travel check, booked-times fail-closed."""

from __future__ import annotations

from datetime import date, datetime, timezone, timedelta
from unittest.mock import patch

import config
from admin.blueprints.booking.helpers import (
    adjust_webform_date_str_for_overnight_time,
    calendar_date_for_overnight_slot,
    _parse_available_hours,
)
from services.calendar.conflicts import check_outcall_conflict_from_cached_events
from services.calendar.list_events import is_webform_non_blocking_calendar_event

_TZ10 = timezone(timedelta(hours=10))


def test_parse_available_hours_matches_admin_schedule_formats() -> None:
    """Webform accepts same hour strings as schedule/admin (must not silently use 3pm-3am)."""
    assert _parse_available_hours("11am-4am, 7 days a week") == ("11:00", "04:00")
    assert _parse_available_hours("11 am - 4 am, Monday-Sunday") == ("11:00", "04:00")
    assert _parse_available_hours("15:00 - 03:00") == ("15:00", "03:00")
    assert _parse_available_hours("15:00-3am") == ("15:00", "03:00")
    assert _parse_available_hours("24/7, 7 days a week") == ("00:00", "23:45")


def test_parse_available_hours_full_day_when_unparseable_not_three_pm_default() -> None:
    assert _parse_available_hours("not a real window xyz") == ("00:00", "23:45")


def test_calendar_date_overnight_post_midnight_next_civil_day() -> None:
    d0 = date(2026, 5, 4)
    assert calendar_date_for_overnight_slot(d0, 1, 0, "15:00", "03:00") == date(2026, 5, 5)
    assert calendar_date_for_overnight_slot(d0, 3, 0, "15:00", "03:00") == date(2026, 5, 5)


def test_calendar_date_overnight_same_day_evening() -> None:
    d0 = date(2026, 5, 4)
    assert calendar_date_for_overnight_slot(d0, 16, 0, "15:00", "03:00") == d0
    assert calendar_date_for_overnight_slot(d0, 23, 45, "15:00", "03:00") == d0


def test_adjust_webform_date_str_skips_dinner_date() -> None:
    assert (
        adjust_webform_date_str_for_overnight_time(
            "2026-05-04",
            "01:00",
            "15:00-3am",
            experience="Dinner Date",
        )
        == "2026-05-04"
    )


def test_adjust_webform_date_str_advances_overnight_tail() -> None:
    assert (
        adjust_webform_date_str_for_overnight_time(
            "2026-05-04",
            "01:30",
            "3pm-3am, 7 days a week",
            experience="GFE",
        )
        == "2026-05-05"
    )


def test_travel_event_without_soft_hold_is_blocking_in_webform() -> None:
    """Missing colorId + travel title must not be treated as soft-hold without the marker (GRAPE / confirmed)."""
    ev = {
        "summary": "Travel time — to client (escort - client)",
        "description": (
            "TRAVEL TIME — calendar block, drive-time reserved\n"
            "No adella soft-hold line here for confirmed travel.\n"
        ),
        "start": {"dateTime": "2026-05-01T12:15:00+10:00"},
        "end": {"dateTime": "2026-05-01T12:30:00+10:00"},
    }
    assert is_webform_non_blocking_calendar_event(ev) is False


def test_lavender_pending_with_soft_hold_marker_is_non_blocking() -> None:
    mark = config.ADELLA_CALENDAR_SOFT_HOLD_MARKER
    ev = {
        "summary": "Travel time — to client (escort - client)",
        "description": f"TRavel body\n\n{mark}",
        "start": {"dateTime": "2026-05-01T12:15:00+10:00"},
        "end": {"dateTime": "2026-05-01T12:30:00+10:00"},
    }
    assert is_webform_non_blocking_calendar_event(ev) is True


def test_check_outcall_from_cached_peacock_blocks_as_confirmed() -> None:
    """Reserved (PEACOCK) outcall holds must block the slot like BASIL (single 'confirmed' bucket)."""
    peacock = {
        "status": "reserved",
        "start_time": datetime(2026, 5, 20, 14, 0, tzinfo=_TZ10),
        "end_time": datetime(2026, 5, 20, 15, 0, tzinfo=_TZ10),
    }
    details = {
        "date": date(2026, 5, 20),
        "time": (14, 0),
        "duration": 60,
        "incall_outcall": "outcall",
        "outcall_address": "100 King William St, Adelaide SA 5000",
    }
    ct, evs = check_outcall_conflict_from_cached_events([peacock], details)
    assert ct == "confirmed"
    assert len(evs) == 1


def test_check_outcall_from_cached_finds_basil_in_extended_window() -> None:
    """Proposed outcall 12:00-13:00 with default travel margins still intersects an 11:15-12:15 BASIL block."""
    basil = {
        "status": "confirmed",
        "start_time": datetime(2026, 5, 15, 11, 15, tzinfo=_TZ10),
        "end_time": datetime(2026, 5, 15, 12, 15, tzinfo=_TZ10),
    }
    details = {
        "date": date(2026, 5, 15),
        "time": (12, 0),
        "duration": 60,
        "incall_outcall": "outcall",
        "outcall_address": "100 King William St, Adelaide SA 5000",
    }
    ct, _ = check_outcall_conflict_from_cached_events([basil], details)
    assert ct == "confirmed"


def test_get_booked_times_with_fallback_fails_closed_when_no_calendar() -> None:
    from admin.blueprints.booking import api_booked_times as m

    with patch("services.calendar_service.get_calendar_service", return_value=None):
        booked, err = m._get_booked_times_for_date_with_fallback(
            date(2026, 6, 1),
            60,
            "GFE",
            True,
            "Somewhere 5000",
            "15:00",
            "03:00",
        )
    assert err is not None
    # Unique HH:MM keys (15-min grid may dedupe at midnight wrap)
    assert len(booked) >= 90
    assert "12:00" in booked and "00:00" in booked
