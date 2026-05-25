"""
Availability check compatibility facade.

This module preserves the original public import surface while delegating the
implementation to handlers.availability_parts.main_flow.
"""

from datetime import datetime
from typing import Any

from handlers.availability_parts.main_flow import (
    _build_time_rule_slots as _build_time_rule_slots_impl,
    _round_to_nearest_minutes as _round_to_nearest_minutes_impl,
    handle_check_availability as _handle_check_availability_impl,
    handle_manual_review_pending as _handle_manual_review_pending_impl,
    handle_unknown_in_checking as _handle_unknown_in_checking_impl,
)


def _round_to_nearest_minutes(dt: datetime, minutes: int) -> datetime:
    return _round_to_nearest_minutes_impl(dt, minutes)


def _build_time_rule_slots(
    booking_fields: dict[str, Any],
    is_outcall: bool,
    max_results: int = 3,
    window_end_override=None,
):
    return _build_time_rule_slots_impl(
        booking_fields,
        is_outcall,
        max_results=max_results,
        window_end_override=window_end_override,
    )


def handle_check_availability(context: dict[str, Any]) -> dict[str, Any]:
    return _handle_check_availability_impl(context)


def handle_unknown_in_checking(context: dict[str, Any]) -> dict[str, Any]:
    return _handle_unknown_in_checking_impl(context)


def handle_manual_review_pending(context: dict[str, Any]) -> dict[str, Any]:
    return _handle_manual_review_pending_impl(context)

