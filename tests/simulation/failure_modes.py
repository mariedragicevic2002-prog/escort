"""
Failure injection for the simulation framework.

Defines injectable failure modes that the engine applies to ~15% of conversations.
Each failure mode alters a specific step in a conversation to simulate real-world
operational problems: API timeouts, slot unavailability, session expiry, etc.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class FailureMode(str, Enum):
    API_TIMEOUT          = "api_timeout"
    SLOT_UNAVAILABLE     = "slot_unavailable"
    SESSION_EXPIRY       = "session_expiry"
    DUPLICATE_BOOKING    = "duplicate_booking"
    PAYMENT_FAILURE      = "payment_failure"
    OUTCALL_AREA_UNAVAIL = "outcall_area_unavailable"
    STALE_AVAILABILITY   = "stale_availability"
    MALFORMED_RESPONSE   = "malformed_response"
    PARTIAL_BOOKING      = "partial_booking"
    CANCELLATION_ERROR   = "cancellation_error"
    RATE_LIMIT_TRIGGER   = "rate_limit_trigger"


@dataclass
class InjectedFailure:
    mode: FailureMode
    occurs_at_step: int          # 0-indexed step at which the failure occurs
    user_visible: bool           # Whether the user sees an error message
    bot_error_message: str       # What the bot says when the failure happens
    recoverable: bool            # True = bot retries; False = conversation ends/escalates
    retry_succeeds: bool = True  # If recoverable: does the retry succeed?
    outcome_override: Optional[str] = None  # Override expected outcome if non-recoverable


# ---------------------------------------------------------------------------
# Pre-defined failure profiles (one per FailureMode)
# ---------------------------------------------------------------------------

FAILURE_PROFILES: dict[FailureMode, InjectedFailure] = {

    FailureMode.API_TIMEOUT: InjectedFailure(
        mode=FailureMode.API_TIMEOUT,
        occurs_at_step=4,
        user_visible=True,
        bot_error_message=(
            "Sorry, I'm having a little trouble processing that right now — "
            "give me just a moment and I'll try again!"
        ),
        recoverable=True,
        retry_succeeds=True,
    ),

    FailureMode.SLOT_UNAVAILABLE: InjectedFailure(
        mode=FailureMode.SLOT_UNAVAILABLE,
        occurs_at_step=2,
        user_visible=True,
        bot_error_message=(
            "Sorry, that slot's actually been taken just now! "
            "Would a different time work for you?"
        ),
        recoverable=True,
        retry_succeeds=True,
    ),

    FailureMode.SESSION_EXPIRY: InjectedFailure(
        mode=FailureMode.SESSION_EXPIRY,
        occurs_at_step=3,
        user_visible=True,
        bot_error_message=(
            "Hey! It looks like our session timed out. "
            "No worries — would you like to start fresh with your booking?"
        ),
        recoverable=False,
        retry_succeeds=False,
        outcome_override="failed",
    ),

    FailureMode.DUPLICATE_BOOKING: InjectedFailure(
        mode=FailureMode.DUPLICATE_BOOKING,
        occurs_at_step=5,
        user_visible=True,
        bot_error_message=(
            "Hey, it looks like you already have a booking for that date and time! "
            "Want me to check the details of your existing booking?"
        ),
        recoverable=False,
        retry_succeeds=False,
        outcome_override="failed",
    ),

    FailureMode.PAYMENT_FAILURE: InjectedFailure(
        mode=FailureMode.PAYMENT_FAILURE,
        occurs_at_step=4,
        user_visible=True,
        bot_error_message=(
            "Hmm, there seems to be an issue with the payment confirmation. "
            "Let me flag this for you — someone will follow up shortly."
        ),
        recoverable=False,
        retry_succeeds=False,
        outcome_override="failed",
    ),

    FailureMode.OUTCALL_AREA_UNAVAIL: InjectedFailure(
        mode=FailureMode.OUTCALL_AREA_UNAVAIL,
        occurs_at_step=2,
        user_visible=True,
        bot_error_message=(
            "Sorry, I'm not currently doing outcalls to that area. "
            "Would incall work for you, or is there another suburb I could check?"
        ),
        recoverable=True,
        retry_succeeds=True,
    ),

    FailureMode.STALE_AVAILABILITY: InjectedFailure(
        mode=FailureMode.STALE_AVAILABILITY,
        occurs_at_step=3,
        user_visible=True,
        bot_error_message=(
            "Sorry — my availability just updated and that time is no longer free. "
            "Do you have a second preference?"
        ),
        recoverable=True,
        retry_succeeds=True,
    ),

    FailureMode.MALFORMED_RESPONSE: InjectedFailure(
        mode=FailureMode.MALFORMED_RESPONSE,
        occurs_at_step=4,
        user_visible=True,
        bot_error_message=(
            "Sorry, something's not quite right on my end. "
            "Could you resend your details and I'll sort that out?"
        ),
        recoverable=True,
        retry_succeeds=True,
    ),

    FailureMode.PARTIAL_BOOKING: InjectedFailure(
        mode=FailureMode.PARTIAL_BOOKING,
        occurs_at_step=5,
        user_visible=True,
        bot_error_message=(
            "Your booking went through but there was a partial issue confirming the time. "
            "Could you confirm the details again for me?"
        ),
        recoverable=True,
        retry_succeeds=True,
    ),

    FailureMode.CANCELLATION_ERROR: InjectedFailure(
        mode=FailureMode.CANCELLATION_ERROR,
        occurs_at_step=5,
        user_visible=True,
        bot_error_message=(
            "I'm having trouble processing the cancellation right now. "
            "I'll flag this and someone will confirm the cancellation with you shortly."
        ),
        recoverable=False,
        retry_succeeds=False,
        outcome_override="failed",
    ),

    FailureMode.RATE_LIMIT_TRIGGER: InjectedFailure(
        mode=FailureMode.RATE_LIMIT_TRIGGER,
        occurs_at_step=2,
        user_visible=True,
        bot_error_message=(
            "You've been sending a lot of messages — "
            "I've had to temporarily slow things down. "
            "Please wait a moment before continuing."
        ),
        recoverable=False,
        retry_succeeds=False,
        outcome_override="blocked",
    ),
}


def get_failure(mode: FailureMode) -> InjectedFailure:
    """Return the pre-defined failure profile for the given mode."""
    return FAILURE_PROFILES[mode]


# ---------------------------------------------------------------------------
# Injector: decides whether and what failure to inject for a given conversation
# ---------------------------------------------------------------------------

class FailureInjector:
    """
    Injects failures into a given percentage of conversations.

    Parameters
    ----------
    injection_rate : float
        Probability (0–1) that any given conversation gets a failure injected.
    rng : random.Random | None
        Seeded RNG for reproducibility.
    """

    def __init__(self, injection_rate: float = 0.15,
                 rng: random.Random | None = None) -> None:
        self.injection_rate = injection_rate
        self._rng = rng or random.Random()
        self._all_modes = list(FAILURE_PROFILES.keys())

    def should_inject(self) -> bool:
        return self._rng.random() < self.injection_rate

    def pick_failure(self, preferred_mode: Optional[str] = None) -> InjectedFailure:
        """
        Return a failure profile. Uses ``preferred_mode`` if provided and valid,
        otherwise picks a random mode.
        """
        if preferred_mode:
            try:
                mode = FailureMode(preferred_mode)
                return FAILURE_PROFILES[mode]
            except (ValueError, KeyError):
                pass
        mode = self._rng.choice(self._all_modes)
        return FAILURE_PROFILES[mode]

    def maybe_inject(self, preferred_mode: Optional[str] = None) -> Optional[InjectedFailure]:
        """
        Probabilistically inject a failure. Returns ``None`` if not injecting.
        """
        if self.should_inject():
            return self.pick_failure(preferred_mode)
        return None
