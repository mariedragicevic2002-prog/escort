"""
handlers/availability_parts/time_rules.py

Time rule helpers extracted from main_flow.py:
  _mark_followup_task_failure, _round_to_nearest_minutes, _build_time_rule_slots
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT

import logging
from datetime import datetime, timedelta
from typing import Any


logger = logging.getLogger("handlers.availability_check")


def _mark_followup_task_failure(state_manager, phone_number: str, task_name: str, exc: Exception) -> None:
    """Persist follow-up failures so they are visible/retryable by ops tooling."""
    try:
        state_manager.update_fields(
            phone_number,
            {
                "post_confirm_followups_pending": True,
                "post_confirm_last_failed_task": task_name,
                "post_confirm_last_error_type": type(exc).__name__,
            },
        )
    except Exception as update_err:
        logger.warning("Failed to persist follow-up failure marker: %s", type(update_err).__name__)


def _round_to_nearest_minutes(dt: datetime, minutes: int) -> datetime:
    """Round a datetime up to the nearest multiple of `minutes`."""
    if minutes <= 0:
        return dt
    total_seconds = dt.hour * 3600 + dt.minute * 60 + dt.second
    step = minutes * 60
    rounded = ((total_seconds + step - 1) // step) * step
    return dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(seconds=rounded)


def _build_time_rule_slots(booking_fields: dict[str, Any], is_outcall: bool, max_results: int = 3,
                           window_end_override=None):
    """Build 3 timeslot suggestions using the chatbot time rules.

    Rules:
      - If user did not specify a future date, assume today.
      - Start from requested time (if given) otherwise now + 30m, rounded to 5m.
      - Slot length is 1 hour.
      - If now is between 12pm-9pm: suggest up to midnight.
      - If now is after 9pm: suggest up to 6am next day.
      - Skip any slots that conflict on calendar.
    """
    from datetime import time

    from services.calendar_service import check_conflict, check_outcall_conflict_with_travel
    from utils.timezone import get_current_datetime

    now = get_current_datetime()
    booking_date = booking_fields.get('date')
    booking_time = booking_fields.get('time')

    start_dt = None
    if booking_date and booking_time:
        try:
            hour, minute = booking_time
            start_dt = datetime.combine(booking_date, time(hour=hour, minute=minute))
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            start_dt = None

    if not start_dt:
        start_dt = now

    # GOLDEN RULE: alternatives should be near the requested time, not just after it.
    # Start from 2 hours before the requested time so suggestions bracket the requested slot.
    # Floor at now+30min so we never suggest times already past.
    _near_start = start_dt - timedelta(hours=2)
    _floor = now + timedelta(minutes=30)
    _floor = _floor.replace(second=0, microsecond=0)
    _rem_f = _floor.minute % 15
    if _rem_f != 0:
        _floor = _floor + timedelta(minutes=15 - _rem_f)
    _candidate = max(_near_start, _floor)
    _candidate = _candidate.replace(second=0, microsecond=0)
    _rem = _candidate.minute % 15
    if _rem != 0:
        _candidate = _candidate + timedelta(minutes=15 - _rem)
    start_dt = _candidate

    from utils.availability_slots import get_business_hours, normalize_business_hours_pair

    _bh = normalize_business_hours_pair(get_business_hours()) or (11, 4)
    _, end_hour = _bh

    if window_end_override is not None:
        end_dt = window_end_override
    elif now.hour < end_hour:
        # Late night / early morning — cap at service-night end same day
        end_dt = datetime.combine(now.date(), time(end_hour, 0))
    else:
        # All other times: extend to end_hour next calendar day so overnight
        # service slots are never cut off by a midnight boundary
        next_day = now.date() + timedelta(days=1)
        end_dt = datetime.combine(next_day, time(end_hour, 0))

    slot_duration = timedelta(hours=1)
    slots = []
    candidate = start_dt

    while candidate <= end_dt and len(slots) < max_results:
        if candidate >= now:
            test_fields = dict(booking_fields)
            test_fields['date'] = candidate.date()
            test_fields['time'] = (candidate.hour, candidate.minute)
            test_fields['duration'] = 60

            if is_outcall:
                conflict_type, _ = check_outcall_conflict_with_travel(test_fields)
            else:
                conflict_type, _ = check_conflict(test_fields)

            # 'graphite' = soft-hold only — slot is available for new bookings (deposit required)
            if conflict_type in ('none', 'graphite'):
                slots.append(candidate)

        candidate += slot_duration

    return slots
