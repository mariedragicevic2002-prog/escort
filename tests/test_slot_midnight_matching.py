"""Slot selection: midnight wording must pair hour 0 with the correct calendar day."""

from handlers.booking_coll._shared import _match_slot_selection


def test_midnight_phrase_matches_offered_hour_zero_with_address_same_message() -> None:
    """Regression: 'Midnight … hotel' must bind slot 0 so date comes from offered_slot_dates."""
    offered = [23, 0, 1]
    msg = "Midnight im located at the pan pacific hotel"
    assert _match_slot_selection(msg, offered) == 0


def test_midnight_not_offered_returns_none() -> None:
    assert _match_slot_selection("midnight works", [22, 23]) is None


def test_not_midnight_skips_hour_zero_prefers_explicit_pm() -> None:
    offered = [23, 0, 1]
    assert _match_slot_selection("not midnight, the 11pm one", offered) == 23


def test_colonless_hhmm_picks_closest_offered_slot(monkeypatch) -> None:
    """Regression: '430' must not match bare '43'; bind to 4:30pm slot (hour 16)."""
    from datetime import datetime

    import pytz

    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 5, 5, 11, 25, 0))
    monkeypatch.setattr("handlers.booking_coll._shared.get_current_datetime", lambda: frozen)

    offered_hours = [15, 16, 17]
    offered_minutes = [30, 30, 30]
    assert (
        _match_slot_selection(
            "430",
            offered_hours,
            offered_minutes=offered_minutes,
            offered_date="2026-05-05",
            now=frozen,
        )
        == 16
    )


def test_bare_digit_hour_matches_when_offered_hours_are_strings() -> None:
    """JSON/DB often yields string hours; bare '7' must still resolve to 19 when 19 is offered."""
    assert _match_slot_selection("7", ["19", "20", "1"]) == 19
    assert _match_slot_selection("7pm", ["19", "20"]) == 19
