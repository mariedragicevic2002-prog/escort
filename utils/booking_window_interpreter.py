"""
Mandatory booking window interpretation rules.

Service-night definition: 21:00 to 03:45.

For explicit-day wording ("tomorrow", "tonight") used in **slot listing** and
availability UX, see ``utils.time_parser.GOLDEN_TIME_RULES`` and
``get_tonight_slot_window`` / ``get_requested_day_start`` (single source of truth).

This module still defines the broader mandatory-window search span for
``get_mandatory_booking_window`` (see below).
"""

from datetime import datetime, timedelta

# Service-night boundaries
_SN_START_HOUR = 21            # 9 pm
_SN_END_HOUR   = 3             # 3 am
_SN_END_MINUTE = 45            # 3:45 am


def is_in_service_night(dt: datetime) -> bool:
    """Return True if *dt* falls within a service-night (21:00 – 03:45)."""
    h, m = dt.hour, dt.minute
    if h >= _SN_START_HOUR:
        return True
    if h < _SN_END_HOUR:
        return True
    if h == _SN_END_HOUR and m <= _SN_END_MINUTE:
        return True
    return False


def is_in_late_night_window(dt: datetime) -> bool:
    """Return True if *dt* is in the late-night portion 00:00 – 03:45."""
    h, m = dt.hour, dt.minute
    if h == 0:
        return True
    if h < _SN_END_HOUR:
        return True
    if h == _SN_END_HOUR and m <= _SN_END_MINUTE:
        return True
    return False


def get_mandatory_booking_window(current_dt: datetime, _client_request: str = "") -> tuple[datetime, datetime]:
    """
    Return (window_start, window_end) for availability search based on the current time.

    Rules:
    - 21:00–23:59  (early service-night): until 03:45 next calendar day
    - 00:00–03:45  (late-night window):   until 03:45 same calendar day
    - 03:46–08:59  (post-night / morning): treat as today until 5 pm
    - 09:00–20:59  (daytime):             until midnight same day
    """
    h, m = current_dt.hour, current_dt.minute

    if h >= _SN_START_HOUR:
        # 21:00-23:59 \u2014 early service-night; window extends to 03:45 next calendar day
        next_day = current_dt + timedelta(days=1)
        window_end = next_day.replace(hour=_SN_END_HOUR, minute=_SN_END_MINUTE, second=0, microsecond=0)
        return current_dt, window_end

    if h < _SN_END_HOUR or (h == _SN_END_HOUR and m <= _SN_END_MINUTE):
        # 00:00-03:45 \u2014 late-night window; window ends at 03:45 same calendar day
        window_end = current_dt.replace(hour=_SN_END_HOUR, minute=_SN_END_MINUTE, second=0, microsecond=0)
        return current_dt, window_end

    if h < 9:
        # 03:46-08:59 \u2014 post-night morning; treat remainder as "today" until 5 pm
        window_end = current_dt.replace(hour=17, minute=0, second=0, microsecond=0)
        return current_dt, window_end

    # 09:00-20:59 \u2014 daytime; search until midnight same day
    window_end = current_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return current_dt, window_end


def should_extend_search_to_next_day(current_dt: datetime) -> bool:
    """
    Return True when the availability search should cross midnight into the next calendar day.
    This is the case during the early service-night (21:00-23:59) only.
    """
    return current_dt.hour >= _SN_START_HOUR


def interpret_tonight_vs_tomorrow(current_dt: datetime, _client_request: str = "") -> str:
    """
    Decide whether a vague request maps to "tonight", "today", or "tomorrow".

    - 09:00-20:59  \u2192 "today"
    - 21:00-03:45  \u2192 "tonight"  (service-night is active)
    - 03:46-08:59  \u2192 if client said "tomorrow": "today" (late-night redirection)
                     otherwise: "today"
    """
    h = current_dt.hour
    in_service_night = is_in_service_night(current_dt)

    if in_service_night:
        return "tonight"

    # Post-night morning (03:46-08:59)
    if h < 9:
        return "today"

    # Daytime
    return "today"


def get_window_description(current_dt: datetime) -> str:
    """Human-readable description of the active booking window (for logging)."""
    h, m = current_dt.hour, current_dt.minute
    if h >= _SN_START_HOUR:
        return "21:00-23:59 early service-night: window extends to 03:45 next day"
    if h < _SN_END_HOUR or (h == _SN_END_HOUR and m <= _SN_END_MINUTE):
        return "00:00-03:45 late-night window: window ends at 03:45 same day"
    if h < 9:
        return "03:46-08:59 post-night morning: treated as today until 5 pm"
    return "09:00-20:59 daytime: window ends at midnight"
