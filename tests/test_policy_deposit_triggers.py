from __future__ import annotations

import config

from booking import deposit_handler
from booking.field_collector import FieldCollector
from handlers.new_conv.enquiries_overnight import handle_overnight_enquiry
from tests.fakes import FakeStateManager

PHONE = "+61400999222"


def test_calculate_deposit_requirement_weekend_requires_deposit(monkeypatch):
    monkeypatch.setattr(deposit_handler, "_get_deposit_outcall", lambda: 150)

    required, amount, reason = deposit_handler.calculate_deposit_requirement(
        {
            "booking_type": "dirty_weekend",
            "experience_type": "dirty weekend",
            "duration": 120,
            "incall_outcall": "outcall",
        },
        PHONE,
        None,
    )

    assert required is True
    assert amount >= 150
    assert "weekend" in reason


def test_calculate_deposit_requirement_fly_me_requires_deposit(monkeypatch):
    monkeypatch.setattr(deposit_handler, "_get_deposit_outcall", lambda: 140)

    required, amount, reason = deposit_handler.calculate_deposit_requirement(
        {
            "booking_type": "fly_me_to_you",
            "experience_type": "fly me to you",
            "duration": 90,
            "incall_outcall": "outcall",
        },
        PHONE,
        None,
    )

    assert required is True
    assert amount >= 140
    assert "fly_me_to_you" in reason


def test_calculate_deposit_requirement_filming_requires_deposit(monkeypatch):
    monkeypatch.setattr(deposit_handler, "_get_deposit_incall", lambda: 95)

    required, amount, reason = deposit_handler.calculate_deposit_requirement(
        {
            "booking_type": "filming",
            "experience_type": "pse_filming",
            "duration": 60,
            "incall_outcall": "incall",
        },
        PHONE,
        None,
    )

    assert required is True
    assert amount >= 95
    assert "filming" in reason


def test_field_collector_detects_filming_experience():
    collector = FieldCollector(config)
    assert collector._parse_experience_type("can we do a pse filming session") == "pse_filming"
    assert collector._parse_experience_type("switch to filming please") == "pse_filming"


def test_overnight_enquiry_requests_deposit_and_updates_state(monkeypatch):
    sm = FakeStateManager(initial={PHONE: {"current_state": "NEW", "first_contact_sent": False}})
    state = sm.get_state(PHONE) or {}
    ctx = {
        "phone_number": PHONE,
        "message": "I want a weekend booking package",
        "state": state,
        "state_manager": sm,
    }

    monkeypatch.setattr("config.get_escort_phone_number", lambda: "+61400111111")
    monkeypatch.setattr("config.get_escort_name", lambda: "Adella")
    monkeypatch.setattr("services.sms_service.send_escort_sms", lambda *_a, **_k: True)

    result = handle_overnight_enquiry(ctx)
    updated = sm.get_state(PHONE) or {}

    assert result["new_state"] == "EXTENDED_ENQUIRY"
    assert "automated message service" in (result["messages"][0] or "").lower()
    assert "dirty weekend rate is $9500" in (result["messages"][0] or "").lower()
    assert "$200 deposit" in (result["messages"][0] or "").lower()
    assert "adella" in (result["messages"][0] or "").lower()
    assert updated.get("deposit_required") is True
    assert int(updated.get("deposit_amount") or 0) > 0
    assert "weekend" in str(updated.get("deposit_reason") or "")
