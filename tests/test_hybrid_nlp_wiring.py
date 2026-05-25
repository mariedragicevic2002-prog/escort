from __future__ import annotations

from core.classifier import Classifier
from handlers.availability_parts.availability_check_impl import handle_unknown_in_checking
from handlers.booking_coll._provide_field_context import CollectingCtx
from handlers.booking_coll._provide_field_stages_slot_load import _stage_nothing_extracted_shortcut
from handlers.new_conv.enquiries_doubles import handle_doubles_enquiry
from templates.booking_collection_messages import BOOKING_CANCELLED_NO_PROBLEM
from tests.fakes import FakeStateManager
from tests.scenarios.utils import build_context, scenario_state_manager

PHONE = "+61400123999"


class _FakeAIByRoute:
    def __init__(self, responses: dict[str, str]):
        self.responses = responses

    def chat(self, prompt: str, *_args, **_kwargs):
        lower = (prompt or "").lower()
        if "route\": \"special_booking" in lower:
            return self.responses.get("special_booking", "{}")
        if "route\": \"doubles" in lower:
            return self.responses.get("doubles", "{}")
        if "route\": \"outcall_venue" in lower:
            return self.responses.get("outcall_venue", "{}")
        if "route\": \"temporal_intent" in lower:
            return self.responses.get("temporal_intent", "{}")
        if "route\": \"flow_shift" in lower:
            return self.responses.get("flow_shift", "{}")
        if "route\": \"doubles_supply_clarity" in lower:
            return self.responses.get("doubles_supply_clarity", "{}")
        if "route\": \"deposit_intent" in lower:
            return self.responses.get("deposit_intent", "{}")
        if "route\": \"loop_break" in lower:
            return self.responses.get("loop_break", "{}")
        return "{}"

    def classify_intent(self, *_args, **_kwargs):
        return None


def test_classifier_routes_special_filming_via_hybrid(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    ai = _FakeAIByRoute(
        responses={
            "special_booking": '{"route":"special_booking","booking_type":"filming","confidence":0.95}'
        }
    )
    c = Classifier(ai_service=ai)
    st = {"current_state": "NEW", "version": 1}
    assert c.classify("keen for a video shoot session", [], {"state": st}) == "overnight_enquiry"


def test_doubles_route_does_not_auto_confirm_supply_from_hybrid(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    monkeypatch.setattr(
        "handlers.booking_coll.doubles_first_turn_compose.compose_ambiguous_doubles_supply_first_turn",
        lambda **_kwargs: "NLP_SUPPLY_GATE",
    )
    monkeypatch.setattr("core.webform_security.get_webform_url", lambda _phone: "https://example.test/booking")

    ai = _FakeAIByRoute(
        responses={
            "doubles": (
                '{"route":"doubles","doubles_type":"mff","escort_supply_source":"escort","confidence":0.93}'
            )
        }
    )
    sm = FakeStateManager(initial={PHONE: {"current_state": "NEW", "first_contact_sent": False}})
    state = sm.get_state(PHONE) or {}
    ctx = {
        "phone_number": PHONE,
        "message": "keen for a threesome this weekend",
        "state": state,
        "state_manager": sm,
        "ai_service": ai,
        "message_history": [],
    }

    result = handle_doubles_enquiry(ctx)
    updated = sm.get_state(PHONE) or {}

    assert result["messages"] == ["NLP_SUPPLY_GATE"]
    assert updated.get("doubles_type") == "mff"
    assert updated.get("escort_supply_source") is None
    assert updated.get("escort_supply_confirmed") is False
    assert updated.get("booking_status") == "doubles_supply_gate"


def test_doubles_route_client_supplied_friend_phrase_resolves_mmf(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    monkeypatch.setattr(
        "handlers.booking_coll.doubles_first_turn_compose.compose_client_supplied_doubles_first_turn",
        lambda **_kwargs: "NLP_CLIENT_SOURCED",
    )
    monkeypatch.setattr("core.webform_security.get_webform_url", lambda _phone: "https://example.test/booking")

    ai = _FakeAIByRoute(
        responses={
            "doubles": (
                '{"route":"doubles","doubles_type":"unknown","escort_supply_source":"client","confidence":0.94}'
            )
        }
    )
    sm = FakeStateManager(initial={PHONE: {"current_state": "NEW", "first_contact_sent": False}})
    state = sm.get_state(PHONE) or {}
    ctx = {
        "phone_number": PHONE,
        "message": "threesome booking, i am providing a friend",
        "state": state,
        "state_manager": sm,
        "ai_service": ai,
        "message_history": [],
    }

    result = handle_doubles_enquiry(ctx)
    updated = sm.get_state(PHONE) or {}

    assert result["messages"] == ["NLP_CLIENT_SOURCED"]
    assert updated.get("doubles_type") == "mmf"
    assert updated.get("escort_supply_source") == "client"
    assert updated.get("escort_supply_confirmed") is True


def test_doubles_route_client_supplied_other_person_resolves_mmf(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    monkeypatch.setattr(
        "handlers.booking_coll.doubles_first_turn_compose.compose_client_supplied_doubles_first_turn",
        lambda **_kwargs: "NLP_CLIENT_SOURCED",
    )
    monkeypatch.setattr("core.webform_security.get_webform_url", lambda _phone: "https://example.test/booking")

    ai = _FakeAIByRoute(
        responses={
            "doubles": (
                '{"route":"doubles","doubles_type":"unknown","escort_supply_source":"client","confidence":0.94}'
            )
        }
    )
    sm = FakeStateManager(initial={PHONE: {"current_state": "NEW", "first_contact_sent": False}})
    state = sm.get_state(PHONE) or {}
    ctx = {
        "phone_number": PHONE,
        "message": "threesome this weekend, i am bringing the other person",
        "state": state,
        "state_manager": sm,
        "ai_service": ai,
        "message_history": [],
    }

    result = handle_doubles_enquiry(ctx)
    updated = sm.get_state(PHONE) or {}

    assert result["messages"] == ["NLP_CLIENT_SOURCED"]
    assert updated.get("doubles_type") == "mmf"
    assert updated.get("escort_supply_source") == "client"
    assert updated.get("escort_supply_confirmed") is True


def test_doubles_route_hybrid_conflicting_mff_friend_phrase_still_resolves_mmf(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    monkeypatch.setattr(
        "handlers.booking_coll.doubles_first_turn_compose.compose_client_supplied_doubles_first_turn",
        lambda **_kwargs: "NLP_CLIENT_SOURCED",
    )
    monkeypatch.setattr("core.webform_security.get_webform_url", lambda _phone: "https://example.test/booking")

    ai = _FakeAIByRoute(
        responses={
            "doubles": (
                '{"route":"doubles","doubles_type":"mff","escort_supply_source":"client","confidence":0.94}'
            )
        }
    )
    sm = FakeStateManager(initial={PHONE: {"current_state": "NEW", "first_contact_sent": False}})
    state = sm.get_state(PHONE) or {}
    ctx = {
        "phone_number": PHONE,
        "message": "threesome booking, i am providing a friend",
        "state": state,
        "state_manager": sm,
        "ai_service": ai,
        "message_history": [],
    }

    result = handle_doubles_enquiry(ctx)
    updated = sm.get_state(PHONE) or {}

    assert result["messages"] == ["NLP_CLIENT_SOURCED"]
    assert updated.get("doubles_type") == "mmf"
    assert updated.get("escort_supply_source") == "client"
    assert updated.get("escort_supply_confirmed") is True


def test_doubles_route_hybrid_conflicting_mff_other_person_phrase_still_resolves_mmf(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    monkeypatch.setattr(
        "handlers.booking_coll.doubles_first_turn_compose.compose_client_supplied_doubles_first_turn",
        lambda **_kwargs: "NLP_CLIENT_SOURCED",
    )
    monkeypatch.setattr("core.webform_security.get_webform_url", lambda _phone: "https://example.test/booking")

    ai = _FakeAIByRoute(
        responses={
            "doubles": (
                '{"route":"doubles","doubles_type":"mff","escort_supply_source":"client","confidence":0.94}'
            )
        }
    )
    sm = FakeStateManager(initial={PHONE: {"current_state": "NEW", "first_contact_sent": False}})
    state = sm.get_state(PHONE) or {}
    ctx = {
        "phone_number": PHONE,
        "message": "threesome this weekend, i am bringing the other person",
        "state": state,
        "state_manager": sm,
        "ai_service": ai,
        "message_history": [],
    }

    result = handle_doubles_enquiry(ctx)
    updated = sm.get_state(PHONE) or {}

    assert result["messages"] == ["NLP_CLIENT_SOURCED"]
    assert updated.get("doubles_type") == "mmf"
    assert updated.get("escort_supply_source") == "client"
    assert updated.get("escort_supply_confirmed") is True


def test_collecting_shortcut_hybrid_cancel(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    ai = _FakeAIByRoute(
        responses={
            "loop_break": '{"route":"loop_break","shift_label":"cancel","confidence":0.92}'
        }
    )
    sm = FakeStateManager(
        initial={PHONE: {"current_state": "COLLECTING", "first_contact_sent": True, "message_count": 3}}
    )
    state = sm.get_state(PHONE) or {}

    class _FakeCollector:
        def get_missing_fields(self, _fields):
            return ["date", "time", "duration"]

    class _FakeValidator:
        pass

    raw_context = {
        "phone_number": PHONE,
        "message": "nah leave it cancel",
        "state": state,
        "state_manager": sm,
        "ai_service": ai,
        "message_history": [],
    }
    ctx = CollectingCtx(
        phone_number=PHONE,
        message="nah leave it cancel",
        raw_context=raw_context,
        state_manager=sm,
        field_collector=_FakeCollector(),
        field_validator=_FakeValidator(),
        ai_service=ai,
        db_service=None,
    )
    ctx.state = state
    ctx.current_fields = {}
    ctx.extracted = {}

    result = _stage_nothing_extracted_shortcut(ctx)
    assert result is not None
    assert result["messages"] == [BOOKING_CANCELLED_NO_PROBLEM]
    assert result["new_state"] == "NEW"
    assert (sm.get_state(PHONE) or {}).get("current_state") == "NEW"


def test_checking_unknown_hybrid_cancel_routes_to_check_handler(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    ai = _FakeAIByRoute(
        responses={
            "loop_break": '{"route":"loop_break","shift_label":"cancel","confidence":0.91}'
        }
    )
    sm = scenario_state_manager(
        PHONE,
        current_state="CHECKING_AVAILABILITY",
        incall_awaiting_yes=True,
        outcall_awaiting_yes=False,
        date="2026-06-02",
        time=(21, 0),
        duration=60,
    )
    ctx = build_context(phone_number=PHONE, message="nah dont want this slot", state_manager=sm, ai_service=ai)
    monkeypatch.setattr(
        "handlers.availability_parts.availability_check_impl.handle_check_availability",
        lambda _ctx: {"messages": ["CHECK_HANDLER_STUB"], "new_state": "NEW", "actions": []},
    )

    result = handle_unknown_in_checking(ctx)
    assert result["messages"] == ["CHECK_HANDLER_STUB"]
    assert result["new_state"] == "NEW"


def test_doubles_route_uses_outcall_venue_hint_for_implicit_outcall(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    monkeypatch.setattr("core.webform_security.get_webform_url", lambda _phone: "https://example.test/booking")
    ai = _FakeAIByRoute(
        responses={
            "doubles": '{"route":"doubles","doubles_type":"unknown","escort_supply_source":"unknown","confidence":0.91}',
            "outcall_venue": '{"route":"outcall_venue","location_mode":"outcall","venue_type":"hotel","confidence":0.94}',
        }
    )
    sm = FakeStateManager(initial={PHONE: {"current_state": "NEW", "first_contact_sent": False}})
    state = sm.get_state(PHONE) or {}
    ctx = {
        "phone_number": PHONE,
        "message": "keen for a threesome near me tonight",
        "state": state,
        "state_manager": sm,
        "ai_service": ai,
        "message_history": [],
    }

    handle_doubles_enquiry(ctx)
    updated = sm.get_state(PHONE) or {}
    assert updated.get("incall_outcall") == "outcall"


def test_doubles_route_temporal_hint_sets_available_now_requested(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    monkeypatch.setattr("utils.time_parser.is_immediate_request", lambda _m: False)
    monkeypatch.setattr(
        "handlers.booking_coll.doubles_first_turn_compose.compose_ambiguous_doubles_supply_first_turn",
        lambda **_kwargs: "TEMPORAL_HINT_USED",
    )
    monkeypatch.setattr("core.webform_security.get_webform_url", lambda _phone: "https://example.test/booking")
    ai = _FakeAIByRoute(
        responses={
            "doubles": '{"route":"doubles","doubles_type":"unknown","escort_supply_source":"unknown","confidence":0.91}',
            "temporal_intent": '{"route":"temporal_intent","urgency":"asap","window_token":"asap","confidence":0.95}',
        }
    )
    sm = FakeStateManager(initial={PHONE: {"current_state": "NEW", "first_contact_sent": False}})
    state = sm.get_state(PHONE) or {}
    ctx = {
        "phone_number": PHONE,
        "message": "threesome whenever you can",
        "state": state,
        "state_manager": sm,
        "ai_service": ai,
        "message_history": [],
    }

    handle_doubles_enquiry(ctx)
    updated = sm.get_state(PHONE) or {}
    assert updated.get("available_now_requested") is True


def test_doubles_route_uses_doubles_supply_clarity_hint(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    monkeypatch.setattr("core.webform_security.get_webform_url", lambda _phone: "https://example.test/booking")
    ai = _FakeAIByRoute(
        responses={
            "doubles": '{"route":"doubles","doubles_type":"mmf","escort_supply_source":"unknown","confidence":0.91}',
            "doubles_supply_clarity": '{"route":"doubles_supply_clarity","escort_supply_source":"client","confidence":0.93}',
        }
    )
    sm = FakeStateManager(initial={PHONE: {"current_state": "NEW", "first_contact_sent": False}})
    state = sm.get_state(PHONE) or {}
    ctx = {
        "phone_number": PHONE,
        "message": "doubles mmf booking with a mate",
        "state": state,
        "state_manager": sm,
        "ai_service": ai,
        "message_history": [],
    }

    result = handle_doubles_enquiry(ctx)
    updated = sm.get_state(PHONE) or {}
    assert result["messages"]
    assert updated.get("escort_supply_source") is None
    assert updated.get("escort_supply_confirmed") is False
    assert updated.get("booking_status") == "doubles_supply_gate"


def test_collecting_shortcut_hybrid_flow_shift_cancel(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    ai = _FakeAIByRoute(
        responses={
            "flow_shift": '{"route":"flow_shift","shift_label":"cancel","confidence":0.92}'
        }
    )
    sm = FakeStateManager(
        initial={PHONE: {"current_state": "COLLECTING", "first_contact_sent": True, "message_count": 3}}
    )
    state = sm.get_state(PHONE) or {}

    class _FakeCollector:
        def get_missing_fields(self, _fields):
            return ["date", "time", "duration"]

    class _FakeValidator:
        pass

    raw_context = {
        "phone_number": PHONE,
        "message": "nah cancel this",
        "state": state,
        "state_manager": sm,
        "ai_service": ai,
        "message_history": [],
    }
    ctx = CollectingCtx(
        phone_number=PHONE,
        message="nah cancel this",
        raw_context=raw_context,
        state_manager=sm,
        field_collector=_FakeCollector(),
        field_validator=_FakeValidator(),
        ai_service=ai,
        db_service=None,
    )
    ctx.state = state
    ctx.current_fields = {}
    ctx.extracted = {}

    result = _stage_nothing_extracted_shortcut(ctx)
    assert result is not None
    assert result["messages"] == [BOOKING_CANCELLED_NO_PROBLEM]
    assert result["new_state"] == "NEW"


def test_checking_unknown_hybrid_deposit_question_response(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    ai = _FakeAIByRoute(
        responses={
            "flow_shift": '{"route":"flow_shift","shift_label":"continue","confidence":0.91}',
            "deposit_intent": '{"route":"deposit_intent","intent":"question","confidence":0.94}',
        }
    )
    sm = scenario_state_manager(
        PHONE,
        current_state="CHECKING_AVAILABILITY",
        incall_awaiting_yes=True,
        outcall_awaiting_yes=False,
        deposit_required=True,
        date="2026-06-02",
        time=(21, 0),
        duration=60,
    )
    ctx = build_context(phone_number=PHONE, message="why deposit?", state_manager=sm, ai_service=ai)
    result = handle_unknown_in_checking(ctx)
    assert result["new_state"] is None
    assert "deposit is required" in result["messages"][0].lower()
