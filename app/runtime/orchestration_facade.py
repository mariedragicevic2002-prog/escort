from __future__ import annotations

from collections.abc import Sequence
import os

from adapters.legacy_sms_adapter import LegacySMSProcessorAdapter
from app.middleware.contracts import InboundMiddleware, NextHandler
from app.middleware.idempotency import IdempotencyMiddleware
from app.middleware.request_validation import RequestValidationMiddleware
from app.middleware.security_controls import SecurityControlsMiddleware
from app.policy import (
    RuntimePolicyDecision,
    RuntimePolicyEngine,
    RuntimePolicyInput,
    build_default_runtime_policy_engine,
)
from app.runtime.context import (
    InboundSMSMessage,
    OrchestrationContext,
    OrchestrationOutcome,
    RuntimeServices,
)
from app.runtime.transition_service import (
    TransitionRequest,
    TransitionResult,
    build_state_transition_service,
)
from app.runtime.intent_router import build_default_intent_router
from app.runtime.response_composer import compose_response
from app.security.auth import SharedSecretVerifier


class OrchestrationFacade:
    """Composable inbound pipeline with behavior-preserving legacy delegation."""

    def __init__(self, *, runtime_services: RuntimeServices, middlewares: Sequence[InboundMiddleware]) -> None:
        self._runtime_services = runtime_services
        self._middlewares = tuple(middlewares)
        self._policy_engine: RuntimePolicyEngine = runtime_services.policy_engine or build_default_runtime_policy_engine()
        self._pipeline_handler = self._build_pipeline()

    def process_sms(self, inbound: InboundSMSMessage) -> OrchestrationOutcome:
        context = OrchestrationContext(inbound=inbound, runtime=self._runtime_services)
        composed = compose_response(self._pipeline_handler(context))
        context.metadata["action_count"] = len(composed.actions)
        return OrchestrationOutcome(
            messages=composed.messages,
            actions=composed.actions,
            duplicate=bool(context.metadata.get("duplicate")),
            metadata=dict(context.metadata),
        )

    def _build_pipeline(self) -> NextHandler:
        handler: NextHandler = self._terminal_handler
        for middleware in reversed(self._middlewares):
            handler = self._compose_middleware(middleware, handler)
        return handler

    @staticmethod
    def _compose_middleware(middleware: InboundMiddleware, next_handler: NextHandler) -> NextHandler:
        def wrapped(current_context: OrchestrationContext) -> list[str]:
            return middleware(current_context, next_handler)

        return wrapped

    def _terminal_handler(self, context: OrchestrationContext) -> list[str]:
        policy_result = self._policy_engine.evaluate(self._build_policy_input(context))
        context.metadata["policy_decision"] = policy_result.decision.value
        context.metadata["policy_reason"] = policy_result.reason.value
        context.metadata["policy_provider"] = policy_result.provider_name
        context.metadata["policy_fallback_used"] = policy_result.fallback_used
        if policy_result.details:
            context.metadata["policy_details"] = dict(policy_result.details)

        if policy_result.decision in (RuntimePolicyDecision.DENY, RuntimePolicyDecision.EARLY_EXIT):
            return list(policy_result.messages)
        if policy_result.decision == RuntimePolicyDecision.FALLBACK_TO_LEGACY:
            context.metadata.setdefault("routing_path", "legacy_fallback")
            context.metadata.setdefault("fallback_to_legacy", True)
            return self._legacy_delegate(context)
        return self._route_with_intent_or_legacy(context)

    @staticmethod
    def _build_policy_input(context: OrchestrationContext) -> RuntimePolicyInput:
        return RuntimePolicyInput(
            phone_number=context.inbound.phone_number,
            message_body=context.inbound.body,
            request_id=context.inbound.request_id,
            metadata=context.metadata,
            has_intent_router=getattr(context.runtime, "intent_router", None) is not None,
        )

    def _route_with_intent_or_legacy(self, context: OrchestrationContext) -> list[str]:
        intent_router = getattr(context.runtime, "intent_router", None)
        if intent_router is not None:
            return intent_router.route(context, fallback_handler=self._legacy_delegate)
        return self._legacy_delegate(context)

    def transition_state(self, request: TransitionRequest) -> TransitionResult:
        """Single transition API exposed to orchestration callers."""
        service = self._runtime_services.transition_service
        if service is None:
            raise RuntimeError("Transition service is not configured")
        return service.transition(request)

    @staticmethod
    def _legacy_delegate(context: OrchestrationContext) -> list[str]:
        processor = context.runtime.legacy_processor
        if hasattr(processor, "process"):
            return processor.process(context.inbound.phone_number, context.inbound.body)
        if callable(processor):
            return list(processor(context.inbound.phone_number, context.inbound.body) or [])
        raise TypeError("Legacy processor must be callable or expose a process method")


def build_default_sms_facade(*, state_manager, db_service, legacy_processor) -> OrchestrationFacade:
    transition_service = build_state_transition_service(
        state_manager=state_manager,
        db_service=db_service,
    )
    auth_verifier = SharedSecretVerifier(
        secret_provider=lambda: (os.environ.get("GATEWAY_SECRET") or "").strip(),
        next_secret_provider=lambda: (os.environ.get("GATEWAY_SECRET_NEXT") or "").strip(),
        deprecated_secret_provider=lambda: (os.environ.get("GATEWAY_SECRET_DEPRECATED") or "").strip(),
        cutover_state_provider=lambda: (os.environ.get("GATEWAY_SECRET_CUTOVER_STATE") or "").strip(),
        header_name="X-Gateway-Secret",
        allow_loopback_without_secret=True,
    )
    legacy_adapter = LegacySMSProcessorAdapter(legacy_processor)
    runtime_services = RuntimeServices(
        state_manager=state_manager,
        db_service=db_service,
        legacy_processor=legacy_adapter,
        transition_service=transition_service,
        intent_router=build_default_intent_router(legacy_processor=legacy_adapter),
        policy_engine=build_default_runtime_policy_engine(),
    )
    middlewares: list[InboundMiddleware] = [
        SecurityControlsMiddleware(auth_verifier=auth_verifier),
        RequestValidationMiddleware(),
        IdempotencyMiddleware(),
    ]
    return OrchestrationFacade(runtime_services=runtime_services, middlewares=middlewares)
