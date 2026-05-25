from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class InboundSMSMessage:
    """Canonical inbound SMS payload for runtime orchestration."""

    phone_number: str
    body: str
    message_data: Mapping[str, Any]
    request_payload: Mapping[str, Any]
    request_id: str
    request_headers: Mapping[str, Any] = field(default_factory=dict)
    remote_addr: str = ""


class LegacySMSProcessor(Protocol):
    """Behavior-preserving adapter to existing SMS processing logic."""

    def process(self, phone_number: str, message_body: str) -> list[str]:
        ...


@dataclass(frozen=True)
class RuntimeServices:
    """Dependencies required by the refactor runtime shell."""

    state_manager: Any
    db_service: Any
    legacy_processor: LegacySMSProcessor | Callable[[str, str], list[str]]
    transition_service: Any | None = None
    intent_router: Any | None = None
    policy_engine: Any | None = None


@dataclass
class OrchestrationContext:
    """Per-request mutable execution context."""

    inbound: InboundSMSMessage
    runtime: RuntimeServices
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OrchestrationOutcome:
    """Pipeline result returned to ingress controllers."""

    messages: list[str]
    actions: list[dict[str, Any]] = field(default_factory=list)
    duplicate: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
