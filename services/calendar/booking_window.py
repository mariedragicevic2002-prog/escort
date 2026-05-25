"""Parse booking details into datetimes and related helpers."""

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _format_duration_label(duration) -> str:
    """Format a duration value as a readable string without duplicating 'minutes'/'hours'.
    Accepts int minutes (e.g. 30 → '30 minutes'), or a string that may already contain units
    (e.g. '30 minutes' → '30 minutes', '1 hour' → '1 hour').
    """
    if duration is None or duration == "N/A":
        return "N/A"
    s = str(duration).strip().lower()
    if any(u in s for u in ("hour", "min", "hr")):
        return str(duration).strip()
    try:
        mins = int(float(s))
        if mins % 60 == 0:
            h = mins // 60
            return f"{h} hour" + ("s" if h != 1 else "")
        return f"{mins} minutes"
    except (ValueError, TypeError):
        return str(duration).strip()


def parse_booking_time_hour_minute(time_value):
    """
    Extract (hour, minute) from booking ``time`` payloads (matches ``_parse_booking_window``).

    Webforms and APIs often send ``\"HH:MM\"`` strings; deposit SMS formatting must not assume
    ``datetime.time`` or tuple form only.
    """
    from datetime import datetime
    from datetime import time as time_type

    if time_value is None:
        return None
    if isinstance(time_value, time_type):
        return time_value.hour, time_value.minute
    if isinstance(time_value, datetime):
        return time_value.hour, time_value.minute
    if isinstance(time_value, int):
        try:
            return int(time_value), 0
        except (TypeError, ValueError):
            return None
    if isinstance(time_value, (list, tuple)) and len(time_value) >= 2:
        try:
            return int(time_value[0]), int(time_value[1])
        except (TypeError, ValueError):
            return None
    s = str(time_value).strip()
    if not s:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(s, fmt).time()
            return t.hour, t.minute
        except ValueError:
            continue
    return None


def _parse_booking_window(details):
    """Parse booking details into start and end datetime objects."""
    from datetime import date as date_type
    from datetime import time as time_type

    from utils.timezone import get_local_timezone

    date_value = details.get("date")
    time_value = details.get("time")
    raw_duration = details.get("duration", 60)
    try:
        duration = int(raw_duration) if raw_duration is not None else 60
    except (TypeError, ValueError):
        # raw_duration is a string like "30 minutes", "1 hour", "1.5 hours"
        import re as _re

        s = str(raw_duration).lower()
        hours_match = _re.search(r"(\d+(?:\.\d+)?)\s*(?:hour|hr)", s)
        mins_match = _re.search(r"(\d+)\s*min", s)
        duration = 0
        if hours_match:
            if hours_match and hours_match.lastindex and hours_match.lastindex >= 1:
                duration += int(float(hours_match.group(1)) * 60)
        if mins_match and mins_match.lastindex and mins_match.lastindex >= 1:
            duration += int(mins_match.group(1))
        if duration == 0:
            duration = 60

    if not date_value or not time_value:
        return None, None

    try:
        # Handle both datetime objects and strings
        if isinstance(date_value, date_type):
            date_obj = date_value
        elif isinstance(date_value, datetime):
            date_obj = date_value.date()
        else:
            # String - parse it
            date_obj = datetime.strptime(str(date_value), "%Y-%m-%d").date()

        hm = parse_booking_time_hour_minute(time_value)
        if hm is None:
            logger.error(f"Could not parse booking time value {time_value!r}")
            return None, None
        try:
            time_obj = time_type(hour=hm[0], minute=hm[1])
        except ValueError as e:
            logger.error(f"Could not build time from hour/minute {hm}: {e}")
            return None, None

        # Combine into datetime
        tz = get_local_timezone()
        start_dt = tz.localize(datetime.combine(date_obj, time_obj))

        # Calculate duration (convert minutes to hours)
        hours = duration / 60.0
        end_dt = start_dt + timedelta(hours=hours)

        return start_dt, end_dt

    except Exception as e:
        logger.error(f"Parse booking window error: {e}")
        return None, None


def is_datetime_in_past(details):
    """Check if booking datetime is in the past."""
    from utils.timezone import get_current_datetime

    start_dt, _ = _parse_booking_window(details)
    if not start_dt:
        return False

    now = get_current_datetime()

    return start_dt < now
