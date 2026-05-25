from core.operator_booking_rules import (
    BAD_CONVERSATION_REGRESSIONS,
    CANONICAL_BOOKING_RULES,
    EXPERIENCE_MENU_URL,
    get_runtime_booking_guardrails_prompt,
    get_runtime_booking_regression_prompt,
)


def test_canonical_booking_rules_capture_key_operator_rules():
    priority = CANONICAL_BOOKING_RULES["rule_priority"]
    defaults = CANONICAL_BOOKING_RULES["conversation_defaults"]
    collection = CANONICAL_BOOKING_RULES["booking_collection"]
    special = CANONICAL_BOOKING_RULES["special_booking_policy"]
    safety = CANONICAL_BOOKING_RULES["deposit_and_safety"]
    availability = CANONICAL_BOOKING_RULES["availability_and_slots"]
    reservation = CANONICAL_BOOKING_RULES["reservation_and_manual_review"]
    lateness = CANONICAL_BOOKING_RULES["lateness_and_access"]
    handoff = CANONICAL_BOOKING_RULES["handoff_and_sync"]

    assert any("precedence" in rule.lower() for rule in priority)
    assert any("today unless" in rule.lower() for rule in defaults)
    assert any("1 hour booking" in rule.lower() for rule in defaults)
    assert any("at most two questions" in rule.lower() for rule in defaults)
    assert any(EXPERIENCE_MENU_URL in rule for rule in collection)
    assert any("manual review" in rule.lower() for rule in special)
    assert any("unsafe words" in rule.lower() for rule in safety)
    assert any("blocking overrides enquiry" in rule.lower() for rule in safety)
    assert any("round the inbound time up" in rule.lower() for rule in availability)
    assert any("assumed 1 hour incall" in rule.lower() for rule in availability)
    assert any("booked over" in rule.lower() for rule in reservation)
    assert any("10-minute leeway" in rule.lower() for rule in lateness)
    assert any("3-strike" in rule.lower() for rule in handoff)
    assert any("5 consecutive spam" in rule.lower() for rule in handoff)
    assert any("dont offer those type of services" in rule.lower() for rule in handoff)


def test_bad_conversation_regression_keeps_duration_and_experience_link_expectations():
    regression = BAD_CONVERSATION_REGRESSIONS[0]

    assert regression["client_reply"] == "30 mins"
    expected = regression["expected_behaviour"]
    observed = regression["observed_failures"]

    assert any("two questions" in item.lower() for item in expected)  # type: ignore[union-attr]
    assert any("experience menu link" in item.lower() for item in observed)  # type: ignore[union-attr]
    assert any("repeat the same valid detail" in item.lower() for item in expected)  # type: ignore[union-attr]


def test_runtime_prompts_include_operator_rules_and_regression_reminder():
    rules_prompt = get_runtime_booking_guardrails_prompt()
    regression_prompt = get_runtime_booking_regression_prompt()

    assert "Operator booking rules:" in rules_prompt
    assert "Ask at most two questions in one SMS." in rules_prompt
    assert EXPERIENCE_MENU_URL in rules_prompt
    assert "refuse the discount request" in rules_prompt
    assert "blocking overrides ENQUIRY" in rules_prompt
    assert "assume today, assume incall, and assume a 1 hour booking" in rules_prompt
    assert "always round up to the next 15-minute increment" in rules_prompt
    assert "5 consecutive spam" in rules_prompt
    assert "3-strike repeat policy" in rules_prompt
    assert "Recent regression to avoid:" in regression_prompt
    assert "30 mins" in regression_prompt
