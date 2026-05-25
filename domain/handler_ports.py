from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

DomainHandlerType = Literal["booking", "escalation", "moderation"]


@dataclass(frozen=True)
class DomainHandlerPayload:
    phone_number: str
    message_body: str
    intent: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BookingHandlerPayload(DomainHandlerPayload):
    pass


@dataclass(frozen=True)
class EscalationHandlerPayload(DomainHandlerPayload):
    pass


@dataclass(frozen=True)
class ModerationHandlerPayload(DomainHandlerPayload):
    pass


@dataclass(frozen=True)
class DomainHandlerResponse:
    messages: list[str]
    metadata: Mapping[str, Any] = field(default_factory=dict)


class BookingHandlerPort(Protocol):
    def handle_booking(self, payload: BookingHandlerPayload) -> DomainHandlerResponse:
        ...


class EscalationHandlerPort(Protocol):
    def handle_escalation(self, payload: EscalationHandlerPayload) -> DomainHandlerResponse:
        ...


class ModerationHandlerPort(Protocol):
    def handle_moderation(self, payload: ModerationHandlerPayload) -> DomainHandlerResponse:
        ...
