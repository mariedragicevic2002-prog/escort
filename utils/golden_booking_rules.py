"""
Golden booking rules — single reference for SMS flows.

Unified flow (3-slot alternatives, time confirmation, outcall policy without street address):
  Applies to all standard bookings including GFE/PSE/DGFE, couples, doubles (after supply gate),
  dinner dates (extra: 5–9pm start window, 2h default, outcall — see utils.dinner_date).

EXEMPT — do not use the unified “❌ + three nearby alternatives / same slot-offer rules”:
  • Fly me to you / FMTY
  • Overnight (incl. sleepover / all-night)
  • Dirty weekend / 48-hour / weekend package style enquiries

These are handled via webform, manual review, and/or forward-to-escort notices.

MMF doubles — escort sources the other male (mandatory before confirmation):
  After date, time, and duration are set, the client MUST receive the checklist below and
  reply with which options apply (Humiliation / Voyeurism / Bisexual / Heterosexual — multi-select).
  Plain YES must NOT confirm until preferences are stored (SMS gate + CHECKING_AVAILABILITY YES guard;
  webform requires ticked boxes). Outcall bookings may append the pair-travel surcharge line from rates.

Outcall policy (client-facing):
  Never disclose the escort’s street address. Use CBD-only wording + surcharge + deposit
  from rates (deposit may vary by experience; callers pass the correct amount).

Outcall **travel surcharge** (per-trip fee) is waived for the same package-style bookings that
use all-inclusive pricing: dinner dates, overnight, fly-me-to-you, dirty weekend / 48hr, etc.
"""

from __future__ import annotations

from typing import Any

from utils.dinner_date import is_dinner_date_booking

# GOLDEN RULE — client-facing SMS body for MMF doubles when the escort arranges the male provider.
# Single source of truth for booking.mmf_exploration.mmf_exploration_sms_prompt base text.
GOLDEN_MMF_ESCORT_SOURCED_EXPLORATION_PROMPT = (
    "Can you please confirm what your wanting to explore in your MMF doubles booking:\n\n"
    "* Humiliation (have me or both of us humiliate you)\n"
    "* Voyeurism (Watch me get fucked by male bull)\n"
    "* Bisexual (get fucked/sucked by both of us)\n"
    "* Heterosexual (Just touch and fuck me only)\n\n"
    "Please note I don't offer double penetration in MMF bookings.\n\n"
    "Let me know want your wanting so I know what male escort I need to source "
    "for your booking (eg. Bisexual/Humiliation)"
)


def is_outcall_travel_surcharge_waived(booking_fields: dict[str, Any] | None) -> bool:
    """
    True when no separate outcall travel fee applies (dinner date, overnight, FMTY, dirty weekend).

    Uses the same markers as calendar/slot exemptions where relevant, plus dinner dates.
    """
    if not booking_fields:
        return False
    if is_dinner_date_booking(booking_fields):
        return True
    bt = (booking_fields.get("booking_type") or "").strip().lower()
    if bt in (
        "overnight",
        "dirty_weekend",
        "fly_me",
        "fmty",
        "fly_me_to_you",
    ):
        return True
    exp = (booking_fields.get("experience_type") or "").strip().lower()
    combined = exp
    markers = (
        "fly me",
        "fmty",
        "fly-me",
        "fly you out",
        "dirty weekend",
        "48 hour",
        "48hr",
        "whole weekend",
        "weekend away",
        "overnight",
        "sleep over",
        "sleepover",
        "all night",
    )
    return any(m in combined for m in markers)


def is_exempt_from_unified_golden_booking_flow(
    state: dict[str, Any] | None,
    message: str = "",
) -> bool:
    """
    True for FMTY / overnight / dirty-weekend style bookings that skip the standard
    calendar slot UX (alternatives list, etc.) in favour of webform / manual handling.
    """
    if not state:
        return False
    bt = (state.get("booking_type") or "").strip().lower()
    exp = (state.get("experience_type") or "").strip().lower()
    msg = (message or "").lower()

    if bt in (
        "overnight",
        "dirty_weekend",
        "fly_me",
        "fmty",
        "fly_me_to_you",
    ):
        return True

    combined = f"{exp} {msg}"
    markers = (
        "fly me",
        "fmty",
        "fly-me",
        "fly you out",
        "dirty weekend",
        "48 hour",
        "48hr",
        "whole weekend",
        "weekend away",
        "overnight",
        "sleep over",
        "sleepover",
        "all night",
    )
    return any(m in combined for m in markers)
