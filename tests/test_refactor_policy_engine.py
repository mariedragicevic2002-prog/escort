from __future__ import annotations

from app.policy import (
    RuntimePolicyDecision,
    RuntimePolicyEngine,
    RuntimePolicyInput,
    RuntimePolicyReason,
    RuntimePolicyResult,
    TerminalRoutingPolicy,
)


def _policy_input(*, has_intent_router: bool = True, metadata: dict | None = None) -> RuntimePolicyInput:
    return RuntimePolicyInput(
        phone_number="+61412345678",
        message_body="hello",
        request_id="req-policy",
        metadata=metadata or {},
        has_intent_router=has_intent_router,
    )


def test_policy_engine_returns_deterministic_allow_and_deny_results() -> None:
    allow_engine = RuntimePolicyEngine(
        providers=[
            _StaticProvider(
                name="allow_provider",
                result=RuntimePolicyResult(
                    decision=RuntimePolicyDecision.ALLOW,
                    reason=RuntimePolicyReason.UNSPECIFIED,
                    provider_name="allow_provider",
                ),
            )
        ],
        fallback_provider=TerminalRoutingPolicy(name="unused_fallback"),
    )
    deny_engine = RuntimePolicyEngine(
        providers=[
            _StaticProvider(
                name="deny_provider",
                result=RuntimePolicyResult(
                    decision=RuntimePolicyDecision.DENY,
                    reason=RuntimePolicyReason.UNSPECIFIED,
                    provider_name="deny_provider",
                    messages=("denied",),
                ),
            )
        ],
        fallback_provider=TerminalRoutingPolicy(name="unused_fallback"),
    )

    allow_first = allow_engine.evaluate(_policy_input())
    allow_second = allow_engine.evaluate(_policy_input())
    deny_first = deny_engine.evaluate(_policy_input())
    deny_second = deny_engine.evaluate(_policy_input())

    assert allow_first == allow_second
    assert allow_first.decision == RuntimePolicyDecision.ALLOW
    assert deny_first == deny_second
    assert deny_first.decision == RuntimePolicyDecision.DENY


def test_policy_engine_applies_first_matching_provider_in_order() -> None:
    call_order: list[str] = []
    engine = RuntimePolicyEngine(
        providers=[
            _StaticProvider(name="first_none", result=None, call_order=call_order),
            _StaticProvider(
                name="second_deny",
                result=RuntimePolicyResult(
                    decision=RuntimePolicyDecision.DENY,
                    reason=RuntimePolicyReason.UNSPECIFIED,
                    provider_name="second_deny",
                ),
                call_order=call_order,
            ),
            _StaticProvider(
                name="third_allow",
                result=RuntimePolicyResult(
                    decision=RuntimePolicyDecision.ALLOW,
                    reason=RuntimePolicyReason.UNSPECIFIED,
                    provider_name="third_allow",
                ),
                call_order=call_order,
            ),
        ],
        fallback_provider=TerminalRoutingPolicy(name="unused_fallback"),
    )

    result = engine.evaluate(_policy_input())

    assert result.provider_name == "second_deny"
    assert result.decision == RuntimePolicyDecision.DENY
    assert call_order == ["first_none", "second_deny"]


def test_policy_engine_uses_fallback_when_provider_errors() -> None:
    class _ExplodingProvider:
        name = "exploding_provider"

        def evaluate(self, _policy_input: RuntimePolicyInput) -> RuntimePolicyResult | None:
            raise RuntimeError("provider unavailable")

    engine = RuntimePolicyEngine(
        providers=[_ExplodingProvider()],
        fallback_provider=TerminalRoutingPolicy(name="fallback_policy"),
    )

    result = engine.evaluate(_policy_input(has_intent_router=False))

    assert result.decision == RuntimePolicyDecision.FALLBACK_TO_LEGACY
    assert result.reason == RuntimePolicyReason.INTENT_ROUTER_MISSING
    assert result.fallback_used is True
    assert result.details["errored_policy_providers"] == ("exploding_provider",)


class _StaticProvider:
    def __init__(
        self,
        *,
        name: str,
        result: RuntimePolicyResult | None,
        call_order: list[str] | None = None,
    ) -> None:
        self.name = name
        self._result = result
        self._call_order = call_order

    def evaluate(self, _policy_input: RuntimePolicyInput) -> RuntimePolicyResult | None:
        if self._call_order is not None:
            self._call_order.append(self.name)
        return self._result
