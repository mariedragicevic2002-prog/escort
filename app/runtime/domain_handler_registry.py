from __future__ import annotations

from collections.abc import Sequence

from app.runtime.context import OrchestrationContext
from app.runtime.intent_contracts import IntentHandler
from app.runtime._utils import normalize_intent
from domain.handler_ports import (
    BookingHandlerPayload,
    BookingHandlerPort,
    DomainHandlerResponse,
    DomainHandlerType,
    EscalationHandlerPayload,
    EscalationHandlerPort,
    ModerationHandlerPayload,
    ModerationHandlerPort,
)


def _normalize_intent(value: str | None) -> str:
    return normalize_intent(value)


def _iter_intents(intents: str | Sequence[str]) -> list[str]:
    values = [intents] if isinstance(intents, str) else list(intents)
    normalized = [_normalize_intent(value) for value in values]
    return [value for value in normalized if value]


class DomainHandlerNotRegisteredError(RuntimeError):
    pass


class DomainHandlerRegistry:
    """Intent-to-domain dispatcher for typed booking/escalation/moderation handlers."""

    def __init__(self) -> None:
        self._booking_handlers: dict[str, BookingHandlerPort] = {}
        self._escalation_handlers: dict[str, EscalationHandlerPort] = {}
        self._moderation_handlers: dict[str, ModerationHandlerPort] = {}

    def register_booking_handler(self, intents: str | Sequence[str], handler: BookingHandlerPort) -> None:
        for intent in _iter_intents(intents):
            self._booking_handlers[intent] = handler

    def register_escalation_handler(self, intents: str | Sequence[str], handler: EscalationHandlerPort) -> None:
        for intent in _iter_intents(intents):
            self._escalation_handlers[intent] = handler

    def register_moderation_handler(self, intents: str | Sequence[str], handler: ModerationHandlerPort) -> None:
        for intent in _iter_intents(intents):
            self._moderation_handlers[intent] = handler

    def resolve_handler_type(self, intent: str | None) -> DomainHandlerType | None:
        normalized = _normalize_intent(intent)
        if normalized in self._booking_handlers:
            return "booking"
        if normalized in self._escalation_handlers:
            return "escalation"
        if normalized in self._moderation_handlers:
            return "moderation"
        return None

    def dispatch(self, context: OrchestrationContext, *, intent: str) -> DomainHandlerResponse:
        normalized = _normalize_intent(intent)
        metadata = dict(context.metadata)
        if normalized in self._booking_handlers:
            return self._booking_handlers[normalized].handle_booking(
                BookingHandlerPayload(
                    phone_number=context.inbound.phone_number,
                    message_body=context.inbound.body,
                    intent=normalized,
                    metadata=metadata,
                )
            )
        if normalized in self._escalation_handlers:
            return self._escalation_handlers[normalized].handle_escalation(
                EscalationHandlerPayload(
                    phone_number=context.inbound.phone_number,
                    message_body=context.inbound.body,
                    intent=normalized,
                    metadata=metadata,
                )
            )
        if normalized in self._moderation_handlers:
            return self._moderation_handlers[normalized].handle_moderation(
                ModerationHandlerPayload(
                    phone_number=context.inbound.phone_number,
                    message_body=context.inbound.body,
                    intent=normalized,
                    metadata=metadata,
                )
            )
        raise DomainHandlerNotRegisteredError(f"No domain handler is registered for intent '{normalized or intent}'")

    def build_intent_handlers(self) -> dict[str, IntentHandler]:
        handlers: dict[str, IntentHandler] = {}
        for intent in self._booking_handlers:
            handlers[intent] = self._build_intent_handler(intent)
        for intent in self._escalation_handlers:
            handlers[intent] = self._build_intent_handler(intent)
        for intent in self._moderation_handlers:
            handlers[intent] = self._build_intent_handler(intent)
        return handlers

    def _build_intent_handler(self, intent: str) -> IntentHandler:
        normalized_intent = _normalize_intent(intent)
        handler_type = self.resolve_handler_type(normalized_intent)

        def _handler(context: OrchestrationContext) -> list[str]:
            response = self.dispatch(context, intent=normalized_intent)
            if handler_type is not None:
                context.metadata["domain_handler_type"] = handler_type
            context.metadata["domain_handler_intent"] = normalized_intent
            if response.metadata:
                context.metadata["domain_handler_response"] = dict(response.metadata)
            return list(response.messages or [])

        return _handler
