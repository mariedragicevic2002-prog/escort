"""

Utility for calculating available time slots for "available now" requests.
Implements 30-min grace period with 1-hour spacing between slots.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import logging
import re
import threading
from datetime import datetime, time, timedelta
from typing import Any

from utils.dinner_date import (
    DINNER_DURATION_MINUTES,
    bump_to_next_dinner_candidate,
    dinner_slot_fits_window,
)

logger = logging.getLogger("adella_chatbot.utils.availability_slots")

_HOURS_CACHE = None
_HOURS_CACHE_KEY = None
_HOURS_LOCK = threading.Lock()


def normalize_business_hours_pair(raw: Any) -> tuple[int, int] | None:
    """Coerce cached or external values to ``(start_hour_24, end_hour_24)`` or ``None``.

    Callers sometimes used ``get_business_hours() or (11, 4)``. A truthy garbage tuple like
    ``(22,)`` skipped the fallback and ``bh[1]`` raised ``IndexError`` (logged as suppressed).
    """
    if raw is None:
        return None
    if not isinstance(raw, (tuple, list)):
        return None
    if len(raw) < 2:
        return None
    try:
        return int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        return None


def get_business_hours() -> tuple[int, int] | None:
    """
    Parse the available_hours setting from the schedule page and return
    (start_hour, end_hour) as 24-hour integers, or None if not configured/parseable.

    e.g. "11am-4am, 7 days a week" \u2192 (11, 4)
         "3pm-3am, 7 days a week"  \u2192 (15, 3)

    Returns None if the setting is missing or unparseable \u2014 callers must handle this.
    """
    global _HOURS_CACHE, _HOURS_CACHE_KEY
    try:
        from core.settings_manager import get_setting
        hours_str = get_setting('available_hours', '') or ''
        if not hours_str:
            logger.warning("available_hours setting is empty \u2014 cannot determine business hours")
            return None
        # Cache hit (including explicit 24/7 \u2192 None). Repair malformed cached pairs.
        with _HOURS_LOCK:
            if hours_str == _HOURS_CACHE_KEY:
                if _HOURS_CACHE is None:
                    return None
                coerced = normalize_business_hours_pair(_HOURS_CACHE)
                if coerced is not None:
                    _HOURS_CACHE = coerced
                    return coerced

        lowered = hours_str.strip().lower()
        # Treat common "always open" strings as 24/7 (same semantics as unparseable \u2192 None below,
        # but without spamming warnings on every call).
        if re.search(
            r"\b24\s*/\s*7\b|24\s*hours?\b|always\s*open\b|open\s*24\b",
            lowered,
        ):
            with _HOURS_LOCK:
                _HOURS_CACHE_KEY = hours_str
                _HOURS_CACHE = None
            return None

        match = re.search(
            r'(\d{1,2})(?::\d{2})?\s*(am|pm)\s*[-\u2013to]+\s*(\d{1,2})(?::\d{2})?\s*(am|pm)',
            lowered,
        )
        if match:
            def _conv(h_str, period):
                h = int(h_str)
                if period == 'am':
                    return 0 if h == 12 else h
                else:
                    return h if h == 12 else h + 12
            result = normalize_business_hours_pair(
                (_conv(match.group(1), match.group(2)), _conv(match.group(3), match.group(4)))
            )
            if result is None:
                with _HOURS_LOCK:
                    _HOURS_CACHE_KEY = hours_str
                    _HOURS_CACHE = None
                return None
            with _HOURS_LOCK:
                _HOURS_CACHE_KEY = hours_str
                _HOURS_CACHE = result
            return result
        logger.warning(
            "Could not parse available_hours setting '%s' \u2014 treating as 24/7 (all times allowed until configured)",
            hours_str,
        )
        with _HOURS_LOCK:
            _HOURS_CACHE_KEY = hours_str
            _HOURS_CACHE = None
        return None
    except Exception as e:
        logger.warning("Could not read available_hours setting: %s", e)
    return None


def persist_offered_slots_from_time_slot_pairs(
    phone_number: str,
    state_manager: Any,
    time_slots: list[tuple[datetime, str]],
) -> None:
    """
    Store offered slot clocks in conversation state so COLLECTING Stage 6 can bind
    shorthand replies (e.g. \"445\", \"430\") to the listed times — any booking flow.
    """
    if not phone_number or state_manager is None or not time_slots:
        return
    try:
        dts: list[datetime] = []
        for pair in time_slots[:3]:
            if not isinstance(pair, (tuple, list)) or len(pair) < 1:
                continue
            dt0 = pair[0]
            if not hasattr(dt0, "hour"):
                continue
            dts.append(dt0)
        if not dts:
            return
        state_manager.update_fields(
            phone_number,
            {
                "offered_slot_hours": [int(dt.hour) for dt in dts],
                "offered_slot_minutes": [int(dt.minute) for dt in dts],
                "offered_slot_dates": [dt.strftime("%Y-%m-%d") for dt in dts],
                "offered_slot_date": dts[0].strftime("%Y-%m-%d"),
            },
        )
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)


def get_next_available_time_slots(
    now: datetime,
    num_slots: int = 3,
    slot_duration_minutes: int = 60,
    grace_period_minutes: int = 30,
    check_calendar: bool = True,
    start_from: datetime | None = None,
    end_by: datetime | None = None,
    booking_type: str | None = None,
    *,
    persist_slots_for_phone: str | None = None,
    persist_slots_state_manager: Any | None = None,
) -> list[tuple[datetime, str]]:
    """
    Calculate the next N available time slots with grace period.
    Prioritizes today's slots, then fills with tomorrow if needed.

    Grace period logic (when start_from is not provided):
    - Round down current time to start of 30-min block
    - Add grace_period_minutes to get first offered slot
    - Generate subsequent slots at 1-hour intervals

    Tonight window params:
    - start_from: Skip grace period and begin search from this datetime (e.g. 6pm for
      daytime "tonight" queries). Slots are generated on clean hour boundaries.
    - end_by: Stop accepting slots at or after this datetime (e.g. midnight for
      daytime "tonight" queries, or 4am for late-night "tonight" queries).
      When set, the function never jumps to the next business day.

    Args:
        now: Current datetime object
        num_slots: Number of slots to return (default 3)
        slot_duration_minutes: Duration of each slot (default 60)
        check_calendar: Whether to verify availability in Google Calendar (default True)
        grace_period_minutes: Grace period in minutes (default 30)
        start_from: Optional earliest datetime to begin offering slots (skips grace period)
        end_by: Optional hard cutoff \u2014 no slots at or after this datetime

    Returns:
        List of (datetime, formatted_string) tuples for available slots.
        Format: "Tuesday 17 March at 4:30pm"

    booking_type:
        When ``dinner_date``, only offers starts between 5pm and 9pm inclusive (last start 9pm);
        the 2h block may extend past 9pm. Calendar checks use 120 minutes.

    persist_slots_for_phone / persist_slots_state_manager:
        When both are set and the slot list is non-empty, offered_slot_* fields are written
        so COLLECTING can match shorthand time replies to the listed slots.
    """

    slots = []

    try:
        dinner_mode = (booking_type or "").strip().lower() == "dinner_date"
        if dinner_mode:
            slot_duration_minutes = DINNER_DURATION_MINUTES

        logger.info(
            f"\U0001F504 Computing available slots: now={now}, check_calendar={check_calendar}, "
            f"num_slots={num_slots}, start_from={start_from}, end_by={end_by}, dinner_mode={dinner_mode}"
        )

        if start_from is not None:
            # Tonight-window mode or explicit start: snap to next 15-min boundary
            _sf = start_from.replace(second=0, microsecond=0)
            _rem = _sf.minute % 15
            if _rem != 0:
                _sf = _sf + timedelta(minutes=15 - _rem)
            current_slot = _sf
            logger.info(f"   Using provided start_from (snapped to 15-min): {current_slot}")
        else:
            # 30-min grace period then round up to next 15-min boundary
            _earliest = now + timedelta(minutes=grace_period_minutes)
            _earliest = _earliest.replace(second=0, microsecond=0)
            _rem = _earliest.minute % 15
            if _rem != 0:
                _earliest = _earliest + timedelta(minutes=15 - _rem)
            current_slot = _earliest
            logger.info(f"   Grace {grace_period_minutes}m \u2192 snapped to 15-min boundary: {current_slot.strftime('%H:%M')}")

        if dinner_mode and not dinner_slot_fits_window(current_slot, slot_duration_minutes):
            current_slot = bump_to_next_dinner_candidate(current_slot, slot_duration_minutes)

        max_days_ahead = 7  # Don't search more than 7 days ahead
        start_date = now.date()

        from core.settings_manager import get_setting
        from handlers.booking_coll._shared import (
            _check_day_within_available_days,
            resolve_available_days_for_checks,
        )

        _ah_set = get_setting("available_hours", "") or ""
        _ad_set = get_setting("available_days", "7 days a week") or "7 days a week"
        _days_eff = resolve_available_days_for_checks(_ah_set, _ad_set)

        # Generate slots at 1-hour intervals
        while len(slots) < num_slots:
            # Enforce end_by cutoff \u2014 never cross the hard boundary
            if end_by is not None and current_slot >= end_by:
                logger.info(f"   Reached end_by boundary ({end_by}), stopping")
                break

            # Check if we've exceeded 7 days
            if (current_slot.date() - start_date).days >= max_days_ahead:
                logger.warning(f"Could not find {num_slots} available slots within {max_days_ahead} days")
                break

            if not _check_day_within_available_days(current_slot.date(), _days_eff):
                logger.info(
                    "   %s not a configured working day — skipping to next calendar day",
                    current_slot.date(),
                )
                next_cal_start = (current_slot + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                if end_by is not None and next_cal_start >= end_by:
                    logger.info(
                        "   next calendar day starts at/after end_by (%s), stopping",
                        end_by,
                    )
                    break
                current_slot = next_cal_start
                if dinner_mode and not dinner_slot_fits_window(
                    current_slot, slot_duration_minutes
                ):
                    current_slot = bump_to_next_dinner_candidate(
                        current_slot, slot_duration_minutes
                    )
                continue

            if dinner_mode:
                if not dinner_slot_fits_window(current_slot, slot_duration_minutes):
                    current_slot = bump_to_next_dinner_candidate(current_slot, slot_duration_minutes)
                    continue
                if check_calendar:
                    slot_end = current_slot + timedelta(minutes=slot_duration_minutes)
                    logger.debug(f"   [dinner] Testing slot #{len(slots)+1}: {current_slot.strftime('%H:%M')}")
                    is_avail, conflict_end = _is_calendar_available(
                        current_slot, slot_end, booking_type=booking_type
                    )
                    if is_avail:
                        formatted = _format_slot_display(current_slot)
                        slots.append((current_slot, formatted))
                        logger.info(f"   \u2705 DINNER SLOT #{len(slots)} ADDED: {formatted}")
                        next_slot = current_slot + timedelta(hours=1)
                        if not dinner_slot_fits_window(next_slot, slot_duration_minutes):
                            current_slot = bump_to_next_dinner_candidate(next_slot, slot_duration_minutes)
                        else:
                            current_slot = next_slot
                    else:
                        if conflict_end and conflict_end > current_slot:
                            _ce = conflict_end.replace(second=0, microsecond=0)
                            _rem = _ce.minute % 15
                            if _rem != 0:
                                _ce = _ce + timedelta(minutes=15 - _rem)
                            current_slot = _ce
                        else:
                            current_slot = current_slot + timedelta(minutes=15)
                else:
                    formatted = _format_slot_display(current_slot)
                    slots.append((current_slot, formatted))
                    current_slot = current_slot + timedelta(hours=1)
                continue

            # Check if slot is within valid business hours (3pm-3am)
            if _is_within_business_hours(current_slot.time()):
                # Check calendar conflict if requested
                if check_calendar:
                    slot_end = current_slot + timedelta(minutes=slot_duration_minutes)
                    logger.debug(f"   Testing slot #{len(slots)+1}: {current_slot.strftime('%H:%M')} (checking calendar...)")
                    is_avail, conflict_end = _is_calendar_available(current_slot, slot_end)
                    if is_avail:
                        formatted = _format_slot_display(current_slot)
                        slots.append((current_slot, formatted))
                        logger.info(f"   \u2705 SLOT #{len(slots)} ADDED: {formatted}")
                        # 1-hour gap between offered slots
                        current_slot = current_slot + timedelta(hours=1)
                    else:
                        logger.info(f"   \u274C SLOT REJECTED: {current_slot.strftime('%H:%M')} (calendar conflict)")
                        # Jump to end of the blocking event if known; otherwise step 15 min
                        if conflict_end and conflict_end > current_slot:
                            _ce = conflict_end.replace(second=0, microsecond=0)
                            _rem = _ce.minute % 15
                            if _rem != 0:
                                _ce = _ce + timedelta(minutes=15 - _rem)
                            current_slot = _ce
                            logger.info(f"   \u23E9 Jumping to end of conflict: {current_slot.strftime('%H:%M')}")
                        else:
                            current_slot = current_slot + timedelta(minutes=15)
                else:
                    formatted = _format_slot_display(current_slot)
                    slots.append((current_slot, formatted))
                    logger.info(f"   \u2705 SLOT #{len(slots)} ADDED: {formatted} (no calendar check)")
                    current_slot = current_slot + timedelta(hours=1)
            else:
                # Outside business hours \u2014 if end_by is set, don't jump to next day
                if end_by is not None:
                    logger.info(f"   {current_slot.strftime('%H:%M')} outside business hours with end_by set, stopping")
                    break
                # Normal mode: jump to next opening time
                _bh = normalize_business_hours_pair(get_business_hours())
                if _bh is None:
                    # 24/7 — no closing time, step forward 15 min and keep going
                    current_slot = current_slot + timedelta(minutes=15)
                    continue
                start_hour, _ = _bh
                if current_slot.hour < start_hour:
                    # Before today's opening \u2014 jump to opening time today
                    logger.debug(f"   {current_slot.strftime('%H:%M')} before opening ({start_hour}:00), jumping to opening today")
                    current_slot = current_slot.replace(hour=start_hour, minute=0, second=0, microsecond=0)
                else:
                    # Past today's closing \u2014 jump to opening time tomorrow
                    logger.debug(f"   {current_slot.strftime('%H:%M')} past closing, jumping to opening tomorrow ({start_hour}:00)")
                    current_slot = (current_slot + timedelta(days=1)).replace(hour=start_hour, minute=0, second=0, microsecond=0)

        logger.info(f"\u2705 Returning {len(slots)} available slots (requested {num_slots})")
        if persist_slots_for_phone and persist_slots_state_manager is not None and slots:
            persist_offered_slots_from_time_slot_pairs(
                persist_slots_for_phone, persist_slots_state_manager, slots
            )
        return slots

    except Exception as e:
        logger.error(f"Error calculating available slots: {e}", exc_info=True)
        return []


def _is_within_business_hours(time_obj: time) -> bool:
    """
    Check if time is within business hours as configured in the schedule page
    available_hours setting (e.g. "11am-4am, 7 days a week").

    Uses minute precision and the same end-of-shift buffer as
    ``check_within_available_hours_and_days`` (see ``OPERATING_HOURS_END_BUFFER_MINUTES``).

    Returns True if hours are not configured or set to 24/7 (always available).
    """
    from handlers.booking_coll._shared import (
        _booking_time_within_operating_minutes,
        _minutes_since_midnight,
    )

    hours = normalize_business_hours_pair(get_business_hours())
    if hours is None:
        return True  # 24/7 \u2014 no closing time, all hours valid
    start_hour, end_hour = hours

    T = _minutes_since_midnight(time_obj.hour, time_obj.minute)
    S = _minutes_since_midnight(start_hour, 0)
    E = _minutes_since_midnight(end_hour, 0)
    return _booking_time_within_operating_minutes(T, S, E)


def _is_calendar_available(
    slot_start: datetime,
    slot_end: datetime,
    booking_type: str | None = None,
):
    """
    Check if a time slot is available in Google Calendar.
    Returns (is_available, conflict_end_dt) where conflict_end_dt is the latest
    end time of all blocking events (or None if available).
    Returns (False, None) if the calendar check itself fails.
    """
    try:
        from services.calendar_service import check_conflict

        # Shift the check start by 1 minute so Google Calendar's inclusive timeMin
        # boundary doesn't return events that end exactly at the slot start
        # (e.g. a booking ending at 12:30am should NOT block a 12:30am slot offer).
        _check_start = slot_start + timedelta(minutes=1)
        # Keep the window end at slot_end: if we add 1 minute at the start but keep
        # the full duration, the window extends 1 minute past the real slot (e.g.
        # 17:00–19:00 dinner becomes 17:01–19:01) and falsely overlaps the next event
        # that starts when this slot ends (e.g. peacock at 19:00).
        _span_to_end = slot_end - _check_start
        _dur_m = max(1, int(_span_to_end.total_seconds() // 60))
        _is_dinner = (booking_type or "").strip().lower() == "dinner_date"
        details = {
            'date': _check_start.date(),
            'time': (_check_start.hour, _check_start.minute),
            'duration': _dur_m,
            # Dinner offers are outcall-style timing; duration is 120m for conflict window.
            'incall_outcall': 'outcall' if _is_dinner else 'incall',
            'booking_type': booking_type or '',
        }

        conflict_type, events = check_conflict(details)
        # Only "none" means the slot is fully available
        is_available = conflict_type == "none"

        if not is_available:
            # Find the latest end time among blocking events so the caller can jump past them
            latest_end = None
            for ev in events:
                end_str = (ev.get('end') or {}).get('dateTime')
                if end_str:
                    try:
                        import dateutil.parser as _dp
                        end_dt = _dp.parse(end_str)
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=slot_start.tzinfo)
                        elif slot_start.tzinfo:
                            end_dt = end_dt.astimezone(slot_start.tzinfo)
                        if latest_end is None or end_dt > latest_end:
                            latest_end = end_dt
                    except Exception as e:
                        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            logger.warning(f"BLOCKED: {slot_start.strftime('%H:%M')} - {conflict_type} ({len(events)} events), ends at {latest_end}")
            return False, latest_end
        else:
            logger.info(f"AVAILABLE: {slot_start.strftime('%H:%M')}")
            return True, None

    except Exception as e:
        logger.error(f"Calendar check FAILED for {slot_start}: {e}", exc_info=True)
        logger.warning("Treating slot as UNAVAILABLE (fail-closed)")
        return False, None


def _ordinal(n: int) -> str:
    """Return ordinal string for integer, e.g. 1 -> '1st', 11 -> '11th'."""
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}" + ["th", "st", "nd", "rd", "th"][min(n % 10, 4)]


_WEEKDAY_ABBREV_3 = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def weekday_abbrev_3(slot_datetime: datetime) -> str:
    """Three-letter weekday Mon–Sun (title case), locale-independent."""
    return _WEEKDAY_ABBREV_3[slot_datetime.weekday()]


def _format_slot_display(slot_datetime: datetime) -> str:
    """
    Format datetime for display.
    Expected format: "Mon 20th March 3:00pm"
    """
    try:
        day_abbrev = weekday_abbrev_3(slot_datetime)
        ordinal_day = _ordinal(slot_datetime.day)        # e.g., "20th"
        month = slot_datetime.strftime("%B")             # e.g., "March"

        time_str = slot_datetime.strftime("%I:%M%p").lower()  # e.g., "03:00pm"
        time_str = time_str.lstrip('0')
        if time_str.startswith(':'):
            time_str = '0' + time_str

        return f"{day_abbrev} {ordinal_day} {month} {time_str}"

    except Exception as e:
        logger.error(f"Error formatting slot display: {e}")
        return slot_datetime.isoformat()


def format_slot_display_short(slot_datetime: datetime) -> str:
    """
    Slot line format for SMS (e.g. dinner date when requested time is busy).
    Example: "Mon 13th April 5:00pm"
    """
    return _format_slot_display(slot_datetime)
