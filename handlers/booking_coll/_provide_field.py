"""
handlers/booking_coll/_provide_field.py

Public handle_provide_field entrypoint and pipeline orchestration.
Re-exports symbols for handlers.booking_coll.__init__ and tests.

Migration note: stages still return absolute ``new_state``; the v2 entrypoint
``handle_provide_field_v2`` maps to an FSM event via
``core.state_machine.target_state_to_event``. A future refactor should have
stages return event names so the pipeline matches ``STATE_TRANSITIONS`` directly.
"""

from __future__ import annotations

from typing import Any

# Tests patch ``handlers.booking_coll._provide_field.get_base_url``

from handlers.booking_coll._provide_field_context import CollectingCtx
from handlers.booking_coll._provide_field_stages_extract import (
    _stage_cancel_doubles,
    _stage_doubles_gate,
    _stage_extract_and_enforce,
    _stage_fifth_message_block,
    _stage_first_contact_guard,
)
from handlers.booking_coll._provide_field_stages_geo_guard import _stage_outcall_wrong_city_correction
from handlers.booking_coll._provide_field_stages_finish import (
    _stage_apply_extracted_updates_and_name,
    _stage_available_now_no_datetime_slots,
    _stage_mandatory_date_time_duration,
    _stage_missing_fields_or_transition,
    _stage_outcall_address_confirmed_after_validate,
    _stage_outcall_policy_after_validate,
    _stage_time_known_no_duration,
)
from handlers.booking_coll._provide_field_stages_slot_load import (
    _stage_build_fields_to_validate,
    _stage_early_duration_fast_path,
    _stage_load_fields_and_defaults,
    _stage_nothing_extracted_shortcut,
    _stage_ordinal_pick_without_offered_slots,
    _stage_slot_selection,
)
from handlers.booking_coll._provide_field_stages_validate import (
    _stage_outcall_no_address_shortcircuit,
    _stage_validate_fields,
)


def _stage_policy_deposit_gate(ctx: CollectingCtx) -> dict[str, Any] | None:
    """
    Enforce policy-critical deposit triggers early in COLLECTING.

    Applies to:
    - profanity escalation (3+ cumulative abusive words)
    - filming sessions
    - fly-me-to-you / weekend package markers
    """
    state = ctx.state_manager.get_state(ctx.phone_number) or ctx.state or {}
    if state.get("deposit_required"):
        return None

    merged = {**(state or {}), **(ctx.current_fields or {}), **(ctx.extracted or {})}
    if not merged.get("incall_outcall"):
        merged["incall_outcall"] = "incall"

    from booking.deposit_handler import build_deposit_gate_response

    return build_deposit_gate_response(
        booking_fields=merged,
        phone_number=ctx.phone_number,
        state_manager=ctx.state_manager,
        client_name=(merged.get("client_name") or None),
        preamble="Before we continue, a deposit is required.",
        default_reason="booking",
        default_amount=100,
        reason_filter={"profanity", "filming", "weekend", "fly_me_to_you"},
    )


def _stage_abuse_deposit_gate(ctx: CollectingCtx) -> dict[str, Any] | None:
    """Fast gate for abusive-language escalation before other COLLECTING sub-flows."""
    state = ctx.state_manager.get_state(ctx.phone_number) or ctx.state or {}
    if not state.get("profanity_detected") or state.get("deposit_required"):
        return None

    from booking.deposit_handler import build_deposit_gate_response

    booking_fields = ctx.state_manager.get_booking_fields(ctx.phone_number) or {}
    booking_fields.setdefault("incall_outcall", (state.get("incall_outcall") or "incall"))
    return build_deposit_gate_response(
        booking_fields=booking_fields,
        phone_number=ctx.phone_number,
        state_manager=ctx.state_manager,
        client_name=(state.get("client_name") or None),
        preamble="Before we continue, a deposit is required.",
        default_reason="profanity",
        default_amount=100,
    )


def handle_provide_field(context: dict[str, Any]) -> dict[str, Any]:
    """Public COLLECTING-state handler registered by the router."""
    return _handle_provide_field_impl(context)


def _handle_provide_field_impl(context: dict[str, Any]) -> dict[str, Any]:
    """Implementation of handle_provide_field. Stages 1-23; see _stage_* helpers in submodules."""
    ctx = CollectingCtx.from_context(context)

    result = _stage_first_contact_guard(ctx)
    if result is not None:
        return result

    result = _stage_fifth_message_block(ctx)
    if result is not None:
        return result

    result = _stage_abuse_deposit_gate(ctx)
    if result is not None:
        return result

    result = _stage_cancel_doubles(ctx)
    if result is not None:
        return result

    result = _stage_doubles_gate(ctx)
    if result is not None:
        return result

    _stage_load_fields_and_defaults(ctx)

    result = _stage_outcall_wrong_city_correction(ctx)
    if result is not None:
        return result

    result = _stage_ordinal_pick_without_offered_slots(ctx)
    if result is not None:
        return result

    result = _stage_slot_selection(ctx)
    if result is not None:
        return result

    result = _stage_early_duration_fast_path(ctx)
    if result is not None:
        return result

    result = _stage_extract_and_enforce(ctx)
    if result is not None:
        return result

    result = _stage_policy_deposit_gate(ctx)
    if result is not None:
        return result

    result = _stage_nothing_extracted_shortcut(ctx)
    if result is not None:
        return result

    result = _stage_build_fields_to_validate(ctx)
    if result is not None:
        return result

    result = _stage_outcall_no_address_shortcircuit(ctx)
    if result is not None:
        return result

    result = _stage_validate_fields(ctx)
    if result is not None:
        return result

    _stage_apply_extracted_updates_and_name(ctx)

    result = _stage_outcall_policy_after_validate(ctx)
    if result is not None:
        return result

    result = _stage_outcall_address_confirmed_after_validate(ctx)
    if result is not None:
        return result

    result = _stage_time_known_no_duration(ctx)
    if result is not None:
        return result

    result = _stage_available_now_no_datetime_slots(ctx)
    if result is not None:
        return result

    result = _stage_mandatory_date_time_duration(ctx)
    if result is not None:
        return result

    return _stage_missing_fields_or_transition(ctx)
