from core.ai_policy_boundary import apply_ai_decision_policy_guard


def test_policy_guard_rewrites_rate_claim():
    out = apply_ai_decision_policy_guard(
        message="what are your rates",
        reply="My rate is $1000/hr.",
        confirmed_context=False,
    )
    assert "$" not in out
    assert "booking policy" in out.lower()


def test_policy_guard_rewrites_deposit_waiver():
    out = apply_ai_decision_policy_guard(
        message="i only have cash no deposit",
        reply="No worries, no deposit needed at all.",
        confirmed_context=False,
    )
    assert "no deposit needed" not in out.lower()
    assert "depend on booking type and status" in out.lower()
