"""Shared time/date formatting utilities — single source of truth.

Consolidates duplicate ordinal-suffix and 12-hour formatting scattered
across templates/, services/, and utils/.
"""

import datetime as _dt


def get_day_ordinal_suffix(day: int) -> str:
    """Return ordinal suffix for a calendar day (1-31).

    >>> get_day_ordinal_suffix(1)
    'st'
    >>> get_day_ordinal_suffix(11)
    'th'
    >>> get_day_ordinal_suffix(22)
    'nd'
    """
    if 11 <= (day % 100) <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def format_time_12h(hour: int, minute: int = 0) -> str:
    """Format 24-hour time as 12-hour string with am/pm.

    Correctly handles midnight (0 → 12am) and noon (12 → 12pm).

    >>> format_time_12h(0, 0)
    '12am'
    >>> format_time_12h(12, 30)
    '12:30pm'
    >>> format_time_12h(15, 0)
    '3pm'
    """
    period = "am" if hour < 12 else "pm"
    h12 = hour % 12 or 12
    if minute:
        return f"{h12}:{minute:02d}{period}"
    return f"{h12}{period}"


def parse_booking_hour_minute(
    bk_time,
) -> "tuple[int, int] | tuple[None, None]":
    """Parse a booking time value into a ``(hour, minute)`` tuple.

    Accepts the four formats used throughout the codebase:
    - ``datetime.time`` object
    - ``(hour, minute)`` tuple or list
    - ``int`` (hour only; minute defaults to 0)
    - anything else → ``(None, None)``

    Examples::

        >>> parse_booking_hour_minute((14, 30))
        (14, 30)
        >>> parse_booking_hour_minute(9)
        (9, 0)
        >>> parse_booking_hour_minute(None)
        (None, None)
    """
    if isinstance(bk_time, _dt.time):
        return bk_time.hour, bk_time.minute
    if isinstance(bk_time, (tuple, list)) and len(bk_time) >= 2:
        return int(bk_time[0]), int(bk_time[1])
    if isinstance(bk_time, int):
        return bk_time, 0
    return None, None
