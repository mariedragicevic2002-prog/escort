import pytest

from core.state_machine import ALLOWED_TRANSITIONS, TransitionError, is_terminal, validate_transition

ALL_STATES = tuple(ALLOWED_TRANSITIONS)
VALID_TRANSITIONS = [
    (from_state, to_state)
    for from_state, next_states in ALLOWED_TRANSITIONS.items()
    for to_state in next_states
]
INVALID_TRANSITIONS = [
    (from_state, to_state)
    for from_state in ALL_STATES
    for to_state in ALL_STATES
    if to_state not in ALLOWED_TRANSITIONS[from_state]
]


@pytest.mark.parametrize(("from_state", "to_state"), VALID_TRANSITIONS)
def test_valid_transitions_succeed(from_state: str, to_state: str) -> None:
    validate_transition(from_state, to_state)


@pytest.mark.parametrize(("from_state", "to_state"), INVALID_TRANSITIONS)
def test_invalid_transitions_raise(from_state: str, to_state: str) -> None:
    with pytest.raises(TransitionError):
        validate_transition(from_state, to_state)


def test_terminal_states_are_reported() -> None:
    assert is_terminal("COMPLETED") is True
    assert is_terminal("CANCELLED") is True
    assert is_terminal("NEW") is False


def test_unknown_state_raises_transition_error() -> None:
    with pytest.raises(TransitionError, match="Unknown state"):
        validate_transition("UNKNOWN", "NEW")
