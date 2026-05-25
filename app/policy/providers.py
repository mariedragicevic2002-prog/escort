from __future__ import annotations

from app.policy.contracts import (
    RuntimePolicyDecision,
    RuntimePolicyInput,
    RuntimePolicyProvider,
    RuntimePolicyReason,
    RuntimePolicyResult,
)


class DuplicateInboundPolicy(RuntimePolicyProvider):
    name = "duplicate_inbound_policy"

    def evaluate(self, policy_input: RuntimePolicyInput) -> RuntimePolicyResult | None:
        if not bool(policy_input.metadata.get("duplicate")):
            return None
        return RuntimePolicyResult(
            decision=RuntimePolicyDecision.EARLY_EXIT,
            reason=RuntimePolicyReason.DUPLICATE_INBOUND,
            provider_name=self.name,
            details={"duplicate": True},
        )


class TerminalRoutingPolicy(RuntimePolicyProvider):
    def __init__(self, *, name: str = "terminal_routing_policy") -> None:
        self.name = name

    def evaluate(self, policy_input: RuntimePolicyInput) -> RuntimePolicyResult:
        if policy_input.has_intent_router:
            return RuntimePolicyResult(
                decision=RuntimePolicyDecision.ALLOW,
                reason=RuntimePolicyReason.INTENT_ROUTER_PRESENT,
                provider_name=self.name,
            )
        return RuntimePolicyResult(
            decision=RuntimePolicyDecision.FALLBACK_TO_LEGACY,
            reason=RuntimePolicyReason.INTENT_ROUTER_MISSING,
            provider_name=self.name,
        )
