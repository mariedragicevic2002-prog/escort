"""
Regression simulations for multi-turn booking edges (no Twilio / no live DB).

Runs in CI like normal unit tests; extend this module when manual SMS simulations find bugs.
"""

from __future__ import annotations

from datetime import date, datetime

import pytz

from handlers.availability_check import handle_check_availability
from templates.enquiry_templates import get_fifth_message_block
from tests.scenarios.utils import build_context, scenario_state_manager

PHONE = "+61400999333"


def test_sim_fifth_message_recovery_copy_mentions_date_time_duration():
    body = get_fifth_message_block().lower()
    assert "date" in body and "time" in body
    assert "pausing" in body or "pause" in body


def test_sim_outcall_pivot_clears_awaiting_yes_flags():
    from unittest.mock import patch

    sm = scenario_state_manager(
        PHONE,
        current_state="CHECKING_AVAILABILITY",
        date=date(2026, 6, 2),
        time=(14, 0),
        duration=60,
        incall_outcall="incall",
        incall_awaiting_yes=True,
        outcall_awaiting_yes=False,
        experience_type="GFE",
        client_name="Alex",
        available_now_requested=False,
    )
    ctx = build_context(
        phone_number=PHONE,
        message="come to my hotel",
        state_manager=sm,
    )

    def _stub_provide_field(_c):
        return {"messages": ["stub_collect"], "new_state": "COLLECTING", "actions": []}

    with patch("handlers.booking_collection.handle_provide_field", _stub_provide_field):
        result = handle_check_availability(ctx)

    assert result.get("messages") == ["stub_collect"]
    st = sm.get_state(PHONE)
    assert st["incall_outcall"] == "outcall"
    assert st.get("outcall_address") is None
    assert st.get("incall_awaiting_yes") is False
    assert st.get("outcall_awaiting_yes") is False


def test_sim_lead_time_within_ten_minutes_copy(monkeypatch):
    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 6, 2, 13, 45, 0))
    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: frozen)

    sm = scenario_state_manager(
        PHONE,
        current_state="CHECKING_AVAILABILITY",
        date=date(2026, 6, 2),
        time=(13, 52),
        duration=60,
        incall_outcall="incall",
        experience_type="GFE",
        client_name="Alex",
        available_now_requested=False,
    )
    ctx = build_context(phone_number=PHONE, message="yes", state_manager=sm)

    result = handle_check_availability(ctx)
    msg = " ".join(result.get("messages") or [])
    assert "10" in msg
    assert "breathing room" in msg.lower()
    assert result.get("new_state") == "COLLECTING"


def test_sim_new_state_confirm_without_core_booking_delegates(monkeypatch):
    """NEW misclassified as confirm_booking with no payload — recover instead of crashing."""
    monkeypatch.setattr(
        "handlers.new_conversation.handle_book_appointment",
        lambda _ctx: {"messages": ["delegated_stub"], "new_state": None, "actions": []},
    )
    sm = scenario_state_manager(PHONE, current_state="NEW", version=1)
    ctx = build_context(
        phone_number=PHONE,
        message="#CONF-993322 already confirmed yeah?",
        state_manager=sm,
    )
    result = handle_check_availability(ctx)
    assert result.get("messages") == ["delegated_stub"]


def test_sim_checking_yes_with_cancel_phrase_aborts_booking():
    """'yes' plus abort wording must not run YES-finalisation."""
    from templates.booking_collection_messages import BOOKING_CANCELLED_NO_PROBLEM

    sm = scenario_state_manager(
        PHONE,
        current_state="CHECKING_AVAILABILITY",
        date=date(2026, 6, 2),
        time=(14, 0),
        duration=60,
        incall_outcall="incall",
        incall_awaiting_yes=True,
        experience_type="GFE",
        client_name="Alex",
        available_now_requested=False,
    )
    ctx = build_context(
        phone_number=PHONE,
        message="yes but actually cancel the whole thing",
        state_manager=sm,
    )
    result = handle_check_availability(ctx)
    assert result.get("messages") == [BOOKING_CANCELLED_NO_PROBLEM]
    assert result.get("new_state") == "NEW"
    assert sm.get_state(PHONE)["current_state"] == "NEW"


def test_sim_start_time_in_past_copy(monkeypatch):
    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 6, 2, 13, 45, 0))
    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: frozen)

    sm = scenario_state_manager(
        PHONE,
        current_state="CHECKING_AVAILABILITY",
        date=date(2026, 6, 2),
        time=(13, 30),
        duration=60,
        incall_outcall="incall",
        experience_type="GFE",
        client_name="Alex",
        available_now_requested=False,
    )
    ctx = build_context(phone_number=PHONE, message="confirm", state_manager=sm)

    result = handle_check_availability(ctx)
    msg = " ".join(result.get("messages") or []).lower()
    assert "passed" in msg
    assert result.get("new_state") == "COLLECTING"


def test_sim_checking_multi_slot_hold_request_routes_to_collecting():
    sm = scenario_state_manager(
        PHONE,
        current_state="CHECKING_AVAILABILITY",
        date=date(2026, 6, 2),
        time=(14, 0),
        duration=60,
        incall_outcall="incall",
        incall_awaiting_yes=True,
        experience_type="GFE",
        client_name="Alex",
        available_now_requested=False,
    )
    ctx = build_context(
        phone_number=PHONE,
        message="can you hold both 8pm and 9pm",
        state_manager=sm,
    )
    result = handle_check_availability(ctx)
    assert result.get("new_state") == "COLLECTING"
    assert "only hold one time" in " ".join(result.get("messages") or []).lower()
    st = sm.get_state(PHONE)
    assert st.get("incall_awaiting_yes") is False
    assert st.get("outcall_awaiting_yes") is False


def test_sim_outcall_yes_recovers_mandatory_deposit_when_flag_is_missing(monkeypatch):
    sm = scenario_state_manager(
        PHONE,
        current_state="CHECKING_AVAILABILITY",
        date=date(2099, 1, 1),
        time=(14, 0),
        duration=60,
        incall_outcall="outcall",
        outcall_address="The Sofitel",
        outcall_awaiting_yes=True,
        incall_awaiting_yes=False,
        deposit_required=False,
        experience_type="PSE",
        client_name="Tony",
        available_now_requested=False,
    )
    ctx = build_context(phone_number=PHONE, message="YES", state_manager=sm)

    monkeypatch.setattr(
        "booking.deposit_handler.calculate_deposit_requirement",
        lambda *_a, **_k: (True, 100, "outcall"),
    )
    monkeypatch.setattr(
        "booking.field_validator.FieldValidator.validate_outcall_address",
        lambda *_a, **_k: (True, ""),
    )
    monkeypatch.setattr(
        "handlers.availability_parts.availability_check_impl._acquire_booking_lock",
        lambda *_a, **_k: object(),
    )
    monkeypatch.setattr(
        "handlers.availability_parts.availability_check_impl._release_booking_lock",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "services.calendar_service.check_outcall_conflict_with_travel",
        lambda *_a, **_k: ("none", []),
    )
    monkeypatch.setattr(
        "services.calendar_service.create_calendar_event",
        lambda *_a, **_k: {
            "event_id": "evt_out",
            "travel_outbound_id": "travel_out",
            "travel_return_id": "travel_back",
        },
    )
    monkeypatch.setattr(
        "templates.confirmations.get_deposit_request_message",
        lambda amount, reason, **_k: f"deposit:{amount}:{reason}",
    )

    result = handle_check_availability(ctx)

    assert result["new_state"] == "DEPOSIT_REQUIRED"
    assert result["actions"] == ["create_pending_event"]
    assert result["messages"] == ["deposit:100:outcall"]
    st = sm.get_state(PHONE)
    assert st["deposit_required"] is True
    assert st["deposit_amount"] == 100
    assert st["deposit_reason"] == "outcall"
    assert st["graphite_event_id"] == "evt_out"
    assert st["travel_outbound_event_id"] == "travel_out"
    assert st["travel_return_event_id"] == "travel_back"


def test_sim_checking_non_confirmation_gets_yes_reminder():
    sm = scenario_state_manager(
        PHONE,
        current_state="CHECKING_AVAILABILITY",
        date=date(2026, 6, 2),
        time=(14, 0),
        duration=60,
        incall_outcall="incall",
        incall_awaiting_yes=True,
        experience_type="GFE",
        client_name="Alex",
        available_now_requested=False,
    )
    ctx = build_context(
        phone_number=PHONE,
        message="thanks",
        state_manager=sm,
    )
    result = handle_check_availability(ctx)
    assert result.get("new_state") is None
    assert "yes" in " ".join(result.get("messages") or []).lower()


def test_sim_checking_no_prompts_for_change_or_cancel():
    sm = scenario_state_manager(
        PHONE,
        current_state="CHECKING_AVAILABILITY",
        date=date(2026, 6, 2),
        time=(14, 0),
        duration=60,
        incall_outcall="incall",
        incall_awaiting_yes=True,
        experience_type="GFE",
        client_name="Alex",
        available_now_requested=False,
    )
    ctx = build_context(
        phone_number=PHONE,
        message="no",
        state_manager=sm,
    )
    result = handle_check_availability(ctx)
    assert result.get("new_state") == "CHECKING_AVAILABILITY"
    body = " ".join(result.get("messages") or []).lower()
    assert "change" in body and "cancel" in body
    st = sm.get_state(PHONE)
    assert st.get("awaiting_booking_change_cancel_choice") is True
    assert st.get("incall_awaiting_yes") is False


def test_sim_checking_change_choice_resets_booking_to_collecting():
    sm = scenario_state_manager(
        PHONE,
        current_state="CHECKING_AVAILABILITY",
        date=date(2026, 6, 2),
        time=(14, 0),
        duration=60,
        incall_outcall="incall",
        awaiting_booking_change_cancel_choice=True,
        experience_type="GFE",
        client_name="Alex",
        available_now_requested=False,
    )
    ctx = build_context(
        phone_number=PHONE,
        message="change",
        state_manager=sm,
    )
    result = handle_check_availability(ctx)
    assert result.get("new_state") == "COLLECTING"
    body = " ".join(result.get("messages") or []).lower()
    assert "fresh booking" in body or "send your new date" in body
    st = sm.get_state(PHONE)
    assert st.get("current_state") == "COLLECTING"
    assert st.get("date") is None
    assert st.get("time") is None
    assert st.get("awaiting_booking_change_cancel_choice") is False


def test_sim_checking_cancel_choice_returns_goodbye():
    sm = scenario_state_manager(
        PHONE,
        current_state="CHECKING_AVAILABILITY",
        date=date(2026, 6, 2),
        time=(14, 0),
        duration=60,
        incall_outcall="incall",
        awaiting_booking_change_cancel_choice=True,
        experience_type="GFE",
        client_name="Alex",
        available_now_requested=False,
    )
    ctx = build_context(
        phone_number=PHONE,
        message="cancel it",
        state_manager=sm,
    )
    result = handle_check_availability(ctx)
    assert result.get("new_state") == "NEW"
    body = " ".join(result.get("messages") or []).lower()
    assert "no booking has been made" in body
    assert "goodbye" in body
    st = sm.get_state(PHONE)
    assert st.get("current_state") == "NEW"
    assert st.get("awaiting_booking_change_cancel_choice") is False


def test_sim_checking_change_request_routes_to_collection_handler(monkeypatch):
    monkeypatch.setattr(
        "handlers.booking_collection.handle_provide_field",
        lambda _ctx: {"messages": ["collect_stub"], "new_state": "COLLECTING", "actions": []},
    )
    sm = scenario_state_manager(
        PHONE,
        current_state="CHECKING_AVAILABILITY",
        date=date(2026, 6, 2),
        time=(14, 0),
        duration=60,
        incall_outcall="incall",
        incall_awaiting_yes=True,
        experience_type="GFE",
        client_name="Alex",
        available_now_requested=False,
    )
    ctx = build_context(
        phone_number=PHONE,
        message="go with 8pm please",
        state_manager=sm,
    )
    result = handle_check_availability(ctx)
    assert result.get("messages") == ["collect_stub"]
    assert result.get("new_state") == "COLLECTING"
    st = sm.get_state(PHONE)
    assert st.get("incall_awaiting_yes") is False
    assert st.get("outcall_awaiting_yes") is False


def test_sim_new_state_cancel_intent_returns_cancelled_response():
    sm = scenario_state_manager(PHONE, current_state="NEW", version=1)
    ctx = build_context(
        phone_number=PHONE,
        message="cancel booking",
        state_manager=sm,
    )
    result = handle_check_availability(ctx)
    assert result.get("messages") == ["No worries! Let me know if you'd like to book another time."]
    assert result.get("new_state") == "NEW"


def test_sim_new_state_punctuation_routes_to_ambiguous_handler(monkeypatch):
    monkeypatch.setattr(
        "handlers.new_conversation.handle_new_ambiguous",
        lambda _ctx: {"messages": ["ambiguous_stub"], "new_state": "NEW", "actions": []},
    )
    sm = scenario_state_manager(PHONE, current_state="NEW", version=1)
    ctx = build_context(
        phone_number=PHONE,
        message="???",
        state_manager=sm,
    )
    result = handle_check_availability(ctx)
    assert result.get("messages") == ["ambiguous_stub"]
    assert result.get("new_state") == "NEW"
