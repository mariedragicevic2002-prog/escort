"""
Finite State Machine for conversational booking workflow.

Pure Python — no external dependencies. This module contains ZERO I/O,
ZERO DB calls, ZERO HTTP calls. It is a pure domain rule set.

Dependency rule: nothing in this module may import from adapters/,
infrastructure/, or application/.
"""

ALLOWED_TRANSITIONS: dict[str, list[str]] = {
    "NEW": ["AWAITING_DETAILS", "CONFIRMED", "CANCELLED", "ESCALATED"],
    "AWAITING_DETAILS": ["CONFIRMED", "CANCELLED", "ESCALATED"],
    "CONFIRMED": ["COMPLETED", "CANCELLED", "RESCHEDULED"],
    "RESCHEDULED": ["CONFIRMED", "CANCELLED", "ESCALATED"],
    "COMPLETED": [],
    "CANCELLED": [],
    "ESCALATED": ["NEW", "CONFIRMED", "CANCELLED"],
}


class TransitionError(Exception):
    """Raised when a state transition is not permitted by the FSM."""


def validate_transition(from_state: str, to_state: str) -> None:
    """
    Assert that transitioning from from_state to to_state is allowed.
    Raises TransitionError if the transition is invalid.
    """
    allowed = ALLOWED_TRANSITIONS.get(from_state)
    if allowed is None:
        raise TransitionError(f"Unknown state: '{from_state}'")
    if to_state not in allowed:
        raise TransitionError(
            f"Invalid transition: '{from_state}' → '{to_state}'. "
            f"Allowed: {allowed}"
        )


def get_allowed_transitions(state: str) -> list[str]:
    """Return the list of valid next states from the given state."""
    return list(ALLOWED_TRANSITIONS.get(state, []))


def is_terminal(state: str) -> bool:
    """Return True if the state has no outbound transitions."""
    return not ALLOWED_TRANSITIONS.get(state)


def is_valid_state(state: str) -> bool:
    """Return True if state is a known FSM state."""
    return state in ALLOWED_TRANSITIONS


def is_valid_transition(from_state: str, to_state: str) -> bool:
    """Return True if the transition is permitted (no exception raised)."""
    allowed = ALLOWED_TRANSITIONS.get(from_state)
    if allowed is None:
        return False
    return to_state in allowed
