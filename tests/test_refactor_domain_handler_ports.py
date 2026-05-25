from __future__ import annotations

import pytest

from app.middleware.request_validation import RequestValidationMiddleware
from app.runtime.context import InboundSMSMessage, OrchestrationContext, RuntimeServices
from app.runtime.domain_handler_registry import DomainHandlerNotRegisteredError
from app.runtime.intent_router import build_default_intent_router
from app.runtime.legacy_domain_handler_adapters import (
    LegacyBookingHandlerAdapter,
    build_legacy_domain_handler_registry,
)
from app.runtime.orchestration_facade import OrchestrationFacade
from refactor.domain.handler_ports import BookingHandlerPayload


def _inbound(*, intent: str, body: str = "hello") -> InboundSMSMessage:
    return InboundSMSMessage(
        phone_number="+61412345678",
        body=body,
        message_data={"intent": intent},
        request_payload={},
        request_id=f"req-{intent}",
    )


class _LegacyStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def process(self, phone_number: str, message_body: str) -> list[str]:
        self.calls.append((phone_number, message_body))
        return [f"legacy:{message_body}"]


@pytest.mark.parametrize(
    ("intent", "expected_type"),
    [
        ("booking", "booking"),
        ("escalate_manual_review", "escalation"),
        ("moderation", "moderation"),
    ],
)
def test_default_router_selects_typed_domain_handler_by_intent(intent: str, expected_type: str) -> None:
    legacy = _LegacyStub()
    router = build_default_intent_router(legacy_processor=legacy)
    runtime = RuntimeServices(
        state_manager=object(),
        db_service=object(),
        legacy_processor=legacy,
        intent_router=router,
    )
    facade = OrchestrationFacade(runtime_services=runtime, middlewares=[RequestValidationMiddleware()])

    outcome = facade.process_sms(_inbound(intent=intent, body=f"body-{intent}"))

    assert outcome.messages == [f"legacy:body-{intent}"]
    assert outcome.metadata["routing_path"] == "intent"
    assert outcome.metadata["routing_handler"] == intent
    assert outcome.metadata["domain_handler_type"] == expected_type
    assert outcome.metadata["domain_handler_intent"] == intent


def test_legacy_booking_adapter_preserves_payload_and_response_shape() -> None:
    observed: dict[str, str] = {}

    def _legacy(phone_number: str, message_body: str):
        observed["phone_number"] = phone_number
        observed["message_body"] = message_body
        return ("ok-1", "ok-2")

    adapter = LegacyBookingHandlerAdapter(_legacy)
    payload = BookingHandlerPayload(
        phone_number="+61412345678",
        message_body="book now",
        intent="booking",
        metadata={"request_id": "req-booking"},
    )

    response = adapter.handle_booking(payload)

    assert observed == {
        "phone_number": "+61412345678",
        "message_body": "book now",
    }
    assert response.messages == ["ok-1", "ok-2"]
    assert response.metadata == {
        "bridge": "legacy_sms_processor",
        "domain_handler_type": "booking",
        "intent": "booking",
    }


def test_registry_raises_explicit_error_for_unknown_intent() -> None:
    legacy = _LegacyStub()
    registry = build_legacy_domain_handler_registry(legacy)
    context = OrchestrationContext(
        inbound=_inbound(intent="unknown_intent"),
        runtime=RuntimeServices(
            state_manager=object(),
            db_service=object(),
            legacy_processor=legacy,
        ),
    )

    with pytest.raises(DomainHandlerNotRegisteredError, match="unknown_intent"):
        registry.dispatch(context, intent="unknown_intent")


def test_unknown_intent_falls_back_to_legacy_in_runtime_path() -> None:
    legacy = _LegacyStub()
    router = build_default_intent_router(legacy_processor=legacy)
    runtime = RuntimeServices(
        state_manager=object(),
        db_service=object(),
        legacy_processor=legacy,
        intent_router=router,
    )
    facade = OrchestrationFacade(runtime_services=runtime, middlewares=[RequestValidationMiddleware()])

    outcome = facade.process_sms(_inbound(intent="unknown_intent", body="safe fallback"))

    assert outcome.messages == ["legacy:safe fallback"]
    assert outcome.metadata["routing_path"] == "legacy_fallback"
    assert outcome.metadata["fallback_to_legacy"] is True
