from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from app.runtime.context import OrchestrationContext
from app.runtime.intent_contracts import (
    FastPathHandler,
    IntentHandler,
    IntentResolution,
    IntentResolver,
    LegacyFallbackHandler,
)
from app.runtime.legacy_domain_handler_adapters import (
    build_legacy_domain_handler_registry,
)
from app.runtime._utils import normalize_intent

logger = logging.getLogger(__name__)


def _normalize_intent(value: str | None) -> str:
    return normalize_intent(value)


def _coerce_messages(value: Sequence[str] | None) -> list[str]:
    if value is None:
        return []
    return value if isinstance(value, list) else list(value)


class SignalIntentResolver:
    """Resolves intent from context metadata and inbound signal payloads."""

    _INTENT_KEYS = ("intent", "resolved_intent", "intent_name", "classification_intent")

    def resolve(self, context: OrchestrationContext) -> IntentResolution | None:
        for source, payload in (
            ("metadata", context.metadata),
            ("message_data", context.inbound.message_data),
            ("request_payload", context.inbound.request_payload),
        ):
            intent = self._extract(payload)
            if intent:
                return IntentResolution(intent=intent, source=source)
        return None

    def _extract(self, payload: Mapping[str, object] | None) -> str | None:
        if not isinstance(payload, Mapping):
            return None
        for key in self._INTENT_KEYS:
            raw_value = payload.get(key)
            if raw_value is None:
                continue
            normalized = _normalize_intent(str(raw_value))
            if normalized:
                return normalized
        return None


class IntentRouter:
    """Typed intent and fast-path router with legacy fallback."""

    def __init__(
        self,
        *,
        resolver: IntentResolver | None = None,
        intent_handlers: Mapping[str, IntentHandler] | None = None,
        fast_path_handlers: Sequence[FastPathHandler] | None = None,
    ) -> None:
        self._resolver = resolver or SignalIntentResolver()
        self._intent_handlers: dict[str, IntentHandler] = {}
        self._fast_path_handlers = list(fast_path_handlers or [])
        for intent, handler in (intent_handlers or {}).items():
            self.register_intent_handler(intent, handler)

    def register_intent_handler(self, intent: str, handler: IntentHandler) -> None:
        normalized = _normalize_intent(intent)
        if normalized:
            self._intent_handlers[normalized] = handler

    def register_fast_path_handler(self, handler: FastPathHandler) -> None:
        self._fast_path_handlers.append(handler)

    def route(
        self,
        context: OrchestrationContext,
        *,
        fallback_handler: LegacyFallbackHandler,
    ) -> list[str]:
        for fast_handler in self._fast_path_handlers:
            try:
                if not fast_handler.matches(context):
                    continue
                context.metadata["routing_path"] = "fast_path"
                context.metadata["routing_handler"] = fast_handler.name
                return _coerce_messages(fast_handler.handle(context))
            except Exception:
                logger.exception("fast-path handler failed: %s", getattr(fast_handler, "name", "unknown"))

        resolution = self._resolver.resolve(context)
        if resolution:
            context.metadata["resolved_intent"] = resolution.intent
            context.metadata["intent_source"] = resolution.source
            handler = self._intent_handlers.get(resolution.intent)
            if handler is not None:
                context.metadata["routing_path"] = "intent"
                context.metadata["routing_handler"] = resolution.intent
                return _coerce_messages(handler(context))

        context.metadata["routing_path"] = "legacy_fallback"
        context.metadata["fallback_to_legacy"] = True
        return _coerce_messages(fallback_handler(context))


def build_default_intent_router(*, legacy_processor: object | None = None) -> IntentRouter:
    router = IntentRouter(resolver=SignalIntentResolver())
    if legacy_processor is None:
        return router

    registry = build_legacy_domain_handler_registry(legacy_processor)
    for intent, handler in registry.build_intent_handlers().items():
        router.register_intent_handler(intent, handler)
    return router
