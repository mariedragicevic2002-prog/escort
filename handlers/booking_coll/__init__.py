# ruff: noqa: F401
"""
handlers/booking_coll/__init__.py

Re-exports all public symbols from the booking_coll sub-package so that
callers using `from handlers.booking_collection import X` continue to work
unchanged after the split.
"""

from handlers.booking_coll._shared import (
    AVAILABLE_NOW_MIN_LEAD_MINUTES,
    AVAILABLE_NOW_OUTCALL_READY_BUFFER_MINUTES,
    _build_outcall_address_confirmed_msg,
    _build_three_slot_available_now_response,
    _check_doubles_supply_response,
    _extract_and_merge_booking_fields,
    _format_outside_hours_message,
    _format_perfect_timing_line,
    _get_outcall_policy_amounts,
    _handle_dinner_date_fields_message,
    _incall_duration_prompt_with_calendar_probe,
    _match_slot_selection,
    _min_hour_error_response,
    _normalize_booking_date,
    _outside_hours_clear_and_respond,
    _too_far_error_response,
    _webform_url_for_phone,
    calculate_available_now_booking_datetime,
    check_and_format_outside_hours,
    check_within_available_hours_and_days,
)
from handlers.booking_coll._provide_field import _handle_provide_field_impl, handle_provide_field
from handlers.booking_coll._provide_field_context import CollectingCtx, _OUTCALL_KWS
from handlers.booking_coll._provide_field_stages_extract import (
    _available_now_inline_calendar_check,
    _stage_doubles_gate,
    _stage_extract_and_enforce,
    _stage_fifth_message_block,
    _stage_first_contact_guard,
)
from handlers.booking_coll._provide_field_stages_finish import (
    _no_experience_branch_response,
    _stage_apply_extracted_updates_and_name,
    _stage_available_now_no_datetime_slots,
    _stage_mandatory_date_time_duration,
    _stage_missing_fields_or_transition,
    _stage_outcall_address_confirmed_after_validate,
    _stage_outcall_policy_after_validate,
    _stage_time_known_no_duration,
    _yes_check_availability,
)
from handlers.booking_coll._provide_field_stages_slot_load import (
    _stage_build_fields_to_validate,
    _stage_early_duration_fast_path,
    _stage_load_fields_and_defaults,
    _stage_nothing_extracted_shortcut,
    _stage_slot_selection,
)
from handlers.booking_coll._provide_field_stages_validate import (
    _stage_outcall_no_address_shortcircuit,
    _stage_validate_fields,
)
from handlers.booking_coll._cancel_rates import (
    handle_ask_rates,
    handle_cancel_booking,
    handle_goodbye,
)
from handlers.booking_coll._quick_booking import (
    handle_quick_booking,
    _extract_and_merge_booking_fields as _quick_extract,  # re-imported via _shared already
)

__all__ = [
    # Public API (imported by external callers)
    "handle_provide_field",
    "handle_cancel_booking",
    "handle_ask_rates",
    "handle_quick_booking",
    "handle_goodbye",
    "calculate_available_now_booking_datetime",
    "_format_outside_hours_message",
    "check_and_format_outside_hours",
    "check_within_available_hours_and_days",
    "_build_outcall_address_confirmed_msg",
    "_format_perfect_timing_line",
    # Constants
    "AVAILABLE_NOW_MIN_LEAD_MINUTES",
    "AVAILABLE_NOW_OUTCALL_READY_BUFFER_MINUTES",
    # Internal helpers (used by tests or other handlers)
    "CollectingCtx",
    "_OUTCALL_KWS",
    "_extract_and_merge_booking_fields",
    "_handle_provide_field_impl",
]
