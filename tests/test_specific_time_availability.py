"""Regression tests for specific-time inference (✅/❌ vs generic slot dumps)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytz

from handlers.new_conv._shared import _new_booking_first_contact
from tests.scenarios.utils import build_context, scenario_state_manager
from utils.time_parser import infer_requested_datetime_for_booking, message_has_explicit_clock

PHONE = "+61400999334"


def test_message_has_explicit_clock_colon_without_ampm():
    assert message_has_explicit_clock("tonight at 1:45")
    assert message_has_explicit_clock("see you at 1:45am")
    assert message_has_explicit_clock("could i book you at 330")
    assert message_has_explicit_clock("book at 3")
    assert not message_has_explicit_clock("free tonight?")
    assert not message_has_explicit_clock("tonight at 11")


def test_infer_colonless_hhmm_nearest_forward(monkeypatch):
    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 5, 5, 2, 52, 0))
    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: frozen)

    dt = infer_requested_datetime_for_booking(
        "Hi its Tom i was wondering if i could book you at 330",
        now=frozen,
    )
    assert dt is not None
    assert dt.date() == frozen.date()
    assert dt.hour == 3 and dt.minute == 30


def test_infer_bare_hour_at_nearest_forward_wee_hours(monkeypatch):
    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 5, 5, 2, 52, 0))
    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: frozen)

    dt = infer_requested_datetime_for_booking("could i book you at 3", now=frozen)
    assert dt is not None
    assert dt.date() == frozen.date()
    assert dt.hour == 3 and dt.minute == 0


def test_infer_bare_hour_at_3_nearest_afternoon(monkeypatch):
    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 5, 5, 14, 0, 0))
    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: frozen)

    dt = infer_requested_datetime_for_booking("book at 3", now=frozen)
    assert dt is not None
    assert dt.hour == 15 and dt.minute == 0


def test_infer_explicit_ampm_same_hour_future_minutes_same_calendar_day(monkeypatch):
    """1:30am now + '1:45am' must not roll to tomorrow (regression: minute > 0 heuristic)."""
    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 5, 5, 1, 30, 0))
    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: frozen)

    dt = infer_requested_datetime_for_booking(
        "Could I come see you at 1:45am",
        now=frozen,
    )
    assert dt is not None
    assert dt.date() == frozen.date()
    assert dt.hour == 1 and dt.minute == 45


def test_infer_tomorrow_at_3am_wee_hours_next_calendar_day(monkeypatch):
    """03:27 Wed + 'tomorrow at 3am' → Thu 03:00 (couples/outcall regression)."""
    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 5, 6, 3, 27, 11))
    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: frozen)

    dt = infer_requested_datetime_for_booking(
        "Hi my boyfriend and I want to book you for a couples booking tomorrow at 3am",
        now=frozen,
    )
    assert dt is not None
    assert dt.date() == frozen.date() + timedelta(days=1)
    assert dt.hour == 3 and dt.minute == 0


def test_infer_tomorrow_at_8pm_wee_hours_same_calendar_evening(monkeypatch):
    """Before 4am, 'tomorrow at 8pm' still means same calendar evening window."""
    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 5, 6, 3, 27, 11))
    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: frozen)

    dt = infer_requested_datetime_for_booking(
        "couples booking tomorrow at 8pm thanks",
        now=frozen,
    )
    assert dt is not None
    assert dt.date() == frozen.date()
    assert dt.hour == 20 and dt.minute == 0


def test_infer_explicit_ampm_same_hour_already_passed_rolls_forward(monkeypatch):
    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 5, 5, 1, 50, 0))
    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: frozen)

    dt = infer_requested_datetime_for_booking("at 1:45am", now=frozen)
    assert dt is not None
    assert dt.date() > frozen.date()


def test_infer_tonight_at_145_late_night_returns_datetime(monkeypatch):
    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 5, 5, 1, 30, 0))
    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: frozen)

    dt = infer_requested_datetime_for_booking(
        "Hi could I come see you tonight at 1:45",
        now=frozen,
    )
    assert dt is not None
    assert dt.hour == 1 and dt.minute == 45


def test_infer_tonight_bare_hour_late_night_returns_none(monkeypatch):
    """Vague 'tonight at 11' (no :MM, no am/pm) keeps legacy slot-list behaviour."""
    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 5, 5, 1, 30, 0))
    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: frozen)

    dt = infer_requested_datetime_for_booking("free tonight at 11?", now=frozen)
    assert dt is None


def test_infer_duration_phrase_not_treated_as_time(monkeypatch):
    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 5, 5, 14, 0, 0))
    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: frozen)

    dt = infer_requested_datetime_for_booking("how much for 1 hour", now=frozen)
    assert dt is None


def test_first_contact_colon_time_yes_line_not_generic_slot_dump(monkeypatch):
    from unittest.mock import patch

    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 5, 5, 1, 30, 0))
    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: frozen)

    sm = scenario_state_manager(PHONE, current_state="NEW")
    ctx = build_context(
        phone_number=PHONE,
        message="Hi could I come see you tonight at 1:45",
        state_manager=sm,
    )

    with patch("handlers.new_conv._shared._build_outside_hours_response", return_value=None):
        with patch("handlers.booking_collection.check_and_format_outside_hours") as m_hours:
            m_hours.return_value = (True, "", "11AM-4AM", "7 days a week")
            with patch("services.calendar_service.check_conflict") as m_cc:
                m_cc.return_value = ("none", None)
                result = _new_booking_first_contact(ctx)

    msg = "\n".join(result.get("messages") or [])
    assert "Here are the times I have available" not in msg
    assert "✅" in msg or "yes " in msg.lower()


def test_first_contact_outside_hours_cross_and_alternatives(monkeypatch):
    from unittest.mock import patch

    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 5, 5, 14, 0, 0))
    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: frozen)

    sm = scenario_state_manager(PHONE, current_state="NEW")
    ctx = build_context(
        phone_number=PHONE,
        message="Book me tonight at 2:30",
        state_manager=sm,
    )

    alt_a = tz.localize(datetime(2026, 5, 5, 21, 0, 0))
    alt_b = tz.localize(datetime(2026, 5, 5, 22, 0, 0))
    alt_c = tz.localize(datetime(2026, 5, 5, 23, 0, 0))

    with patch("handlers.new_conv._shared._build_outside_hours_response", return_value=None):
        with patch("handlers.booking_collection.check_and_format_outside_hours") as m_hours:
            m_hours.return_value = (False, "", "11AM-4AM", "7 days a week")
            with patch("services.calendar_service.find_alternative_slots") as m_fas:
                m_fas.return_value = [alt_a, alt_b, alt_c]
                result = _new_booking_first_contact(ctx)

    msg = "\n".join(result.get("messages") or [])
    assert "❌" in msg
    assert "my hours are" in msg.lower()
    assert "11AM-4AM" in msg

