"""
Calendar colour-blocking regression tests.

Locks in the rules documented in the calendar-colour golden-rules memory:
  - Only GRAPHITE (8) + LAVENDER (1)  -> non-blocking (webform: time fully available to book)
  - BASIL, PEACOCK, GRAPE, TOMATO, BANANA, any other colour, unknown -> HARD BLOCK
  - GRAPE = confirmed travel; PEACOCK = reserved; both are strictly unavailable

If somebody tightens or loosens those rules, these tests fail and force the discussion.
"""

from __future__ import annotations

import pytest

from config import (
    COLOR_BANANA,
    COLOR_BASIL,
    COLOR_GRAPE,
    COLOR_GRAPHITE,
    COLOR_LAVENDER,
    COLOR_PEACOCK,
    COLOR_TOMATO,
)
from services.calendar import conflicts as conflicts_mod
from services.calendar.list_events import (
    WEBFORM_MANUAL_SCHEDULE_BLOCKING_COLOR_IDS,
    WEBFORM_SOFT_HOLD_COLOR_IDS,
    is_webform_non_blocking_calendar_event,
)


# --- pure-function tests for the non-blocking filter ------------------------------------


def test_banana_tomato_are_disjoint_from_soft_holds() -> None:
    """Admin maintenance (Banana) and social (Tomato) must never be classed as soft holds."""
    assert not (WEBFORM_MANUAL_SCHEDULE_BLOCKING_COLOR_IDS & WEBFORM_SOFT_HOLD_COLOR_IDS)


def test_graphite_is_non_blocking(event_factory):
    ev = event_factory(color_id=COLOR_GRAPHITE, summary="Pending deposit")
    assert is_webform_non_blocking_calendar_event(ev) is True


def test_lavender_is_non_blocking(event_factory):
    ev = event_factory(color_id=COLOR_LAVENDER, summary="Travel time — pending")
    assert is_webform_non_blocking_calendar_event(ev) is True


@pytest.mark.parametrize(
    "colour",
    [COLOR_BASIL, COLOR_PEACOCK, COLOR_GRAPE, COLOR_BANANA, COLOR_TOMATO],
)
def test_blocking_colours_are_blocking(event_factory, colour):
    ev = event_factory(color_id=colour)
    assert is_webform_non_blocking_calendar_event(ev) is False


def test_grape_is_blocking_even_though_travel_text(event_factory):
    """GRAPE and LAVENDER share travel-block text; the colour ID must win over text when set."""
    ev = event_factory(color_id=COLOR_GRAPE, summary="Travel time — outbound")
    assert is_webform_non_blocking_calendar_event(ev) is False


def test_banana_maintenance_manual_entry_blocks(event_factory):
    """Yellow maintenance blocks the public webform the same as a booking."""
    ev = event_factory(color_id=COLOR_BANANA, summary="Haircut")
    assert is_webform_non_blocking_calendar_event(ev) is False


def test_tomato_social_manual_entry_blocks(event_factory):
    """Red social / personal blocks the public webform the same as a booking."""
    ev = event_factory(color_id=COLOR_TOMATO, summary="Dinner with friend")
    assert is_webform_non_blocking_calendar_event(ev) is False


def test_missing_color_with_graphite_text_marker_is_non_blocking(event_factory):
    """When the Calendar API omits colorId (limited-ACL service accounts), fall back to text."""
    ev = event_factory(color_id=None, summary="Pending deposit — John")
    assert is_webform_non_blocking_calendar_event(ev) is True


def test_unknown_color_is_treated_as_blocking(event_factory):
    ev = event_factory(color_id="99")  # Not a mapped palette id — still hard-blocks
    assert is_webform_non_blocking_calendar_event(ev) is False


def test_banana_calendar_conflict_is_confirmed(monkeypatch, booking_details):
    """Admin/maintenance bookings hard-block the slot."""
    monkeypatch.setattr(
        "services.calendar.conflicts._query_blocking_bookings",
        lambda s, e: [{"id": 1, "client_name": "Admin", "status": "admin", "start_time": s, "end_time": e}],
    )
    result, events = conflicts_mod.check_conflict(booking_details)
    assert result == "confirmed"
    assert len(events) == 1


def test_tomato_calendar_conflict_is_confirmed(monkeypatch, booking_details):
    """Social/personal bookings hard-block the slot."""
    monkeypatch.setattr(
        "services.calendar.conflicts._query_blocking_bookings",
        lambda s, e: [{"id": 2, "client_name": "Personal", "status": "social", "start_time": s, "end_time": e}],
    )
    result, events = conflicts_mod.check_conflict(booking_details)
    assert result == "confirmed"
    assert len(events) == 1


# --- check_conflict integration with DB-backed mocking ---


@pytest.fixture
def patch_db_blocking(monkeypatch):
    """Returns a setter that controls what _query_blocking_bookings returns."""
    rows_holder = {"rows": []}

    monkeypatch.setattr(
        "services.calendar.conflicts._query_blocking_bookings",
        lambda start, end: rows_holder["rows"],
    )

    def set_rows(rows):
        rows_holder["rows"] = rows

    return set_rows


def test_no_events_means_available(patch_db_blocking, booking_details):
    patch_db_blocking([])
    result, events = conflicts_mod.check_conflict(booking_details)
    assert result == "none"
    assert events == []


def test_basil_event_is_confirmed_conflict(patch_db_blocking, booking_details):
    """Confirmed booking blocks the slot."""
    patch_db_blocking([{"id": 1, "status": "confirmed"}])
    result, events = conflicts_mod.check_conflict(booking_details)
    assert result == "confirmed"
    assert len(events) == 1


def test_peacock_event_is_peacock_conflict(patch_db_blocking, booking_details):
    """Reserved booking returns 'peacock' (soft block)."""
    patch_db_blocking([{"id": 2, "status": "reserved"}])
    result, events = conflicts_mod.check_conflict(booking_details)
    assert result == "peacock"
    assert len(events) == 1


def test_grape_event_is_confirmed_conflict(patch_db_blocking, booking_details):
    """Travel booking for a confirmed client is a hard block."""
    patch_db_blocking([{"id": 3, "status": "travel"}])
    result, events = conflicts_mod.check_conflict(booking_details)
    assert result == "confirmed"
    assert len(events) == 1


def test_graphite_event_does_not_block(patch_db_blocking, booking_details):
    """Pending-deposit slots are not in the DB as blocking rows."""
    patch_db_blocking([])  # graphite = non-blocking = not returned by query
    result, events = conflicts_mod.check_conflict(booking_details)
    assert result == "none"
    assert events == []


def test_lavender_event_does_not_block(patch_db_blocking, booking_details):
    """Pending travel hold slots are not in the DB as blocking rows."""
    patch_db_blocking([])
    result, events = conflicts_mod.check_conflict(booking_details)
    assert result == "none"
    assert events == []


def test_mixed_basil_and_graphite_reports_confirmed(patch_db_blocking, booking_details):
    """Only confirmed rows come back from the query; graphite stays out."""
    patch_db_blocking([{"id": 1, "status": "confirmed"}])
    result, events = conflicts_mod.check_conflict(booking_details)
    assert result == "confirmed"
    assert len(events) == 1


def test_mixed_peacock_and_lavender_reports_peacock(patch_db_blocking, booking_details):
    """Only reserved rows come back; lavender (non-blocking) never enters DB query."""
    patch_db_blocking([{"id": 2, "status": "reserved"}])
    result, events = conflicts_mod.check_conflict(booking_details)
    assert result == "peacock"
    assert len(events) == 1


def test_unavailable_db_returns_none(monkeypatch, booking_details):
    """When the DB is unavailable, _query_blocking_bookings returns [] and we report 'none'."""
    monkeypatch.setattr(
        "services.calendar.conflicts._query_blocking_bookings",
        lambda s, e: [],
    )
    result, events = conflicts_mod.check_conflict(booking_details)
    assert result == "none"
    assert events == []
