from __future__ import annotations

import logging
from collections.abc import Sequence

from app.policy.contracts import (
    RuntimePolicyDecision,
    RuntimePolicyInput,
    RuntimePolicyProvider,
    RuntimePolicyReason,
    RuntimePolicyResult,
)
from app.policy.providers import DuplicateInboundPolicy, TerminalRoutingPolicy

logger = logging.getLogger(__name__)


def _merge_error_details(result: RuntimePolicyResult, *, errored_providers: Sequence[str]) -> RuntimePolicyResult:
    details = dict(result.details)
    details["errored_policy_providers"] = tuple(errored_providers)
    return RuntimePolicyResult(
        decision=result.decision,
        reason=result.reason,
        provider_name=result.provider_name,
        messages=result.messages,
        details=details,
        fallback_used=True,
    )


class RuntimePolicyEngine:
    """Ordered runtime policy evaluator with fail-open provider fallback."""

    def __init__(
        self,
        *,
        providers: Sequence[RuntimePolicyProvider] | None = None,
        fallback_provider: RuntimePolicyProvider | None = None,
    ) -> None:
        self._providers = tuple(providers or (DuplicateInboundPolicy(), TerminalRoutingPolicy()))
        self._fallback_provider = fallback_provider or TerminalRoutingPolicy(name="legacy_terminal_fallback_policy")

    def evaluate(self, policy_input: RuntimePolicyInput) -> RuntimePolicyResult:
        errored_providers: list[str] = []
        for provider in self._providers:
            try:
                outcome = provider.evaluate(policy_input)
            except Exception:
                logger.exception("Runtime policy provider failed: %s", provider.name)
                errored_providers.append(provider.name)
                continue
            if outcome is None:
                continue
            if errored_providers:
                return _merge_error_details(outcome, errored_providers=errored_providers)
            return outcome
        fallback = self._evaluate_fallback(policy_input, errored_providers=errored_providers)
        return fallback

    def _evaluate_fallback(
        self,
        policy_input: RuntimePolicyInput,
        *,
        errored_providers: Sequence[str],
    ) -> RuntimePolicyResult:
        provider = self._fallback_provider
        outcome: RuntimePolicyResult | None = None
        if provider is not None:
            try:
                outcome = provider.evaluate(policy_input)
            except Exception:
                logger.exception("Runtime policy fallback provider failed: %s", provider.name)

        if outcome is None:
            default_decision = (
                RuntimePolicyDecision.ALLOW
                if policy_input.has_intent_router
                else RuntimePolicyDecision.FALLBACK_TO_LEGACY
            )
            outcome = RuntimePolicyResult(
                decision=default_decision,
                reason=RuntimePolicyReason.POLICY_PROVIDER_ERROR
                if errored_providers
                else RuntimePolicyReason.LEGACY_TERMINAL_FALLBACK,
                provider_name="runtime_policy_engine",
            )
        if errored_providers:
            return _merge_error_details(outcome, errored_providers=errored_providers)
        return outcome


def build_default_runtime_policy_engine() -> RuntimePolicyEngine:
    return RuntimePolicyEngine()
