"""

handlers/booking_coll/_shared.py

Utility helpers, constants, and public API functions shared across the
booking_coll sub-package.  All public symbols are re-exported via __init__.py.

Doubles supply + dinner-date collection live in ``_shared_dinner_doubles`` and are
imported here for a stable ``handlers.booking_coll._shared`` namespace.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any, cast

from config import get_base_url
from templates.booking_collection_messages import (
    build_incall_duration_experience_prompt_after_time_free,
    build_outcall_address_confirmed_message,
)
from utils.dinner_date import slot_kwargs_from_booking_state
from utils.time_parser import (
    match_colonless_booking_hhmm,
    message_has_explicit_ampm,
    _nearest_ambiguous_12h_clock,
)
from utils.timezone import get_current_datetime, get_local_timezone

# Re-exported for the stable _shared namespace (consumed via __init__.py)
from handlers.booking_coll._shared_dinner_doubles import (  # noqa: F401
    _check_doubles_supply_response,
    _handle_dinner_date_fields_message,
    infer_doubles_type_hint_from_message,
)

logger = logging.getLogger("adella_chatbot.handlers.collecting")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AVAILABLE_NOW_MIN_LEAD_MINUTES = 10
AVAILABLE_NOW_OUTCALL_READY_BUFFER_MINUTES = 10

# Bookings must start at least this many minutes **before** the configured shift end
# (e.g. 4:00am end → last offered start is 3:30am).
OPERATING_HOURS_END_BUFFER_MINUTES = 30


# ---------------------------------------------------------------------------
# Private helpers — pricing
# ---------------------------------------------------------------------------

def _get_outcall_policy_amounts() -> tuple[int, int]:
    """Return surcharge/deposit values from centralized pricing defaults when needed."""
    try:
        from core.rates_from_config import get_deposit_outcall, get_surcharge
        return int(get_surcharge()), int(get_deposit_outcall())
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        try:
            from core.rates_from_config import get_default_pricing
            defaults = get_default_pricing()
            return int(defaults.get("surcharge", 100)), int(defaults.get("deposit_outcall", 100))
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            return 100, 100


def _webform_url_for_phone(phone_number: str | None) -> str:
    """Return a per-phone secure webform URL, falling back to base booking URL."""
    try:
        from core.webform_security import get_webform_url

        return get_webform_url(phone_number or "")
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return f"{get_base_url()}/booking"


# ---------------------------------------------------------------------------
# Public API — available-now datetime calculation
# ---------------------------------------------------------------------------

def calculate_available_now_booking_datetime(
    now: datetime,
    arrival_mins: int | None,
    is_outcall: bool = False,
    outcall_address: str | None = None,
) -> datetime:
    """Calculate the effective booking start datetime for available-now flows."""
    if is_outcall and outcall_address:
        from services.calendar_service import get_outcall_one_way_travel_minutes
        travel_mins = get_outcall_one_way_travel_minutes(outcall_address)
        total_lead = AVAILABLE_NOW_OUTCALL_READY_BUFFER_MINUTES + max(0, int(travel_mins or 0))
        return now + timedelta(minutes=total_lead)

    try:
        arrival = max(0, int(arrival_mins or 0))
    except (TypeError, ValueError):
        arrival = 0

    total_lead = max(arrival, AVAILABLE_NOW_MIN_LEAD_MINUTES)
    return now + timedelta(minutes=total_lead)


# ---------------------------------------------------------------------------
# Private helpers — time parsing
# ---------------------------------------------------------------------------

def _parse_time_string(time_str: str) -> int | None:
    """
    Parse an admin-config hour string like "3pm", "11am", "3:30pm" into hour (0-23).

    Scope: admin-configured operating-hour ranges (e.g. "9am-5pm"). Minutes are
    discarded — this is NOT the parser for user-supplied booking times.

    For booking-time parsing use ``booking.field_collector.FieldCollector._parse_time``,
    which returns a ``tuple[int, int]`` (hour, minute). The canonical shape for
    ``booking_fields['time']`` and ``extracted['time']`` is the (hour, minute) tuple.

    Returns None if parsing fails.
    """
    if not time_str or not isinstance(time_str, str):
        return None
    time_str = time_str.lower().strip()
    match = re.match(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)', time_str)
    if not match:
        return None

    hour = int(match.group(1)) if match and match.lastindex and match.lastindex >= 1 else None
    if hour is None or not (1 <= hour <= 12):
        return None
    period = match.group(3)

    if period == 'am':
        if hour == 12:
            hour = 0
    else:  # pm
        if hour != 12:
            hour += 12

    return hour


def _parse_24h_time(time_str: str) -> int | None:
    """Parse admin-config 24h time like '15:00' or '03:00' into hour (0-23).

    Same scope as ``_parse_time_string``: admin-configured operating hours only.
    Not used for user-supplied booking times.
    """
    if not time_str or not isinstance(time_str, str):
        return None
    time_str = time_str.strip()
    match = re.match(r'^(\d{1,2})(?::(\d{2}))?$', time_str)
    if not match:
        return None
    hour = int(match.group(1)) if match and match.lastindex and match.lastindex >= 1 else None
    if hour is not None and 0 <= hour <= 23:
        return hour
    return None


def _parse_admin_clock_fragment(raw: str) -> tuple[int, int] | None:
    """Parse admin operating-hour fragment like ``1pm``, ``4:30am``, ``15:00`` → (hour24, minute)."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*$", s)
    if m:
        hour = int(m.group(1)) if m and m.lastindex and m.lastindex >= 1 else None
        minute = int(m.group(2) or 0) if m and m.lastindex and m.lastindex >= 2 else 0
        if hour is None or not (0 <= minute <= 59) or not (1 <= hour <= 12):
            return None
        period = m.group(3) if m and m.lastindex and m.lastindex >= 3 else None
        if period == "am":
            if hour == 12:
                hour = 0
        elif period == "pm":
            if hour != 12:
                hour += 12
        return hour, minute
    m24 = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*$", s)
    if m24:
        hour = int(m24.group(1)) if m24 and m24.lastindex and m24.lastindex >= 1 else None
        minute = int(m24.group(2) or 0) if m24 and m24.lastindex and m24.lastindex >= 2 else 0
        if hour is not None and 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    return None


def _minutes_since_midnight(hour: int, minute: int) -> int:
    return max(0, min(23, hour)) * 60 + max(0, min(59, minute))


def parse_available_hours_window_hhmm(available_hours_str: str | None) -> tuple[str, str] | None:
    """
    Parse admin ``available_hours`` (same shapes as ``check_within_available_hours_and_days``)
    into ``(start_hhmm, end_hhmm)`` 24h strings for the public booking webform / booked-times API.

    Examples:
        ``11am-4am, 7 days`` → ``(\"11:00\", \"04:00\")``
        ``15:00 - 03:00`` → ``(\"15:00\", \"03:00\")``
        ``24/7, 7 days`` → ``(\"00:00\", \"23:45\")``

    Returns:
        Tuple of HH:MM strings, or ``None`` if the string is empty/whitespace-only or has no
        parseable window (caller may fall back to full-day or config default).
    """
    if not (available_hours_str or "").strip():
        return None

    raw = (available_hours_str or "").strip()
    lowered = raw.lower()

    if any(
        phrase in lowered
        for phrase in (
            "24/7",
            "24 hours",
            "always",
            "all day",
            "anytime",
            "24hrs",
            "24 7",
        )
    ):
        return ("00:00", "23:45")
    if re.search(r"\b24\s*/\s*7\b", lowered):
        return ("00:00", "23:45")

    # Same core pattern as check_within_available_hours_and_days (supports 24h fragments).
    m = re.search(
        r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*(?:-|–|to)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
        lowered,
    )
    if not m:
        return None

    start_raw = m.group(1).strip()
    end_raw = m.group(2).strip()

    sp = _parse_admin_clock_fragment(start_raw)
    if sp is None:
        sh_fallback = _parse_time_string(start_raw)
        if sh_fallback is None:
            sh_fallback = _parse_24h_time(start_raw)
        sp = (sh_fallback, 0) if sh_fallback is not None else None

    ep = _parse_admin_clock_fragment(end_raw)
    if ep is None:
        eh_fallback = _parse_time_string(end_raw)
        if eh_fallback is None:
            eh_fallback = _parse_24h_time(end_raw)
        ep = (eh_fallback, 0) if eh_fallback is not None else None

    if sp is None or ep is None:
        return None

    sh, sm = sp
    eh, em = ep
    return (f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}")


def _overnight_operating_span(start_minutes: int, end_minutes: int) -> bool:
    """True when the admin window crosses midnight ( evening → early morning )."""
    return start_minutes > end_minutes


def _morning_tail_booking(
    booking_minutes: int,
    start_minutes: int,
    end_minutes_nominal: int,
) -> bool:
    """Early-morning segment before afternoon reopen ( overnight shifts only )."""
    return booking_minutes < start_minutes and booking_minutes < end_minutes_nominal


def _booking_time_within_operating_minutes(
    booking_minutes: int,
    start_minutes: int,
    end_minutes: int,
    *,
    buffer_before_end_minutes: int = OPERATING_HOURS_END_BUFFER_MINUTES,
) -> bool:
    """
    Clock-times only (same generic calendar day as ``booking_minutes``).

    The latest acceptable **start** is ``buffer_before_end_minutes`` before the configured
    shift end ( overnight morning segment uses the nominal end, e.g. 4:00am → last start 3:30am ).
    """
    buf = max(0, int(buffer_before_end_minutes))
    end_latest = end_minutes - buf
    if not _overnight_operating_span(start_minutes, end_minutes):
        if end_latest < start_minutes:
            return False
        return start_minutes <= booking_minutes <= end_latest
    return booking_minutes >= start_minutes or booking_minutes <= end_latest


def _check_time_within_hours(booking_hour: int, start_hour: int, end_hour: int) -> bool:
    """Backward-compatible hour-only check ( minutes assumed 0; respects end buffer )."""
    bh = _minutes_since_midnight(booking_hour, 0)
    return _booking_time_within_operating_minutes(
        bh,
        _minutes_since_midnight(start_hour, 0),
        _minutes_since_midnight(end_hour, 0),
    )


# ---------------------------------------------------------------------------
# Public API — outcall address message builder
# ---------------------------------------------------------------------------

def _build_outcall_address_confirmed_msg(
    client_name: str,
    _verified_address: str,
    _distance_km: float,
    _escort_address: str,
    ask_experience: bool = True,
    acknowledge_unparsed_duration: bool = False,
    *,
    city: str = "",
    venue_name: str = "",
) -> str:
    """Build the standard message sent after confirming a client's address is within range."""
    return build_outcall_address_confirmed_message(
        client_name=client_name,
        verified_address=_verified_address,
        ask_experience=ask_experience,
        acknowledge_unparsed_duration=acknowledge_unparsed_duration,
        city=city,
        venue_name=venue_name,
    )


# ---------------------------------------------------------------------------
# Private helpers — day/date logic
# ---------------------------------------------------------------------------

def resolve_available_days_for_checks(
    available_hours_str: str | None,
    available_days_str: str | None,
) -> str:
    """
    Effective working-days string for validation.

    The admin schedule UI saves a single field like ``11am-4am, Mon, Wed``.
    The comma suffix is day availability; ``available_days`` may still say
    ``7 days a week``. Prefer the embedded suffix when present.
    """
    ah = (available_hours_str or "").strip()
    if ah and "," in ah:
        suffix = ah.split(",", 1)[1].strip()
        if suffix:
            return suffix
    ad = (available_days_str or "").strip()
    return ad if ad else "7 days a week"


def _check_day_within_available_days(booking_date: date, available_days_str: str) -> bool:
    """
    Check if a booking date falls within available days.

    Handles formats like:
    - "7 days a week" or "every day" -> always available
    - "Mon-Fri" or "Monday-Friday" -> weekdays only
    - "Mon, Wed, Fri" -> specific days
    - "Weekdays" -> Mon-Fri
    - "Weekends" -> Sat-Sun
    """
    if not available_days_str or not isinstance(available_days_str, str):
        return True
    available_days_str = available_days_str.lower().strip()
    day_of_week = booking_date.weekday()  # 0=Monday, 6=Sunday

    if any(x in available_days_str for x in ['7 days', 'every day', 'all week', 'any day', '7 days a week', 'mon-sun', 'monday-sunday']):
        return True

    if 'weekday' in available_days_str:
        return day_of_week < 5  # Mon-Fri

    if 'weekend' in available_days_str:
        return day_of_week >= 5  # Sat-Sun

    range_match = re.search(r'(mon|tue|wed|thu|fri|sat|sun)\w*\s*[-–to]+\s*(mon|tue|wed|thu|fri|sat|sun)\w*', available_days_str)
    if range_match:
        day_map = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
        start_day = day_map.get(range_match.group(1)[:3], 0)
        end_day = day_map.get(range_match.group(2)[:3], 6)

        if start_day <= end_day:
            return start_day <= day_of_week <= end_day
        else:
            return day_of_week >= start_day or day_of_week <= end_day

    day_abbrevs = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
    mentioned_days = [idx for abbrev, idx in day_abbrevs.items() if abbrev in available_days_str]
    if mentioned_days:
        return day_of_week in mentioned_days

    return True


def _normalize_booking_date(booking_date) -> date | None:
    """Convert booking_date to date. Handles date, datetime, string, or None."""
    if booking_date is None:
        return None
    if isinstance(booking_date, date) and not isinstance(booking_date, datetime):
        return booking_date
    if isinstance(booking_date, datetime):
        return booking_date.date()
    if isinstance(booking_date, str):
        try:
            return datetime.strptime(booking_date[:10], '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return None
    return None


# ---------------------------------------------------------------------------
# Public API — outside-hours message builder
# ---------------------------------------------------------------------------

def _resolved_incall_location_parts(
    city: str,
    address: str,
    venue_name: str,
) -> tuple[str, str, str]:
    """Fill missing location fragments from admin Location settings."""
    _city = (city or "").strip()
    _addr = (address or "").strip()
    _venue = (venue_name or "").strip()
    try:
        from config import get_current_incall_location

        loc = get_current_incall_location() or {}
        if not _city:
            _city = (loc.get("city") or "").strip()
        if not _venue:
            _venue = (loc.get("hotel_name") or loc.get("display_name") or "").strip()
        if not _addr:
            _addr = (loc.get("address") or "").strip()
            if not _addr:
                _addr = (loc.get("hotel_name") or "").strip()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
    return _city, _addr, _venue


def _format_outside_hours_message(
    booking_fields: dict,
    available_hours: str,
    available_days: str,
    webform_url: str = "",
    profile_url: str = "",
    city: str = "",
    address: str = "",
    venue_name: str = "",
    *,
    persist_slots_phone: str | None = None,
    persist_slots_state_manager: Any | None = None,
    suppress_time_specific_opener: bool = False,
) -> str:
    """
    Build an outside-available-hours response message.

    Gathers available slots and delegates to the canonical template so the
    message is always consistent regardless of which handler calls it.
    """
    from config import get_base_url
    from core.settings_manager import get_setting
    from templates.utility_templates import get_outside_available_hours_message

    client_name = (booking_fields or {}).get("client_name", "") or ""
    is_outcall = (booking_fields or {}).get("incall_outcall", "incall") == "outcall"

    req_bt = (booking_fields or {}).get("time")
    requested_booking_time = None
    if isinstance(req_bt, (tuple, list)) and len(req_bt) >= 2:
        try:
            requested_booking_time = (int(req_bt[0]), int(req_bt[1]))
        except (TypeError, ValueError):
            requested_booking_time = None

    time_slots = []
    try:
        from utils.availability_slots import get_next_available_time_slots
        from utils.timezone import get_current_datetime

        _now = get_current_datetime()
        time_slots = get_next_available_time_slots(
            _now,
            num_slots=3,
            check_calendar=True,
            persist_slots_for_phone=persist_slots_phone,
            persist_slots_state_manager=persist_slots_state_manager,
            **slot_kwargs_from_booking_state(booking_fields or {}),
        )
    except Exception as e:
        logger.warning("Could not compute outside-hours alternative slots: %s", e)

    if not webform_url:
        webform_url = f"{get_base_url()}/booking"

    _city, _addr, _venue = _resolved_incall_location_parts(city, address, venue_name)

    return get_outside_available_hours_message(
        city=_city,
        address=_addr,
        available_hours=available_hours or get_setting("available_hours", "11AM-4AM") or "11AM-4AM",
        available_days=available_days or get_setting("available_days", "7 days a week") or "7 days a week",
        profile_url=profile_url or "",
        webform_url=webform_url,
        client_name=client_name,
        time_slots=time_slots,
        is_outcall=is_outcall,
        requested_booking_time=requested_booking_time,
        venue_name=_venue,
        suppress_time_specific_opener=suppress_time_specific_opener,
    )


def check_and_format_outside_hours(
    booking_fields: dict,
    webform_url: str = "",
    profile_url: str = "",
    city: str = "",
    address: str = "",
    venue_name: str = "",
    *,
    phone_number: str | None = None,
    state_manager: Any | None = None,
    available_hours: str | None = None,
    available_days: str | None = None,
    hours_setting_default: str = "11AM-4AM",
    days_setting_default: str = "7 days a week",
    suppress_time_specific_opener: bool = False,
) -> tuple[bool, str, str, str]:
    """
    Load operating hours/days (unless explicitly passed), check booking_fields date/time,
    and build the outside-hours SMS when the slot is outside configured hours/days.

    Returns:
        (is_within_hours, outside_hours_message, resolved_hours, resolved_days).
        When is_within_hours is True, outside_hours_message is "".
    """
    from core.settings_manager import get_setting

    if available_hours is None:
        _ah_raw = get_setting("available_hours", hours_setting_default)
        ah = (_ah_raw if _ah_raw is not None else "") or hours_setting_default
    else:
        ah = available_hours
    if available_days is None:
        _ad_raw = get_setting("available_days", days_setting_default)
        ad = (_ad_raw if _ad_raw is not None else "") or days_setting_default
    else:
        ad = available_days

    is_within, _ = check_within_available_hours_and_days(
        cast(date, booking_fields.get("date")),
        booking_fields.get("time"),
        ah,
        ad,
    )
    if is_within:
        return True, "", ah, ad

    wf = (webform_url or "").strip()
    if not wf and phone_number:
        try:
            from core.webform_security import get_webform_url

            wf = get_webform_url(phone_number)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            wf = ""
    if not wf:
        wf = f"{get_base_url()}/booking"

    pu = (profile_url or "").strip()
    if not pu:
        try:
            from config import get_profile_url

            pu = get_profile_url() or ""
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            pu = ""

    msg = _format_outside_hours_message(
        booking_fields,
        ah,
        ad,
        webform_url=wf,
        profile_url=pu,
        city=city,
        address=address,
        venue_name=venue_name,
        persist_slots_phone=phone_number,
        persist_slots_state_manager=state_manager,
        suppress_time_specific_opener=suppress_time_specific_opener,
    )
    return False, msg, ah, ad


# ---------------------------------------------------------------------------
# Public API — availability hours/days check
# ---------------------------------------------------------------------------

def _coerce_booking_time_to_hm(booking_time: Any) -> tuple[int, int] | None:
    """
    Normalize booking time for hour/day checks.

    In-memory flows often use ``(hour, minute)`` tuples; PostgreSQL TIME columns
    and ``state_manager`` round-trip as ``datetime.time``. Treating only tuples as
    valid caused false ``time_unparseable`` / outside-hours replies for valid slots.
    """
    if booking_time is None:
        return None
    if isinstance(booking_time, datetime):
        return (booking_time.hour, booking_time.minute)
    if isinstance(booking_time, time):
        return (booking_time.hour, booking_time.minute)
    if isinstance(booking_time, (tuple, list)):
        if len(booking_time) < 1:
            return None
        try:
            h = int(booking_time[0])
            m = int(booking_time[1]) if len(booking_time) >= 2 else 0
            if not (0 <= h <= 23 and 0 <= m <= 59):
                return None
            return (h, m)
        except (TypeError, ValueError):
            return None
    if isinstance(booking_time, int):
        if 0 <= booking_time <= 23:
            return (booking_time, 0)
        return None
    return None


def check_within_available_hours_and_days(
    booking_date: date,
    booking_time: Any,
    available_hours_str: str,
    available_days_str: str
) -> tuple[bool, str]:
    """
    Check if a booking falls within available hours and days.

    Overnight shifts: early-morning times (before afternoon reopen) can count toward
    the **previous calendar day** for weekday rules (e.g. Monday 3am with Thu–Sun only
    may match Sunday night's session).

    The configured shift **end** is guarded by ``OPERATING_HOURS_END_BUFFER_MINUTES`` (30):
    the latest bookable **start** is that many minutes before the nominal end (e.g. 1pm–4am
    → starts after 3:30am are outside).

    Returns:
        Tuple of (is_available, reason_message)
    """
    from core.settings_manager import get_setting

    # H7: fail CLOSED on user-input shape errors. A malformed date/time at this
    # layer is upstream-parser drift (see M4), and letting the booking through
    # means silently scheduling outside operating hours. Return `time_unparseable`
    # so the dispatcher sends the client back to provide a clean date/time.
    _date = _normalize_booking_date(booking_date)
    if _date is None:
        logger.warning("check_within_available_hours_and_days: could not normalize booking_date %r", booking_date)
        return False, "time_unparseable"

    if not available_hours_str:
        available_hours_str = get_setting('available_hours', '') or ''
    if not available_days_str:
        available_days_str = get_setting('available_days', '7 days a week') or '7 days a week'

    days_eff = resolve_available_days_for_checks(available_hours_str, available_days_str)

    # No configured hour span → weekday gate only ( legacy behaviour ).
    if not (available_hours_str or "").strip():
        if not _check_day_within_available_days(_date, days_eff):
            return False, "outside available days"
        return True, "available"

    m = re.search(r'(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*(?:-|–|to)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)', available_hours_str.lower())
    if not m:
        logger.warning("Could not parse available_hours format: %r", available_hours_str)
        if not _check_day_within_available_days(_date, days_eff):
            return False, "outside available days"
        return True, "available"

    start_raw = m.group(1).strip()
    end_raw = m.group(2).strip()

    sp = _parse_admin_clock_fragment(start_raw)
    if sp is None:
        sh_fallback = _parse_time_string(start_raw)
        if sh_fallback is None:
            sh_fallback = _parse_24h_time(start_raw)
        sp = (sh_fallback, 0) if sh_fallback is not None else None

    ep = _parse_admin_clock_fragment(end_raw)
    if ep is None:
        eh_fallback = _parse_time_string(end_raw)
        if eh_fallback is None:
            eh_fallback = _parse_24h_time(end_raw)
        ep = (eh_fallback, 0) if eh_fallback is not None else None

    if sp is None or ep is None:
        logger.warning("Could not parse start/end hours from available_hours: %r", available_hours_str)
        if not _check_day_within_available_days(_date, days_eff):
            return False, "outside available days"
        return True, "available"

    sh, sm = sp
    eh, em = ep
    S = _minutes_since_midnight(sh, sm)
    E = _minutes_since_midnight(eh, em)

    _hm = _coerce_booking_time_to_hm(booking_time)
    if _hm is None:
        logger.warning("Invalid booking_time format: %r", booking_time)
        return False, "time_unparseable"

    booking_hour, booking_minute = _hm

    T = _minutes_since_midnight(booking_hour, booking_minute)

    day_candidates = [_date]
    if (
        _overnight_operating_span(S, E)
        and _morning_tail_booking(T, S, E)
    ):
        day_candidates.append(_date - timedelta(days=1))

    if not any(_check_day_within_available_days(d, days_eff) for d in day_candidates):
        return False, "outside available days"

    within = _booking_time_within_operating_minutes(T, S, E)
    return (within, "available" if within else "outside available hours")


# ---------------------------------------------------------------------------
# Private helpers — slot selection (doubles gate: _shared_dinner_doubles)
# ---------------------------------------------------------------------------

def _date_for_slot_index(offered_hours: list, slot_index: int, base_date_str) -> str:
    """Return the correct booking date for a slot, accounting for midnight crossings.

    When offered slots span midnight (e.g. 11pm=23, 12am=0, 1am=1), each hour
    decrease in the sequence signals a new calendar day relative to the first
    slot's date stored in offered_slot_date.
    """
    if slot_index == 0 or not offered_hours or not base_date_str:
        return base_date_str
    try:
        day_offset = 0
        prev_hour = int(offered_hours[0])
        for i in range(1, slot_index + 1):
            curr_hour = int(offered_hours[i])
            if curr_hour < prev_hour:
                day_offset += 1
            prev_hour = curr_hour
        if day_offset == 0:
            return base_date_str
        base = datetime.strptime(str(base_date_str)[:10], '%Y-%m-%d').date()
        return (base + timedelta(days=day_offset)).strftime('%Y-%m-%d')
    except Exception:
        return base_date_str


def _match_slot_selection(
    message: str | None,
    offered_hours: list,
    *,
    offered_minutes: list | None = None,
    offered_dates: list | None = None,
    offered_date: str | None = None,
    now: datetime | None = None,
) -> int | None:
    """
    Try to match a client reply like "11:50 is good", "ill take 8", "8pm", "1am"
    to one of the offered 24h slot hours (e.g. [23, 0, 1] for 11pm/midnight/1am).
    Returns the matched 24h hour, or None if no match.

    Colonless shorthand like "430" (no am/pm) uses the same nearest-future 12h rule as
    ``utils.time_parser._nearest_ambiguous_12h_clock`` and picks the offered slot closest
    to that instant when slot dates/minutes are available.
    """
    msg = (message or "").lower().strip()

    # JSON / DB may deserialize hours as strings — int membership checks must match bare "7" → 19pm logic.
    _norm_hours: list[int] = []
    for _h in offered_hours or []:
        try:
            _norm_hours.append(int(_h))
        except (TypeError, ValueError):
            continue
    offered_hours = _norm_hours

    if offered_hours:
        _first_kws = ("soonest", "earliest", "first one", "the first", "1st one", "that first", "first available")
        _second_kws = ("second one", "the second", "2nd one", "that second")
        _last_kws = ("last one", "the last", "third one", "the third", "3rd one", "that last")
        if any(kw in msg for kw in _first_kws):
            return offered_hours[0]
        if len(offered_hours) >= 2 and any(kw in msg for kw in _second_kws):
            return offered_hours[1]
        if len(offered_hours) >= 2 and any(kw in msg for kw in _last_kws):
            return offered_hours[-1]

        # Word "midnight" / 12am — Stage 6 must bind hour 0 to offered_slot_dates (crossing midnight).
        # Without this, extraction defaults date to "today" and midnight becomes wrong-calendar-day 00:00.
        if 0 in offered_hours and not re.search(r'\bnot\s+midnight\b', msg):
            _signals_midnight = (
                re.search(r'\bmidnight\b', msg)
                or re.search(r'\bmid[\s-]night\b', msg)
                or re.search(r'\b12\s*:?\s*00\s*am\b', msg)
                or re.search(r'\b12\s*am\b', msg)
                or re.search(r'\b12\s+am\b', msg)
            )
            if _signals_midnight:
                return 0

        # Colonless "430" / "945" (no am/pm): nearest-future 12h reading, then closest slot.
        cl = match_colonless_booking_hhmm(msg)
        if cl and not message_has_explicit_ampm(msg):
            tz = get_local_timezone()
            now_eff = now if now is not None else get_current_datetime()
            base_date_str = None
            if offered_dates:
                for d in offered_dates:
                    if d:
                        base_date_str = str(d)[:10]
                        break
            if not base_date_str and offered_date:
                base_date_str = str(offered_date)[:10]
            if not base_date_str:
                base_date_str = now_eff.strftime("%Y-%m-%d")

            slot_rows: list[tuple[int, datetime]] = []
            om = offered_minutes or []
            for i, h in enumerate(offered_hours):
                try:
                    hi = int(h)
                except (TypeError, ValueError):
                    continue
                try:
                    mi = int(om[i]) if i < len(om) else 0
                except (TypeError, ValueError):
                    mi = 0
                if offered_dates and i < len(offered_dates) and offered_dates[i]:
                    d_raw = str(offered_dates[i])[:10]
                else:
                    d_raw = _date_for_slot_index(offered_hours, i, base_date_str)
                try:
                    day = datetime.strptime(str(d_raw)[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
                naive = datetime.combine(day, time(hi % 24, mi))
                slot_rows.append((i, tz.localize(naive)))

            if slot_rows:
                nearest = _nearest_ambiguous_12h_clock(now_eff, cl[0], cl[1])
                best_idx, _ = min(
                    slot_rows,
                    key=lambda ix_dt: abs((ix_dt[1] - nearest).total_seconds()),
                )
                return int(offered_hours[best_idx])

    hhmm_match = re.search(r'\b(\d{1,2}):(\d{2})\s*(am|pm)?\b', msg)
    if hhmm_match:
        h = int(hhmm_match.group(1)) if hhmm_match and hhmm_match.lastindex and hhmm_match.lastindex >= 1 else None
        if h is None:
            return None
        ampm = hhmm_match.group(3) if hhmm_match and hhmm_match.lastindex and hhmm_match.lastindex >= 3 else None
        if ampm == 'pm':
            h24 = 12 if h == 12 else h + 12
        elif ampm == 'am':
            h24 = 0 if h == 12 else h
        else:
            h24_pm = 12 if h == 12 else h + 12
            if h24_pm in offered_hours:
                return h24_pm
            # "12:30" with no am/pm could be midnight (12:30am = hour 0)
            h24_am = 0 if h == 12 else h
            if h24_am in offered_hours:
                return h24_am
            h24 = h
        if h24 in offered_hours:
            return h24

    pm_match = re.search(r'\b(\d{1,2})\s*pm\b', msg)
    if pm_match:
        h = int(pm_match.group(1)) if pm_match and pm_match.lastindex and pm_match.lastindex >= 1 else None
        h24 = 12 if h == 12 else h + 12 if h is not None else None
        if h24 is not None and h24 in offered_hours:
            return h24

    am_match = re.search(r'\b(\d{1,2})\s*am\b', msg)
    if am_match:
        h = int(am_match.group(1)) if am_match and am_match.lastindex and am_match.lastindex >= 1 else None
        h24 = 0 if h == 12 else h if h is not None else None
        if h24 is not None and h24 in offered_hours:
            return h24

    bare_match = re.search(r'\b(\d{1,2})(?!\d)\b', msg)
    if bare_match:
        h = int(bare_match.group(1)) if bare_match and bare_match.lastindex and bare_match.lastindex >= 1 else None
        h24_pm = 12 if h == 12 else h + 12 if h is not None else None
        if h24_pm is not None and h24_pm in offered_hours:
            return h24_pm
        # bare "12" could be midnight (hour 0) when offered slots are after midnight
        h24_am = 0 if h == 12 else h
        if h24_am in offered_hours:
            return h24_am
        if h in offered_hours:
            return h

    return None



# ---------------------------------------------------------------------------
# Private helpers — validation error responses
# ---------------------------------------------------------------------------

def _too_far_error_response(phone_number: str, state: dict, ftv: dict, errors: list) -> dict | None:
    """Build 'outcall too far' message. Returns None if the error cannot be handled."""
    try:
        import re as _re
        from config import get_current_incall_location
        from core.webform_security import get_webform_url
        from templates.confirmations import get_outcall_unavailable_message

        location = get_current_incall_location() or {}
        city = location.get("city", "")
        hotel_name = location.get("hotel_name") or location.get("display_name", "")
        client_name = (state.get("client_name") or ftv.get("client_name") or "").strip()
        distance_km = 0.0
        for e in (errors or []):
            m = _re.search(r"(\d+\.?\d*)\s*km", e or "")
            if m:
                distance_km = float(m.group(1)) if m and m.lastindex and m.lastindex >= 1 else None
                break
        webform_url = get_webform_url(phone_number)
        outcall_msg = get_outcall_unavailable_message(
            city=city, hotel_name=hotel_name, webform_url=webform_url,
            client_name=client_name or None, distance_km=distance_km,
        )
        return {"messages": [outcall_msg], "new_state": None, "actions": []}
    except Exception as e:
        logger.warning("Failed to build outcall unavailable message: %s", e)
        return None


def _min_hour_error_response(phone_number: str, state_manager, extracted: dict, ftv: dict, current_fields: dict, greetings) -> dict | None:
    """Build 'outcall minimum 1 hour' message. Saves safe extracted fields before returning."""
    safe_updates = {}
    if extracted.get("outcall_address"):
        safe_updates["outcall_address"] = extracted["outcall_address"]
    if extracted.get("incall_outcall"):
        safe_updates["incall_outcall"] = extracted["incall_outcall"]
    if extracted.get("client_name") and greetings.is_valid_client_name(extracted["client_name"]):
        safe_updates["client_name"] = extracted["client_name"]
    if extracted.get("experience_type"):
        safe_updates["experience_type"] = extracted["experience_type"]
    if safe_updates:
        state_manager.update_fields(phone_number, safe_updates)
    try:
        from config import get_current_incall_location, get_profile_url
        from core.webform_security import get_webform_url

        location = get_current_incall_location() or {}
        city = location.get("city", "")
        webform_url = get_webform_url(phone_number)

        no_address_yet = not (ftv.get("outcall_address") or current_fields.get("outcall_address"))
        if no_address_yet:
            from core.rates_from_config import get_deposit_outcall, get_outcall_travel_surcharge_for_booking
            from templates.greetings import build_outcall_policy_message

            _bf = {**(current_fields or {}), **(ftv or {})}
            _bf.setdefault("incall_outcall", "outcall")
            policy_msg = build_outcall_policy_message(
                city=city,
                surcharge=get_outcall_travel_surcharge_for_booking(_bf),
                deposit_outcall=get_deposit_outcall(),
                webform_url=webform_url,
                has_duration=False,
            )
            return {"messages": [policy_msg], "new_state": None, "actions": []}

        from templates.utility_templates import get_outcall_min_duration_booking_message
        hotel_name = location.get("hotel_name") or location.get("address") or location.get("display_name", "")
        hotel_address = location.get("address", "")
        hotel_display = f"{hotel_name} {hotel_address}".strip() if hotel_address and hotel_address != hotel_name else hotel_name
        location_str = f"{hotel_display}, {city}".strip(", ") if hotel_display else city
        profile_url = get_profile_url() or ""
        # Format the requested time (from current state or newly extracted)
        _req_time_tuple = current_fields.get("time") or extracted.get("time")
        requested_time_str = ""
        if _req_time_tuple and isinstance(_req_time_tuple, (tuple, list)) and len(_req_time_tuple) >= 2:
            try:
                from templates.greetings import format_time_simple
                requested_time_str = format_time_simple(_req_time_tuple[0], _req_time_tuple[1])
            except Exception:
                pass
        outcall_min_msg = get_outcall_min_duration_booking_message(
            hotel_name=location_str,
            webform_url=webform_url,
            requested_time=requested_time_str,
        )
        return {"messages": [outcall_min_msg], "new_state": None, "actions": []}
    except Exception as e:
        logger.warning("Failed to build outcall min duration message: %s", e)
        return None


# ---------------------------------------------------------------------------
# Private helpers — available-now 3-slot response
# ---------------------------------------------------------------------------

def _build_three_slot_available_now_response(
    phone_number: str,
    state: dict,
    updated_fields: dict,
    *,
    is_outcall: bool,
    state_manager: Any | None = None,
) -> dict[str, Any]:
    """Shared 3-slot 'available now' template used after validation and in conflict fallbacks."""
    from config import get_available_hours, get_current_incall_location
    from core.webform_security import get_webform_url
    from templates import greetings
    from utils.availability_slots import get_next_available_time_slots
    from utils.timezone import get_current_datetime

    location = get_current_incall_location() or {}
    client_name = (state.get("client_name") or "").strip() if state else ""
    try:
        webform_url = get_webform_url(phone_number)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        webform_url = f"{get_base_url()}/booking"

    now = get_current_datetime()
    _slot_kw = slot_kwargs_from_booking_state(state)
    time_slots = get_next_available_time_slots(
        now,
        num_slots=3,
        check_calendar=True,
        persist_slots_for_phone=phone_number,
        persist_slots_state_manager=state_manager,
        **_slot_kw,
    )
    _merged = {**(state or {}), **(updated_fields or {})}
    if is_outcall:
        _merged["incall_outcall"] = "outcall"
    _booking_type = (_merged.get("booking_type") or "").strip().lower()
    _experience_type = (_merged.get("experience_type") or "").strip().lower()
    _booking_type_norm = _booking_type.replace("-", "_").replace(" ", "_")
    _experience_type_norm = _experience_type.replace("-", "_").replace(" ", "_")
    try:
        from config import get_profile_url
        _profile_url = (get_profile_url() or "").strip()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        _profile_url = ""

    try:
        from core.rates_from_config import get_deposit_mff_pair, get_outcall_travel_surcharge_for_booking, get_surcharge, is_doubles_escort_supplies_second_provider
        _special_deposit = int(get_deposit_mff_pair())
        _special_surcharge = int(get_outcall_travel_surcharge_for_booking(_merged) if is_outcall else get_surcharge())
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        _special_deposit = 200
        _special_surcharge = 100

    if _booking_type == "couples_booking" or _experience_type == "couples_mff":
        from templates.special_bookings import build_couples_available_now_message

        msg = build_couples_available_now_message(
            client_name=client_name or "",
            time_slots=time_slots,
            profile_url=_profile_url,
            webform_url=webform_url,
            city=location.get("city", ""),
            hotel_name=location.get("hotel_name", ""),
            address=location.get("address", ""),
            is_outcall=is_outcall,
            surcharge=_special_surcharge,
            deposit=_special_deposit,
        )
        return {"messages": [msg], "new_state": "COLLECTING", "actions": []}

    if _booking_type_norm in ("doubles_mff", "doubles_mmf") or _experience_type_norm in ("doubles_mff", "doubles_mmf"):
        from templates.special_bookings import build_doubles_available_now_message

        _doubles_type = (_merged.get("doubles_type") or "").strip().lower()
        if not _doubles_type:
            if _experience_type_norm == "doubles_mmf":
                _doubles_type = "mmf"
            elif _experience_type_norm == "doubles_mff":
                _doubles_type = "mff"

        msg = build_doubles_available_now_message(
            client_name=client_name or "",
            doubles_type=_doubles_type,
            time_slots=time_slots,
            profile_url=_profile_url,
            webform_url=webform_url,
            city=location.get("city", ""),
            hotel_name=location.get("hotel_name", ""),
            address=location.get("address", ""),
            is_outcall=is_outcall,
            surcharge=_special_surcharge,
            deposit=_special_deposit,
            escort_sources_second_partner=is_doubles_escort_supplies_second_provider(_merged),
        )
        return {"messages": [msg], "new_state": "COLLECTING", "actions": []}

    available_now_msg = greetings.get_available_now_message(
        city=location.get("city", ""),
        hotel_name=location.get("hotel_name", ""),
        available_hours=get_available_hours(),
        client_name=client_name or "",
        is_outcall=is_outcall,
        address=location.get("address", ""),
        has_duration=False,
        webform_url=webform_url,
        time_slots=time_slots if time_slots else None,
        booking_fields=_merged,
        phone_number=phone_number,
        state_manager=state_manager,
    )
    return {"messages": [available_now_msg], "new_state": "COLLECTING", "actions": []}


# ---------------------------------------------------------------------------
# Private helpers — outside-hours clear + incall duration probe
# ---------------------------------------------------------------------------

def _outside_hours_clear_and_respond(phone_number: str, state_manager, fields: dict, avail_hours: str, avail_days: str) -> dict:
    """Build outside-hours message, clear date+time from state, return response dict."""
    try:
        _, msg, _, _ = check_and_format_outside_hours(
            fields,
            phone_number=phone_number,
            state_manager=state_manager,
            available_hours=avail_hours,
            available_days=avail_days,
        )
        if not msg:
            msg = _format_outside_hours_message(
                fields,
                avail_hours,
                avail_days,
                persist_slots_phone=phone_number,
                persist_slots_state_manager=state_manager,
            )
    except Exception as e:
        logger.warning("Failed to build outside hours message: %s", e)
        msg = _format_outside_hours_message(
            fields,
            avail_hours,
            avail_days,
            persist_slots_phone=phone_number,
            persist_slots_state_manager=state_manager,
        )
    state_manager.update_fields(phone_number, {"date": None, "time": None})
    return {"messages": [msg], "new_state": None, "actions": []}


def _incall_duration_prompt_with_calendar_probe(updated_fields: dict, _state: dict, exp_already_set: bool) -> str:
    """For incall bookings: probe calendar and return the best duration-ask prompt.

    Returns a confirmed-free prompt if the slot is open, otherwise the plain duration prompt.
    """
    from templates import field_prompts as _fp
    base_prompt = _fp.get_duration_only_prompt(experience_already_set=exp_already_set)
    try:
        from templates.field_prompts import _get_experience_url
        _exp_url = _get_experience_url()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        _exp_url = "https://www.adella-allure.com.au/experience"
    try:
        from services.calendar_service import check_conflict

        d_raw = updated_fields.get("date")
        if isinstance(d_raw, datetime):
            d_norm = d_raw.date()
        elif isinstance(d_raw, date):
            d_norm = d_raw
        elif isinstance(d_raw, str):
            d_norm = datetime.strptime(d_raw[:10], "%Y-%m-%d").date()
        else:
            d_norm = d_raw

        t_raw = updated_fields.get("time")
        if isinstance(t_raw, time):
            t_norm = (t_raw.hour, t_raw.minute)
        elif isinstance(t_raw, (tuple, list)) and len(t_raw) >= 2:
            t_norm = (int(t_raw[0]), int(t_raw[1]))
        elif isinstance(t_raw, int):
            t_norm = (int(t_raw), 0)
        else:
            t_norm = (12, 0)

        conflict_type, _ = check_conflict({"date": d_norm, "time": t_norm, "duration": 60, "incall_outcall": "incall", "outcall_address": None})
        if conflict_type == "none":
            return build_incall_duration_experience_prompt_after_time_free(
                _exp_url, d_norm, t_norm, experience_already_set=exp_already_set
            )
    except Exception as e:
        logger.warning("Incall duration prompt: could not add time-free confirmation: %s", e)
    return base_prompt


# ---------------------------------------------------------------------------
# Public API — pre-calendar booking summary (legacy name: "perfect timing line")
# ---------------------------------------------------------------------------

def _format_perfect_timing_line(
    fields: dict[str, Any],
    client_name: str = "",
    profile_url: str = "",
    phone_number: str = "",
    webform_url: str = "",
) -> str:
    """Full booking reconfirmation (date/time/duration/experience/location/total) before calendar check.

    Used when the client already supplied mandatory details (Scenario B, CHECKING_AVAILABILITY).
    ``profile_url`` is kept for call-site compatibility; the listing link is not appended here.
    """
    from templates.booking_reconfirmation import build_booking_reconfirmation

    booking_fields = dict(fields) if fields else {}
    if client_name and str(client_name).strip():
        booking_fields["client_name"] = str(client_name).strip()
    if phone_number:
        booking_fields["phone_number"] = phone_number
    try:
        msg = build_booking_reconfirmation(
            booking_fields,
            include_yes_prompt=True,
            skip_optional_deposit=False,
        )
    except Exception as e:
        logger.warning("pre-calendar booking summary failed: %s", e, exc_info=False)
        cn = (client_name or "").strip()
        if cn:
            return (
                f"Thanks {cn}, just to confirm your booking. "
                "Reply with your first name and YES to lock in (e.g. John YES)."
            )
        return (
            "Thanks! Just to confirm your booking. "
            "Reply with your first name and YES (e.g. John YES)."
        )
    wf = (webform_url or "").strip()
    if wf:
        msg = f"{msg.rstrip()}\n\nTo book a different time fill in my booking webform:\n{wf}"
    return msg


# ---------------------------------------------------------------------------
# Private helpers — field extraction utility
# ---------------------------------------------------------------------------

def _extract_and_merge_booking_fields(context: dict[str, Any]) -> tuple:
    """Extract booking fields from current message and merge into state. Returns (current_fields, missing_fields, field_collector)."""
    import config as cfg
    from booking.field_collector import FieldCollector

    phone_number = context['phone_number']
    state_manager = context['state_manager']
    msg = context.get('message', '')
    ai_service = context.get('ai_service')

    field_collector = FieldCollector(cfg, ai_service=ai_service)
    current_fields = state_manager.get_booking_fields(phone_number)
    extracted = field_collector.extract_fields(msg, current_fields)

    if extracted:
        updates = {k: v for k, v in extracted.items() if v is not None and (v != '' or k not in ('outcall_address',))}
        if updates:
            state_manager.update_fields(phone_number, updates)
            current_fields = state_manager.get_booking_fields(phone_number)

    missing = field_collector.get_missing_fields(current_fields)
    return current_fields, missing, field_collector
