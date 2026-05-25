from __future__ import annotations

from handlers.booking_coll._shared_dinner_doubles import _check_doubles_supply_response
from handlers.new_conv.enquiries_doubles import handle_doubles_enquiry
from tests.fakes import FakeStateManager

PHONE = "+61408887777"


def test_doubles_enquiry_persists_outcall_intent_during_threesome_clarification(monkeypatch):
    monkeypatch.setattr("core.webform_security.get_webform_url", lambda _phone: "https://example.test/webform")

    sm = FakeStateManager(initial={PHONE: {"current_state": "NEW", "first_contact_sent": False}})
    state = sm.get_state(PHONE) or {}
    ctx = {
        "phone_number": PHONE,
        "message": "Hi im keen to book you for a threesome can you come to my place asap?",
        "state": state,
        "state_manager": sm,
        "ai_service": None,
        "message_history": [],
    }

    out = handle_doubles_enquiry(ctx)

    assert out.get("new_state") == "COLLECTING"
    saved = sm.get_state(PHONE) or {}
    assert saved.get("incall_outcall") == "outcall"
    assert saved.get("available_now_requested") is True


def test_mmf_ambiguous_supply_prompt_uses_outcall_template_when_state_is_outcall(monkeypatch):
    monkeypatch.setattr("core.webform_security.get_webform_url", lambda _phone: "https://example.test/webform")
    monkeypatch.setattr("config.get_profile_url", lambda: "https://example.test/profile")

    sm = FakeStateManager(
        initial={
            PHONE: {
                "current_state": "COLLECTING",
                "booking_type": "Doubles MMF",
                "experience_type": "Doubles MMF",
                "doubles_type": "mmf",
                "incall_outcall": "outcall",
                "client_name": "Joe",
            }
        }
    )
    state = sm.get_state(PHONE) or {}

    out = _check_doubles_supply_response(
        "Doubles MMF",
        PHONE,
        state,
        sm,
        doubles_supply_gate_follow_up=False,
    )
    assert out is not None
    body = "\n".join(out.get("messages") or [])
    low = body.lower()

    assert "i only do outcalls to hotels or apartments within 15km" in low
    assert "if your wanting me to organise the other person" in low
    assert "additional surcharge is because two of us would need to travel to you" in low
    assert "please advise if you will be supplying the other person" not in low


def test_mmf_ambiguous_supply_prompt_uses_updated_incall_template(monkeypatch):
    monkeypatch.setattr("core.webform_security.get_webform_url", lambda _phone: "https://example.test/webform")
    monkeypatch.setattr("config.get_profile_url", lambda: "https://example.test/profile")

    sm = FakeStateManager(
        initial={
            PHONE: {
                "current_state": "COLLECTING",
                "booking_type": "Doubles MMF",
                "experience_type": "Doubles MMF",
                "doubles_type": "mmf",
                "incall_outcall": "incall",
                "client_name": "Joe",
            }
        }
    )
    state = sm.get_state(PHONE) or {}

    out = _check_doubles_supply_response(
        "Doubles MMF",
        PHONE,
        state,
        sm,
        doubles_supply_gate_follow_up=False,
    )
    assert out is not None
    body = "\n".join(out.get("messages") or [])
    low = body.lower()

    assert "hi joe" in low
    assert "i love doubles mmf bookings." in low
    assert "will you be bringing the other person yourself" in low
    assert "just so you know, when i need to arrange someone there is a minimum 4 hours notice required" in low
    assert "i strongly recommend booking through my webform for all doubles bookings:" in low
    assert "please advise if you will be supplying the other person" not in low
    assert "mandatory deposit is required for all doubles bookings" not in low
