"""
Clear stale booking-flow state when the client switches to a different booking template
mid-conversation (e.g. dinner date → couples). Without this, COLLECTING treats the pivot
message as address/time extraction and can geocode nonsense or offer wrong slots.

Also exposes lane detection used by ``dispatch_message`` so any structured enquiry
intent triggers a pivot clear when the conversation was on a *different* product lane.
"""

from __future__ import annotations

from typing import Any

from core.state_manager import ALLOWED_STATE_UPDATE_FIELDS

# Intent → canonical "lane" string for structured special bookings.
# Intents not listed here use COLLECTING handlers but do not auto-clear via lane mismatch.
STRUCTURED_ENQUIRY_INTENT_LANE: dict[str, str] = {
    "dinner_date_enquiry": "dinner_date",
    "couples_booking": "couples_booking",
    "doubles_enquiry": "doubles_mff",
    "overnight_enquiry": "overnight_enquiry",
}


def canonical_booking_lane(state: dict[str, Any]) -> str:
    """
    Best-effort lane for the *current* persisted booking flow.

    ``generic`` = standard incall/outcall collection without a special template marker.
    """
    bt = (state.get("booking_type") or "").strip().lower()
    exp = (state.get("experience_type") or "").strip().lower()
    if bt == "dinner_date" or exp in ("dinner date", "dinner_date"):
        return "dinner_date"
    if bt == "couples_booking" or exp == "couples_mff":
        return "couples_booking"
    if bt == "doubles_mff" or exp in ("doubles_mff", "Doubles MMF"):
        return "doubles_mff"
    if bt == "overnight" or "overnight" in exp or "fly me" in exp or "fmty" in exp:
        return "overnight_enquiry"
    return "generic"


def collecting_should_clear_for_structured_enquiry_switch(intent: str, state: dict[str, Any]) -> bool:
    """
    True when COLLECTING + already introduced + classified intent is a different
    structured lane than what's in state (including generic → dinner/couples/doubles).
    """
    cs = (state.get("current_state") or "").strip().upper()
    if cs != "COLLECTING" or not state.get("first_contact_sent"):
        return False
    target_lane = STRUCTURED_ENQUIRY_INTENT_LANE.get(intent)
    if target_lane is None:
        return False
    return canonical_booking_lane(state) != target_lane


def refresh_legacy_context_after_collecting_lane_switch(
    intent: str,
    legacy_context: dict[str, Any],
    *,
    state_manager: Any,
    phone_number: str,
) -> dict[str, Any]:
    """
    If the client pivoted to another structured enquiry type while COLLECTING,
    clear incompatible fields and return a shallow-copied context with fresh ``state``.

    Call from ``dispatch_message`` before router.route / route_v2.
    """
    state = legacy_context.get("state") or {}
    if not collecting_should_clear_for_structured_enquiry_switch(intent, state):
        return legacy_context
    clear_incompatible_flow_for_special_booking_pivot(state_manager, phone_number)
    fresh = state_manager.get_state(phone_number) or {}
    ctx = dict(legacy_context)
    ctx["state"] = fresh
    return ctx


def clear_incompatible_flow_for_special_booking_pivot(
    state_manager: Any,
    phone_number: str,
) -> None:
    """
    Reset fields from the previous template so the new handler owns scheduling,
    location lines, and booking_type-specific flags.

    Only ALLOWED_STATE_UPDATE_FIELDS keys are sent — unknown columns are skipped.
    """
    clears: dict[str, Any] = {
        "date": None,
        "time": None,
        "duration": None,
        "experience_type": None,
        "incall_outcall": None,
        "outcall_address": None,
        "available_now_requested": False,
        "arrival_time_minutes": None,
        "offered_slot_hours": None,
        "offered_slot_minutes": None,
        "offered_slot_date": None,
        "booking_status": None,
        "booking_type": None,
        "dinner_restaurant": None,
        "dinner_after_preference": None,
        "dinner_client_address": None,
        "dinner_client_outside_15km": False,
        "earliest_slot_auto_selected": False,
        "deposit_required": False,
        "deposit_amount": None,
        "deposit_reason": None,
        "deposit_requested_at": None,
        "outcall_awaiting_yes": False,
        "incall_awaiting_yes": False,
        "missing_fields": ["date", "time", "duration"],
    }
    filtered = {k: v for k, v in clears.items() if k in ALLOWED_STATE_UPDATE_FIELDS}
    state_manager.update_fields(phone_number, filtered)
