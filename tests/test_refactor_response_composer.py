from __future__ import annotations

from app.runtime.context import OrchestrationOutcome
from app.runtime.response_composer import compose_response


def test_compose_response_normalizes_messages_and_actions() -> None:
    payload = [
        " hello ",
        {"message": "world"},
        {"messages": ["third", "", "   "]},
        {"action": "handoff_to_human"},
        {"actions": [{"name": "notify_ops", "priority": "high"}, "audit"]},
        None,
    ]

    composed = compose_response(payload)

    assert composed.messages == ["hello", "world", "third"]
    assert composed.actions == [
        {"name": "handoff_to_human"},
        {"name": "notify_ops", "priority": "high"},
        {"name": "audit"},
    ]


def test_compose_response_accepts_runtime_outcome_shapes() -> None:
    outcome = OrchestrationOutcome(
        messages=["first", "second"],
        actions=[{"name": "transition"}],
        duplicate=False,
    )

    composed = compose_response(outcome)

    assert composed.messages == ["first", "second"]
    assert composed.actions == [{"name": "transition"}]

