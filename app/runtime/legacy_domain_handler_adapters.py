from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from app.runtime.domain_handler_registry import DomainHandlerRegistry
from domain.handler_ports import (
    BookingHandlerPayload,
    BookingHandlerPort,
    DomainHandlerResponse,
    EscalationHandlerPayload,
    EscalationHandlerPort,
    ModerationHandlerPayload,
    ModerationHandlerPort,
)

BOOKING_INTENTS: tuple[str, ...] = (
    "booking",
    "booking_started",
    "fields_complete",
)
ESCALATION_INTENTS: tuple[str, ...] = (
    "escalation",
    "escalate_manual_review",
    "human_handoff",
)
MODERATION_INTENTS: tuple[str, ...] = (
    "moderation",
    "blocked_content",
    "safety_block",
)


def _invoke_legacy_processor(processor: Any, phone_number: str, message_body: str) -> list[str]:
    if hasattr(processor, "process"):
        return list(processor.process(phone_number, message_body) or [])  # type: ignore[arg-type]
    if callable(processor):
        legacy = processor
        return list(legacy(phone_number, message_body) or [])  # type: ignore[arg-type]
    raise TypeError("Legacy processor must be callable or expose a process method")


class _LegacyDomainAdapter:
    def __init__(self, legacy_processor: Callable[[str, str], Sequence[str]] | Any) -> None:
        self._legacy_processor = legacy_processor

    def _process(self, *, intent: str, phone_number: str, message_body: str, domain: str) -> DomainHandlerResponse:
        messages = _invoke_legacy_processor(self._legacy_processor, phone_number, message_body)
        return DomainHandlerResponse(
            messages=messages,
            metadata={
                "bridge": "legacy_sms_processor",
                "domain_handler_type": domain,
                "intent": intent,
            },
        )


class LegacyBookingHandlerAdapter(_LegacyDomainAdapter, BookingHandlerPort):
    def handle_booking(self, payload: BookingHandlerPayload) -> DomainHandlerResponse:
        return self._process(
            intent=payload.intent,
            phone_number=payload.phone_number,
            message_body=payload.message_body,
            domain="booking",
        )


class LegacyEscalationHandlerAdapter(_LegacyDomainAdapter, EscalationHandlerPort):
    def handle_escalation(self, payload: EscalationHandlerPayload) -> DomainHandlerResponse:
        return self._process(
            intent=payload.intent,
            phone_number=payload.phone_number,
            message_body=payload.message_body,
            domain="escalation",
        )


class LegacyModerationHandlerAdapter(_LegacyDomainAdapter, ModerationHandlerPort):
    def handle_moderation(self, payload: ModerationHandlerPayload) -> DomainHandlerResponse:
        return self._process(
            intent=payload.intent,
            phone_number=payload.phone_number,
            message_body=payload.message_body,
            domain="moderation",
        )


def build_legacy_domain_handler_registry(
    legacy_processor: Callable[[str, str], Sequence[str]] | Any,
) -> DomainHandlerRegistry:
    registry = DomainHandlerRegistry()
    registry.register_booking_handler(BOOKING_INTENTS, LegacyBookingHandlerAdapter(legacy_processor))
    registry.register_escalation_handler(ESCALATION_INTENTS, LegacyEscalationHandlerAdapter(legacy_processor))
    registry.register_moderation_handler(MODERATION_INTENTS, LegacyModerationHandlerAdapter(legacy_processor))
    return registry
