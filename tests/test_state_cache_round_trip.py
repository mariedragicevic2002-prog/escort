"""
State cache round-trip type-fidelity tests.

``state_cache.set_cached_state`` serialises via ``json.dumps(default=str)``,
converting Python ``date``, ``time``, and ``datetime`` objects to ISO strings.
``state_manager._hydrate_state_record`` is responsible for re-parsing those
strings back to native types on cache hit.

These tests assert that the *full* round-trip:

    dict with typed values
      → json.dumps(default=str)
      → json.loads
      → _hydrate_state_record()
      → dict with typed values

preserves types for every known date/time field.  A regression here would
mean the application silently receives strings instead of date objects, causing
hard-to-debug bugs in calendar / scheduling logic.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timezone

from core.state_manager import _hydrate_state_record  # noqa: PLC2701  # testing internal


def _round_trip(state: dict) -> dict:
    """Simulate cache set → get: json dumps with default=str, then loads, then hydrate."""
    serialised = json.dumps(state, default=str)
    deserialised = json.loads(serialised)
    hydrated = _hydrate_state_record(deserialised)
    assert hydrated is not None, "hydrate returned None for a non-empty dict"
    return hydrated


def test_date_field_round_trips_to_date():
    state = {"current_state": "awaiting_confirmation", "date": date(2026, 7, 15)}
    result = _round_trip(state)
    assert isinstance(result["date"], date), f"expected date, got {type(result['date'])}"
    assert result["date"] == date(2026, 7, 15)


def test_time_field_round_trips_to_time():
    state = {"current_state": "confirmed", "time": time(19, 30)}
    result = _round_trip(state)
    assert isinstance(result["time"], time), f"expected time, got {type(result['time'])}"
    assert result["time"] == time(19, 30)


def test_datetime_fields_round_trip_to_datetime():
    now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)
    state = {
        "current_state": "confirmed",
        "created_at": now,
        "updated_at": now,
        "last_message_at": now,
        "confirmed_at": now,
        "deposit_requested_at": now,
    }
    result = _round_trip(state)
    for field in ("created_at", "updated_at", "last_message_at", "confirmed_at", "deposit_requested_at"):
        assert isinstance(result[field], datetime), f"{field}: expected datetime, got {type(result[field])}"
        assert result[field].year == 2026


def test_combined_date_time_and_datetime_round_trip():
    state = {
        "current_state": "confirmed",
        "date": date(2026, 8, 1),
        "time": time(15, 0),
        "created_at": datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
        "confirmed_at": datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc),
        "phone_number": "+61400123456",
        "duration_minutes": 60,
        "experience_type": "GFE",
    }
    result = _round_trip(state)
    assert isinstance(result["date"], date)
    assert isinstance(result["time"], time)
    assert isinstance(result["created_at"], datetime)
    assert isinstance(result["confirmed_at"], datetime)
    # Non-temporal fields survive unchanged
    assert result["duration_minutes"] == 60
    assert result["experience_type"] == "GFE"


def test_missing_date_time_fields_leave_state_intact():
    """A state with no date/time fields must not be mangled by hydration."""
    state = {"current_state": "NEW", "phone_number": "+61400000000"}
    result = _round_trip(state)
    assert result["current_state"] == "NEW"
    assert "date" not in result
    assert "time" not in result


def test_hydrate_adds_default_current_state_when_missing():
    """Ensure hydrate inserts 'NEW' when current_state key is absent."""
    result = _round_trip({"phone_number": "+611234"})
    assert result.get("current_state") == "NEW"
