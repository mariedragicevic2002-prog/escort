from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol


class RuntimePolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    EARLY_EXIT = "early_exit"
    FALLBACK_TO_LEGACY = "fallback_to_legacy"


class RuntimePolicyReason(str, Enum):
    DUPLICATE_INBOUND = "duplicate_inbound"
    INTENT_ROUTER_PRESENT = "intent_router_present"
    INTENT_ROUTER_MISSING = "intent_router_missing"
    LEGACY_TERMINAL_FALLBACK = "legacy_terminal_fallback"
    POLICY_PROVIDER_ERROR = "policy_provider_error"
    UNSPECIFIED = "unspecified"


@dataclass(frozen=True)
class RuntimePolicyInput:
    phone_number: str
    message_body: str
    request_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    has_intent_router: bool = False


@dataclass(frozen=True)
class RuntimePolicyResult:
    decision: RuntimePolicyDecision
    reason: RuntimePolicyReason
    provider_name: str
    messages: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)
    fallback_used: bool = False


class RuntimePolicyProvider(Protocol):
    name: str

    def evaluate(self, policy_input: RuntimePolicyInput) -> RuntimePolicyResult | None:
        ...
