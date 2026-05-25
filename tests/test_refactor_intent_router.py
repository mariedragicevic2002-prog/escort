from __future__ import annotations

from refactor.app.middleware.request_validation import RequestValidationMiddleware
from refactor.app.runtime.context import InboundSMSMessage, RuntimeServices
from refactor.app.runtime.intent_router import IntentRouter, SignalIntentResolver
from refactor.app.runtime.orchestration_facade import OrchestrationFacade


def _inbound(
    *,
    phone: str = "+61412345678",
    body: str = "hello",
    message_data: dict | None = None,
    request_payload: dict | None = None,
) -> InboundSMSMessage:
    return InboundSMSMessage(
        phone_number=phone,
        body=body,
        message_data=message_data or {},
        request_payload=request_payload or {},
        request_id="req-intent-router",
    )


class _AlwaysFastPath:
    name = "always-fast"

    def matches(self, _context) -> bool:
        return True

    def handle(self, _context) -> list[str]:
        return ["fast-path-reply"]


def test_intent_router_selects_registered_handler() -> None:
    legacy_called = {"value": False}

    def _legacy(_phone: str, _body: str) -> list[str]:
        legacy_called["value"] = True
        return ["legacy-reply"]

    router = IntentRouter(
        resolver=SignalIntentResolver(),
        intent_handlers={"booking": lambda _ctx: ["intent-reply"]},
    )
    runtime = RuntimeServices(
        state_manager=object(),
        db_service=object(),
        legacy_processor=_legacy,
        intent_router=router,
    )
    facade = OrchestrationFacade(runtime_services=runtime, middlewares=[RequestValidationMiddleware()])

    outcome = facade.process_sms(_inbound(message_data={"intent": "booking"}))

    assert outcome.messages == ["intent-reply"]
    assert legacy_called["value"] is False
    assert outcome.metadata["routing_path"] == "intent"
    assert outcome.metadata["resolved_intent"] == "booking"


def test_fast_path_takes_precedence_over_intent_handler() -> None:
    router = IntentRouter(
        resolver=SignalIntentResolver(),
        intent_handlers={"booking": lambda _ctx: ["intent-reply"]},
        fast_path_handlers=[_AlwaysFastPath()],
    )
    runtime = RuntimeServices(
        state_manager=object(),
        db_service=object(),
        legacy_processor=lambda _phone, _body: ["legacy-reply"],
        intent_router=router,
    )
    facade = OrchestrationFacade(runtime_services=runtime, middlewares=[RequestValidationMiddleware()])

    outcome = facade.process_sms(_inbound(message_data={"intent": "booking"}))

    assert outcome.messages == ["fast-path-reply"]
    assert outcome.metadata["routing_path"] == "fast_path"
    assert outcome.metadata["routing_handler"] == "always-fast"


def test_intent_router_falls_back_to_legacy_without_registered_handler() -> None:
    legacy_called = {"value": False}

    def _legacy(_phone: str, _body: str) -> list[str]:
        legacy_called["value"] = True
        return ["legacy-reply"]

    router = IntentRouter(resolver=SignalIntentResolver())
    runtime = RuntimeServices(
        state_manager=object(),
        db_service=object(),
        legacy_processor=_legacy,
        intent_router=router,
    )
    facade = OrchestrationFacade(runtime_services=runtime, middlewares=[RequestValidationMiddleware()])

    outcome = facade.process_sms(_inbound(message_data={"intent": "booking"}))

    assert outcome.messages == ["legacy-reply"]
    assert legacy_called["value"] is True
    assert outcome.metadata["routing_path"] == "legacy_fallback"
    assert outcome.metadata["fallback_to_legacy"] is True
