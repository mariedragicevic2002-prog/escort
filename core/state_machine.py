"""
core/state_machine.py

Central declarative state machine for the Adella chatbot.

All valid states and ALL state transitions are defined here as the single
source of truth.  Handlers (v2) return an *event* name; the router calls
``transition()`` to resolve the next state.  The existing StateManager
continues to own the DB write and optimistic-lock; it delegates transition
validation here so both paths share the same rule-set.

Backward-compatibility note
---------------------------
Existing v1 handlers that set ``new_state`` directly in their return dict
are still fully supported by the router.  Only handlers that opt-in to
``flow_version == "v2"`` return ``(event, response)`` tuples and go through
``transition()`` below.
"""

from __future__ import annotations

import logging
import os
from typing import Final

from utils.structured_logging import log_quality_metric

logger = logging.getLogger("adella_chatbot.state_machine")


class FsmBridgeError(RuntimeError):
    """V1 new_state could not be mapped to a one-step FSM event (STRICT_FSM_BRIDGE)."""

    def __init__(self, current_state: str, target: str | None, reason: str) -> None:
        self.current_state = current_state
        self.target = target
        self.reason = reason
        super().__init__(f"FsmBridgeError({current_state!r} -> {target!r}): {reason}")


def is_strict_fsm_bridge() -> bool:
    """
    When true, ``target_state_to_event`` raises FsmBridgeError on invalid
    or unmapped (current_state, target) pairs instead of returning ``stay``.
    Set env ``STRICT_FSM_BRIDGE=1`` (or ``true`` / ``yes``).
    """
    v = (os.environ.get("STRICT_FSM_BRIDGE") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# 1. Canonical state registry
# ---------------------------------------------------------------------------

VALID_STATES: Final[frozenset[str]] = frozenset({
    "NEW",
    "COLLECTING",
    "CHECKING_AVAILABILITY",
    "DEPOSIT_REQUIRED",
    "CONFIRMED",
    "POST_BOOKING",
    "EXTENDED_ENQUIRY",
    "MANUAL_REVIEW_PENDING",
})

# ---------------------------------------------------------------------------
# 2. Declarative event-driven transition table
#
# Structure:  STATE -> { event -> next_state }
#
# Only add an entry when the transition is *intentional*.  The engine will
# warn and hold the current state for any (state, event) pair not listed.
# ---------------------------------------------------------------------------

STATE_TRANSITIONS: Final[dict[str, dict[str, str]]] = {
    "NEW": {
        # Client starts filling in booking details
        "booking_started":      "COLLECTING",
        # All required fields present in one shot (e.g. first message) — go straight to calendar check
        "fields_complete":      "CHECKING_AVAILABILITY",
        # Client sends extended service / availability enquiry
        "extended_enquiry":     "EXTENDED_ENQUIRY",
        # Edge case: deposit was already set externally and link was resent
        "deposit_required":     "DEPOSIT_REQUIRED",
        # Self-loop: intents that keep us in NEW (greeting, rates, etc.)
        "stay":                 "NEW",
    },
    "COLLECTING": {
        # All required fields collected — move to calendar check
        "fields_complete":      "CHECKING_AVAILABILITY",
        # Deposit needed before confirming (outcall / new client / etc.)
        "deposit_required":     "DEPOSIT_REQUIRED",
        # Rare: operator manually confirmed without full flow
        "confirmed":            "CONFIRMED",
        # Client or operator resets the conversation
        "reset":                "NEW",
        # Client pivots to an extended service / rates enquiry
        "extended_enquiry":     "EXTENDED_ENQUIRY",
        # Self-loop: still collecting, nothing resolved yet
        "stay":                 "COLLECTING",
    },
    "CHECKING_AVAILABILITY": {
        # Slot is free — booking confirmed
        "availability_confirmed":  "CONFIRMED",
        # Slot taken AND deposit path triggered
        "availability_failed_deposit": "DEPOSIT_REQUIRED",
        # Slot taken, no deposit required — fall back to collecting
        "availability_failed":  "COLLECTING",
        # Operator or system routes to manual review
        "manual_review":        "MANUAL_REVIEW_PENDING",
        # Client cancels while availability is being checked
        "cancelled":            "NEW",
        # Self-loop
        "stay":                 "CHECKING_AVAILABILITY",
    },
    "DEPOSIT_REQUIRED": {
        # Deposit screenshot received and accepted
        "deposit_paid":         "CONFIRMED",
        # Self-loop: query / partial screenshot / refuse handled inline
        "stay":                 "DEPOSIT_REQUIRED",
    },
    "CONFIRMED": {
        # Appointment has ended; move to post-booking nurture
        "booking_complete":     "POST_BOOKING",
        # Booking was cancelled after confirmation
        "cancelled":            "NEW",
        # Self-loop: greeting / rates / service enquiry while confirmed
        "stay":                 "CONFIRMED",
    },
    "POST_BOOKING": {
        # Client starts a new booking inquiry
        "booking_started":      "COLLECTING",
        # Client resets / says goodbye
        "reset":                "NEW",
        # Self-loop
        "stay":                 "POST_BOOKING",
    },
    "EXTENDED_ENQUIRY": {
        # Client pivots from enquiry to booking
        "booking_started":      "COLLECTING",
        # Reset
        "reset":                "NEW",
        # Self-loop
        "stay":                 "EXTENDED_ENQUIRY",
    },
    "MANUAL_REVIEW_PENDING": {
        # Operator has reviewed and resolved — push back to collecting
        "resolved":             "COLLECTING",
        # Operator cancelled the booking
        "cancelled":            "NEW",
        # Self-loop
        "stay":                 "MANUAL_REVIEW_PENDING",
    },
}

FSM_EVENT_NAMES: Final[frozenset[str]] = frozenset(
    event_name for row in STATE_TRANSITIONS.values() for event_name in row.keys()
)

# ---------------------------------------------------------------------------
# 3. Transition engine
# ---------------------------------------------------------------------------

def transition(current_state: str, event: str) -> str:
    """
    Resolve the next state for a given *current_state* + *event* pair.

    Rules
    -----
    * Returns the correct next state when the (state, event) pair exists.
    * Returns *current_state* unchanged (no crash) when the pair is invalid.
    * Always logs: previous state, event, resolved next state.

    Args:
        current_state:  The caller's current FSM state string.
        event:          The event emitted by the handler.

    Returns:
        Next state string (may equal current_state if event is invalid).
    """
    # Safety guard — unknown current states are a bug, but we must not crash.
    if current_state not in VALID_STATES:
        logger.warning(
            "state_machine.transition: unknown current_state=%r (event=%r) — defaulting to NEW",
            current_state, event,
        )
        current_state = "NEW"

    state_events = STATE_TRANSITIONS.get(current_state, {})
    next_state = state_events.get(event)

    if next_state is None:
        logger.warning(
            "state_machine.transition: no rule for (%r, %r) — holding state",
            current_state, event,
        )
        return current_state

    logger.info(
        "state_machine.transition: %r --[%s]--> %r",
        current_state, event, next_state,
    )
    return next_state


# ---------------------------------------------------------------------------
# 4. Validation helpers (used by StateManager and safety guards)
# ---------------------------------------------------------------------------

def is_valid_state(state: str) -> bool:
    """Return True if *state* is a recognised FSM state."""
    return state in VALID_STATES


def assert_valid_state(state: str) -> None:
    """Raise AssertionError if *state* is not a recognised FSM state."""
    assert state in VALID_STATES, (
        f"Invalid state {state!r}. Valid states: {sorted(VALID_STATES)}"
    )


def get_valid_events_for_state(state: str) -> list[str]:
    """Return the list of events that are defined for *state*."""
    return list(STATE_TRANSITIONS.get(state, {}).keys())


def is_valid_transition(from_state: str, to_state: str) -> bool:
    """
    Return True if any event in *from_state* leads to *to_state*.

    Used by StateManager to validate direct state-to-state transitions
    (v1 path) against the same rule-set as the event-driven path (v2).
    """
    return to_state in STATE_TRANSITIONS.get(from_state, {}).values()


def target_state_to_event(current_state: str, target: str | None) -> str:
    """
    Map a v1-style handler ``new_state`` (absolute target) to a single FSM *event*.

    ``Router.route_v2`` runs legacy v1 handlers and must translate their return
    value into an event. The same target string (e.g. ``\"COLLECTING\"``) can
    require **different** events depending on the current state: for example
    ``CHECKING_AVAILABILITY`` → ``COLLECTING`` is ``availability_failed``, but
    ``NEW`` → ``COLLECTING`` is ``booking_started``. A static target→event
    table can therefore apply the wrong event and leave the DB stuck in the
    wrong state (see e.g. slot-unavailable flow).

    Invariants
    ----------
    * ``target is None`` → ``stay`` (no change).
    * ``target == current_state`` → ``stay``.
    * Otherwise, return the first *event* in ``STATE_TRANSITIONS[current_state]``
      whose value equals ``target``.

    If there is no one-step path: with ``STRICT_FSM_BRIDGE`` (env) raises
    :exc:`FsmBridgeError`; otherwise logs at ERROR with ``fsm_bridge_failure=1``
    in the log record extra and returns ``\"stay\"`` (legacy behaviour).
    """
    if target is None:
        return "stay"
    if not is_valid_state(target):
        msg = f"invalid target state {target!r} from {current_state!r}"
        if is_strict_fsm_bridge():
            log_quality_metric(
                "fsm_bridge_mapping_failed",
                current_state=current_state,
                target_state=target,
                reason="invalid_target_state",
            )
            raise FsmBridgeError(current_state, target, "invalid target state")
        logger.error("target_state_to_event: %s — stay", msg, extra={"fsm_bridge_failure": 1})
        log_quality_metric(
            "fsm_bridge_mapping_failed",
            current_state=current_state,
            target_state=target,
            reason="invalid_target_state",
        )
        return "stay"
    if not is_valid_state(current_state):
        msg = f"invalid current state {current_state!r}"
        if is_strict_fsm_bridge():
            log_quality_metric(
                "fsm_bridge_mapping_failed",
                current_state=current_state,
                target_state=target,
                reason="invalid_current_state",
            )
            raise FsmBridgeError(current_state, target, "invalid current state")
        logger.error("target_state_to_event: %s — stay", msg, extra={"fsm_bridge_failure": 1})
        log_quality_metric(
            "fsm_bridge_mapping_failed",
            current_state=current_state,
            target_state=target,
            reason="invalid_current_state",
        )
        return "stay"
    if target == current_state:
        return "stay"

    for event, nxt in STATE_TRANSITIONS.get(current_state, {}).items():
        if nxt == target:
            return event

    msg = (
        f"no one-step FSM event from {current_state!r} to {target!r} (check STATE_TRANSITIONS)"
    )
    if is_strict_fsm_bridge():
        log_quality_metric(
            "fsm_bridge_mapping_failed",
            current_state=current_state,
            target_state=target,
            reason="no_one_step_transition",
        )
        raise FsmBridgeError(current_state, target, "no one-step transition in STATE_TRANSITIONS")
    logger.error("target_state_to_event: %s — stay", msg, extra={"fsm_bridge_failure": 1})
    log_quality_metric(
        "fsm_bridge_mapping_failed",
        current_state=current_state,
        target_state=target,
        reason="no_one_step_transition",
    )
    return "stay"
