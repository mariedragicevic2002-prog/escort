from __future__ import annotations

from datetime import date

from handlers.confirmed_booking import handle_provide_field, handle_rate_negotiation
from tests.scenarios.utils import build_context, scenario_state_manager

PHONE = "+61400999336"


def _confirmed_ctx(message: str):
    sm = scenario_state_manager(
        PHONE,
        current_state="CONFIRMED",
        date=date(2026, 6, 3),
        time=(15, 0),
        duration=60,
        client_name="Alex",
    )
    return sm, build_context(phone_number=PHONE, message=message, state_manager=sm)


def test_confirmed_short_low_signal_message_uses_deterministic_reply():
    _sm, ctx = _confirmed_ctx("k")
    result = handle_provide_field(ctx)
    assert "booking is still confirmed" in (result["messages"][0] or "").lower()
    assert result["new_state"] is None


def test_confirmed_are_you_real_uses_deterministic_reply():
    _sm, ctx = _confirmed_ctx("are you real?")
    result = handle_provide_field(ctx)
    assert "actively monitored" in (result["messages"][0] or "").lower()
    assert result["new_state"] is None


def test_confirmed_rate_negotiation_uses_fixed_rates_copy():
    _sm, ctx = _confirmed_ctx("can you do cheaper?")
    result = handle_rate_negotiation(ctx)
    assert "rates are set" in (result["messages"][0] or "").lower()
    assert "unable to negotiate" in (result["messages"][0] or "").lower()


def test_confirmed_provide_field_routes_cheaper_to_rate_negotiation():
    _sm, ctx = _confirmed_ctx("can you do cheaper please")
    result = handle_provide_field(ctx)
    assert "unable to negotiate" in (result["messages"][0] or "").lower()
    assert result["new_state"] is None


def test_confirmed_provide_field_routes_another_booking_to_collecting():
    _sm, ctx = _confirmed_ctx("i want another booking tomorrow")
    result = handle_provide_field(ctx)
    assert "another booking" in (result["messages"][0] or "").lower()
    assert result["new_state"] == "COLLECTING"


def test_confirmed_non_latin_message_gets_english_guidance():
    _sm, ctx = _confirmed_ctx("こんにちは")
    result = handle_provide_field(ctx)
    assert "english" in (result["messages"][0] or "").lower()
    assert result["new_state"] is None


def test_confirmed_reschedule_intent_triggers_handler():
    _sm, ctx = _confirmed_ctx("Can I change to midnight instead?")
    from handlers.confirmed_booking import handle_reschedule
    result = handle_reschedule(ctx)
    assert "what date and time works better" in (result["messages"][0] or "").lower()
    assert result["new_state"] == "COLLECTING"


def test_confirmed_reschedule_regex_variants():
    from handlers.confirmed_booking import handle_reschedule
    for msg in [
        "Can you reschedule my booking?",
        "Move it to 10pm please",
        "Change to a different time",
        "I'd like a different day",
    ]:
        _sm, ctx = _confirmed_ctx(msg)
        result = handle_reschedule(ctx)
        assert result["new_state"] == "COLLECTING"
        assert "what date and time works better" in (result["messages"][0] or "").lower()
