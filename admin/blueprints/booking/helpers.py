"""Shared formatting and validation helpers for booking routes."""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import re
from datetime import date, datetime, timedelta
from datetime import time as dt_time

import config

from .log import logger
from .overnight_calendar_date import calendar_date_for_overnight_slot


def webform_dinner_start_time_ok(time_str: str) -> bool:
    """Dinner date webform: start time must be 17:00–21:00 inclusive (no 30m end buffer)."""
    if not time_str or not str(time_str).strip():
        return False
    parts = str(time_str).strip().split(":")
    if len(parts) < 2:
        return False
    try:
        h, m = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return False
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return False
    mins = h * 60 + m
    return 17 * 60 <= mins <= 21 * 60


def _parse_duration_minutes(duration_str: str) -> int:
    """Convert '1 hour 30 minutes', '2 hours', '30 minutes' etc. to total minutes."""
    if not duration_str:
        return 60
    total = 0
    h = re.search(r"(\d+)\s*hour", duration_str)
    if h:
        if h and h.lastindex and h.lastindex >= 1:
            total += int(h.group(1)) * 60
    m = re.search(r"(\d+)\s*min", duration_str)
    if m and m.lastindex and m.lastindex >= 1:
        total += int(m.group(1))
    return total or 60


def _parse_available_hours(hours_str: str):
    """Parse available hours setting into (start_hhmm, end_hhmm) for the booking webform.

    Uses the same window rules as the SMS bot (``check_within_available_hours_and_days``):
    supports ``11am-4am``, ``15:00-03:00``, commas/day suffixes, etc.

    Returns:
        (start_hhmm, end_hhmm). On empty input or unparseable text, uses full-day slots and logs
        (never silently falls back to 3pm–3am).
    """
    from handlers.booking_coll._shared import parse_available_hours_window_hhmm

    parsed = parse_available_hours_window_hhmm(hours_str or "")
    if parsed:
        start, end = parsed
        if start == "00:00" and end == "00:00":
            return ("00:00", "23:45")
        return (start, end)

    if not (hours_str or "").strip():
        logger.warning("available_hours empty — webform using full-day slot window")
        return ("00:00", "23:45")

    logger.warning(
        "Could not parse available_hours for webform %r — using full-day slot window",
        hours_str,
    )
    return ("00:00", "23:45")


def adjust_webform_date_str_for_overnight_time(
    date_str: str | None,
    time_str: str | None,
    hours_str: str | None,
    *,
    experience: str | None = None,
) -> str | None:
    """Normalize YYYY-MM-DD for calendar/DB when the slot is after midnight on an overnight schedule."""
    if experience and experience.strip() == "Dinner Date":
        return date_str
    if not date_str or not time_str:
        return date_str
    try:
        d0 = datetime.strptime(str(date_str).strip(), "%Y-%m-%d").date()
    except ValueError:
        return date_str
    parts = str(time_str).strip().split(":")
    if len(parts) < 2:
        return date_str
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return date_str
    avail_start, avail_end = _parse_available_hours(hours_str or "")
    eff = calendar_date_for_overnight_slot(d0, h, m, avail_start, avail_end)
    return eff.isoformat()


def _safe_int_money(val, default=100):
    """Coerce deposit/price to int. dict.get('x', default) still returns None if key exists with null — avoid int(None)."""
    if val is None or val == "":
        return default
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


# Group / doubles experiences that can require arranging another escort (webform checkboxes).
WEBFORM_GROUP_EXPERIENCES = frozenset(
    {"Doubles MFF", "Doubles MMF", "Couples MFF"}
)


def _validate_group_escort_notice(
    date_str: str,
    time_str: str,
    experience: str,
    needs_provider_female: bool,
    needs_provider_male: bool,
    city: str,
) -> tuple[bool, str | None]:
    """
    If client needs the provider to arrange another escort, same-day bookings must start
    at least 4 hours from now (escort local timezone). Times use the same 15-minute grid as the form.
    """
    if experience not in WEBFORM_GROUP_EXPERIENCES or not (needs_provider_female or needs_provider_male):
        return True, None
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(config.get_timezone_for_city(city or ""))
        now = datetime.now(tz)
        booking_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        if booking_date != now.date():
            return True, None
        parts = (time_str or "").strip().split(":")
        h = int(parts[0])
        mi = int(parts[1]) if len(parts) > 1 else 0
        start_dt = datetime.combine(booking_date, dt_time(h, mi), tzinfo=tz)
        if start_dt < now + timedelta(hours=4):
            return (
                False,
                "When you need the provider to arrange another escort, same-day bookings must be at least "
                "4 hours from now. Please pick a later time or another date.",
            )
    except Exception as e:
        logger.warning("group escort notice validation failed: %s", e)
        return False, "Could not validate booking time. Please check date and time and try again."
    return True, None


def _append_group_escort_notes(
    special_requests: str,
    experience: str,
    needs_provider_female: bool,
    needs_provider_male: bool,
) -> str:
    if experience not in WEBFORM_GROUP_EXPERIENCES:
        return special_requests
    lines = []
    if needs_provider_female:
        lines.append("[Booking] Client asked provider to arrange the other female escort (e.g. MFF / Couples MFF).")
    if needs_provider_male:
        lines.append("[Booking] Client asked provider to arrange the other male escort (e.g. MMF).")
    if not lines:
        return special_requests
    extra = "\n".join(lines)
    if special_requests:
        return (special_requests + "\n\n" + extra).strip()
    return extra


def _append_mmf_exploration_special_requests_line(special_requests: str, tags_slugs: list[str]) -> str:
    """Append readable MMF exploration line when escort arranges male (webform/SMS sourced tags)."""
    if not tags_slugs:
        return special_requests
    try:
        from booking.mmf_exploration import format_mmf_exploration_calendar_line

        line = format_mmf_exploration_calendar_line(tags_slugs)
    except Exception:
        return special_requests
    if not line:
        return special_requests
    if special_requests:
        return (special_requests.strip() + "\n\n" + line).strip()
    return line


def _fmt_sms_date(date_val) -> str:
    """Format a date value as 'Mon 6th April' for SMS messages."""
    try:
        if isinstance(date_val, str):
            d = datetime.strptime(date_val[:10], "%Y-%m-%d").date()
        elif isinstance(date_val, datetime):
            d = date_val.date()
        elif isinstance(date_val, date):
            d = date_val
        else:
            return str(date_val)
        day = d.day
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day if day < 20 else day % 10, "th")
        return d.strftime(f"%a {day}{suffix} %B")
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        return str(date_val)


def get_booking_place_autocomplete_center(location: dict | None) -> dict[str, float] | None:
    """
    Lat/lng for Google Places bias on the booking webform: escort's reference point (admin Location).

    Prefer :func:`booking.outcall_verification.get_escort_reference_coords_for_ui` (same geocoded
    address/hotel + CBD fallback as 15 km verification). If that fails, fall back to city-only
    lookup in :data:`booking.outcall_verification.CBD_COORDINATES`.
    """
    try:
        from booking.outcall_verification import get_escort_reference_coords_for_ui

        ui = get_escort_reference_coords_for_ui()
        if ui:
            return ui
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
    try:
        from booking.outcall_verification import CBD_COORDINATES
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return None
    city_raw = (location or {}).get("city") if isinstance(location, dict) else ""
    c = (city_raw or "").strip().lower()
    if not c:
        return None
    for sep in (",", "/", "|"):
        if sep in c:
            c = c.split(sep)[0].strip()
    c = re.sub(r"\s+(wa|nsw|vic|qld|sa|tas|nt|act)\s*$", "", c, flags=re.I).strip()
    c = re.sub(r"\s+cbd\s*$", "", c, flags=re.I).strip()
    c = re.sub(r"\s+australia\s*$", "", c, flags=re.I).strip()
    candidates: list[str] = []
    if c:
        candidates.append(c)
        if "gold coast" in c:
            candidates.append("gold coast")
        parts = c.split()
        if len(parts) >= 2 and f"{parts[0]} {parts[1]}" == "gold coast":
            candidates.append("gold coast")
        elif parts:
            candidates.append(parts[0])
    for cand in candidates:
        if cand in CBD_COORDINATES:
            v = CBD_COORDINATES[cand]
            return {"lat": float(v["lat"]), "lng": float(v["lng"])}
    for cand in candidates:
        for k, v in CBD_COORDINATES.items():
            if k in cand or cand in k:
                return {"lat": float(v["lat"]), "lng": float(v["lng"])}
    return None


# Get browser Google Maps API key for client-side autocomplete
# Prefer DB setting (set via admin config page); fall back to env var
def _get_google_maps_browser_key() -> str:
    try:
        from core.settings_manager import get_setting

        db_key = (get_setting("google_maps_browser_api_key") or "").strip()
        if db_key:
            return db_key
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
    return config.get_google_maps_browser_api_key() or ""


def _duration_to_minutes(duration_str):
    """Convert webform duration string to integer minutes for DB. Returns None if unparseable."""
    if not duration_str:
        return None
    try:
        return _parse_duration_minutes(duration_str)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        s = (duration_str or "").strip().lower()
        if "48" in s or "weekend" in s:
            return 48 * 60
        if "12" in s or "overnight" in s:
            return 12 * 60
        return None


def _minutes_to_duration_label(minutes):
    """Convert integer minutes back to a friendly duration string for display."""
    if not minutes:
        return "As requested"
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return "As requested"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour" + ("" if hours == 1 else "s")
    if minutes < 60:
        return f"{minutes} mins"
    # e.g. 90 mins → "1 hour 30 mins"
    hours = minutes // 60
    remainder = minutes % 60
    return f"{hours} hour{'s' if hours > 1 else ''} {remainder} mins"


def _format_booking_date(date_str):
    """Convert YYYY-MM-DD date string to friendly format like 'Friday 10 January 2025'."""
    if not date_str:
        return "Confirmed"
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%A %-d %B %Y")
    except (ValueError, AttributeError):
        try:
            # Windows strftime doesn't support %-d, use manual formatting
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B %Y')}"
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
            return date_str


def _format_booking_time(time_str):
    """Convert HH:MM time string to friendly format like '7:00 PM'."""
    if not time_str:
        return "As requested"
    try:
        # Handle both HH:MM and HH:MM:SS
        parts = time_str.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        ampm = "AM" if hour < 12 else "PM"
        display_hour = hour % 12
        if display_hour == 0:
            display_hour = 12
        return f"{display_hour}:{minute:02d} {ampm}"
    except (ValueError, IndexError):
        return time_str


def _format_time_value_for_confirmation(time_value):
    """
    Format DB time for confirmation pages as compact 12h, e.g. 2:15am.
    Accepts (hour, minute), datetime.time, datetime.datetime, or 'HH:MM' / 'HH:MM:SS' strings.
    """
    if time_value is None:
        return "TBA"
    hour, minute = None, None
    try:
        if isinstance(time_value, tuple) and len(time_value) >= 2:
            h_raw, m_raw = time_value[0], time_value[1]
            if h_raw is None or m_raw is None:
                return str(time_value)
            hour, minute = int(h_raw), int(m_raw)
        elif isinstance(time_value, dt_time):
            hour, minute = int(time_value.hour), int(time_value.minute)
        elif isinstance(time_value, datetime):
            hour, minute = int(time_value.hour), int(time_value.minute)
        elif isinstance(time_value, str):
            m = re.match(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?", time_value.strip())
            if m:
                if m and m.lastindex and m.lastindex >= 2:
                    hour, minute = int(m.group(1)), int(m.group(2))
                else:
                    hour = minute = None
    except (TypeError, ValueError):
        return str(time_value)
    if hour is None or not (0 <= hour <= 23 and 0 <= minute <= 59):
        return str(time_value)
    period = "am" if hour < 12 else "pm"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{minute:02d}{period}"
