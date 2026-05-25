"""Operating window: overnight grace, morning-tail weekday attribution."""

from __future__ import annotations

from datetime import date, time

from handlers.booking_coll._shared import (
    OPERATING_HOURS_END_BUFFER_MINUTES,
    check_within_available_hours_and_days,
)


def test_overnight_shift_last_start_is_thirty_minutes_before_configured_end():
    ah = "1pm-4am, Thursday-Sunday"
    # Thursday — morning tail; 4:00am end → last start 3:30am
    ok, _reason = check_within_available_hours_and_days(
        date(2026, 5, 7),
        (3, 30),
        ah,
        "7 days a week",
    )
    assert ok is True

    ok_late_minute, _ = check_within_available_hours_and_days(
        date(2026, 5, 7),
        (3, 31),
        ah,
        "7 days a week",
    )
    assert ok_late_minute is False

    ok_exact_end, _ = check_within_available_hours_and_days(
        date(2026, 5, 7),
        (4, 0),
        ah,
        "7 days a week",
    )
    assert ok_exact_end is False

    ok_late, _ = check_within_available_hours_and_days(
        date(2026, 5, 7),
        (4, 35),
        ah,
        "7 days a week",
    )
    assert ok_late is False


def test_monday_early_am_counts_previous_sunday_for_thu_sun_schedule():
    ah = "1pm-4am, Thursday-Sunday"
    # Monday 4 May 2026 — morning tail; Sunday was a working day
    ok, reason = check_within_available_hours_and_days(
        date(2026, 5, 4),
        (3, 15),
        ah,
        "7 days a week",
    )
    assert ok is True, reason


def test_tuesday_early_am_still_outside_for_thu_sun_only():
    ah = "1pm-4am, Thursday-Sunday"
    ok, reason = check_within_available_hours_and_days(
        date(2026, 5, 5),
        (3, 0),
        ah,
        "7 days a week",
    )
    assert ok is False
    assert reason == "outside available days"


def test_end_buffer_constant_is_thirty_minutes():
    assert OPERATING_HOURS_END_BUFFER_MINUTES == 30


def test_1pm_to_430am_accepts_7pm_and_1am_rejects_5am():
    """Regression: production doubles transcript — overnight span must accept evening + morning-tail slots."""
    ah = "1pm-4:30am"
    ad = "7 days a week"
    d = date(2026, 5, 5)

    ok_eve, reason_eve = check_within_available_hours_and_days(d, (19, 0), ah, ad)
    assert ok_eve is True
    assert reason_eve == "available"

    ok_am, reason_am = check_within_available_hours_and_days(d, (1, 0), ah, ad)
    assert ok_am is True
    assert reason_am == "available"

    ok_late, reason_late = check_within_available_hours_and_days(d, (5, 0), ah, ad)
    assert ok_late is False
    assert reason_late == "outside available hours"


def test_datetime_time_from_postgresql_accepted_not_time_unparseable():
    """Booking ``time`` loaded from PG TIME / state round-trip must not fail the hours gate."""
    ah = "1pm-4:30am"
    ad = "7 days a week"
    d = date(2026, 5, 5)
    ok, reason = check_within_available_hours_and_days(d, time(2, 0), ah, ad)
    assert ok is True
    assert reason == "available"
