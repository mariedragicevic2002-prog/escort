"""
Guards for the conversation FSM + v1→v2 event mapping.

``target_state_to_event`` must agree with ``STATE_TRANSITIONS`` so legacy
handlers that return an absolute ``new_state`` never map to the wrong event
when two different events could change the same code path (e.g. COLLECTING).
"""

from __future__ import annotations

import pytest

from core.state_machine import (
    FSM_EVENT_NAMES,
    FsmBridgeError,
    STATE_TRANSITIONS,
    VALID_STATES,
    target_state_to_event,
    transition,
)
def test_target_state_to_event_round_trips_each_declared_edge() -> None:
    """Every (state, event) row must satisfy transition(s, e) == target via target_state_to_event(s, target)."""
    for state, events in STATE_TRANSITIONS.items():
        assert state in VALID_STATES
        for event, target in events.items():
            assert transition(state, event) == target
            if event != "stay":
                mapped = target_state_to_event(state, target)
                assert mapped == event, (
                    f"target_state_to_event({state!r}, {target!r}) got {mapped!r}, expected {event!r}"
                )


@pytest.mark.parametrize(
    "current,target,expected_event",
    [
        ("NEW", "COLLECTING", "booking_started"),
        ("NEW", "CHECKING_AVAILABILITY", "fields_complete"),
        ("COLLECTING", "CHECKING_AVAILABILITY", "fields_complete"),
        ("CHECKING_AVAILABILITY", "COLLECTING", "availability_failed"),
        ("CHECKING_AVAILABILITY", "DEPOSIT_REQUIRED", "availability_failed_deposit"),
        ("CHECKING_AVAILABILITY", "CONFIRMED", "availability_confirmed"),
        ("CHECKING_AVAILABILITY", "MANUAL_REVIEW_PENDING", "manual_review"),
    ],
)
def test_target_state_to_event_critical_booking_paths(
    current: str, target: str, expected_event: str
) -> None:
    assert target_state_to_event(current, target) == expected_event


def test_target_none_and_same_state_are_stay() -> None:
    assert target_state_to_event("COLLECTING", None) == "stay"
    assert target_state_to_event("COLLECTING", "COLLECTING") == "stay"


def test_fsm_event_names_is_union_of_table_keys() -> None:
    """Every transition row key is an FSM event name; no strays in empty dicts."""
    assert FSM_EVENT_NAMES
    for state, row in STATE_TRANSITIONS.items():
        for name in row.keys():
            assert name in FSM_EVENT_NAMES
            assert name  # non-empty string


def test_no_duplicate_target_states_per_from_state() -> None:
    """At most one event may lead to a given next state (avoid ambiguous first-wins)."""
    for state, row in STATE_TRANSITIONS.items():
        targets = list(row.values())
        assert len(targets) == len(set(targets)), (
            f"duplicate next-state in STATE_TRANSITIONS[{state!r}]: {targets}"
        )


def test_every_valid_state_has_non_empty_transition_map() -> None:
    for s in VALID_STATES:
        row = STATE_TRANSITIONS.get(s)
        assert row is not None and len(row) > 0, f"STATE_TRANSITIONS missing or empty for {s!r}"


def test_strict_fsm_bridge_raises_on_unmapped_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRICT_FSM_BRIDGE", "1")
    from core.state_machine import is_strict_fsm_bridge  # local import after setenv

    assert is_strict_fsm_bridge() is True
    with pytest.raises(FsmBridgeError):
        target_state_to_event("NEW", "CONFIRMED")


def test_non_strict_returns_stay_on_unmapped_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRICT_FSM_BRIDGE", "0")
    from core.state_machine import is_strict_fsm_bridge

    assert is_strict_fsm_bridge() is False
    assert target_state_to_event("NEW", "CONFIRMED") == "stay"
