"""
handlers/availability_parts/main_flow.py

Thin re-exporter. Logic lives in focused sub-modules:
  locking                 — Redis/in-process booking lock helpers
  time_rules              — _build_time_rule_slots, _round_to_nearest_minutes
  availability_check_impl — handle_check_availability, handle_unknown_in_checking, handle_manual_review_pending
"""
from handlers.availability_parts.locking import (  # noqa: F401
    _LOCAL_BOOKING_LOCKS,
    _LOCAL_BOOKING_LOCKS_GUARD,
    _acquire_booking_lock,
    _booking_lock_key,
    _finalization_booking_identity_key,
    _release_booking_lock,
    _require_redis_booking_lock,
    _truthy_env,
)
from handlers.availability_parts.time_rules import (  # noqa: F401
    _build_time_rule_slots,
    _mark_followup_task_failure,
    _round_to_nearest_minutes,
)
from handlers.availability_parts.availability_check_impl import (  # noqa: F401
    handle_check_availability,
    handle_manual_review_pending,
    handle_unknown_in_checking,
)
