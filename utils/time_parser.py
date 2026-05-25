"""

Time parsing and inference utilities for booking requests.

Service-night definition: 21:00 – 03:45.

Key rules for bare-hour inference:
- During service-night, if a bare 1-12 hour has ONLY ONE sensible future occurrence
  within the current service-night window, use that automatically.
- Service-night hours (24h): 21, 22, 23, 0, 1, 2, 3 (up to 03:45).
  - Hours 9-11 map uniquely to PM (21-23) \u2192 9 pm, 10 pm, 11 pm.
  - Hours 1-3 map uniquely to AM (01-03) \u2192 1 am, 2 am, 3 am.
  - Hours 4-8 and 12 don't fall in the service-night window; use closest future occurrence.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import re
from datetime import date as date_type
from datetime import datetime, timedelta
from datetime import time as time_type

# Service-night boundaries

import logging
logger = logging.getLogger("adella_chatbot.time_parser")

_SN_START = 21   # 9 pm (inclusive)
_SN_END_H = 3    # 3 am
_SN_END_M = 45   # 3:45 am

# --- Golden time rules (slot windows + explicit-day requests) ---
# Canonical reference for "tonight" slot clamping and "tomorrow" day windows used by
# get_tonight_slot_window / get_requested_day_start. See also booking_window_interpreter.
GOLDEN_TIME_RULES = """
Golden time rules (availability UX):

Colonless 12h clock + minutes with no am/pm (e.g. SMS "430", "945"):
- Interpret as the closest *future* wall-clock instant among the valid AM/PM readings
  (same rule as _nearest_ambiguous_12h_clock). Example: at 11:25 local, "430" → 4:30 pm,
  not 4:30 am.
- When matching against offered slots, bind to the slot datetime nearest that interpretation.

"Tonight" slot list (get_tonight_slot_window) — local hour of *now*:
- 0 <= hour < 4:  ASAP until 04:00 same calendar day (wee-hours / late-night cap).
- 4 <= hour < 18: start_from = 18:00 same day, end_by = 00:00 next calendar day.
- 18 <= hour < 21: end_by = 00:00 next calendar day (midnight cap — general rule).
- 21 <= hour <= 23: end_by = 04:00 next calendar day
                    (9pm–midnight: tonight up to midnight PLUS tomorrow 00:00–04:00).

"Tomorrow" explicit-day requests (get_requested_day_start when label is "tomorrow"):
- If hour >= 4: target_date = next calendar day; window = 11:00–21:00 on target_date.
- If hour < 4 (midnight–3:59am): target_date = same calendar day for *vague* tomorrow
  (listing window 18:00–00:00 that date — "tomorrow at 1am" colloquially tied to that evening).
  Exception: when the client names an explicit wee-hours clock (midnight–~4am including am/pm
  or compact "330am"), target_date = next calendar day ("tomorrow at 3am" at 03:27 Wed → Thu 03:00).
"""

TOMORROW_WINDOW_START_HOUR = 11
TOMORROW_WINDOW_END_EXCLUSIVE_HOUR = 21
# Used only for midnight-3am "tomorrow" requests
TOMORROW_LATE_NIGHT_START_HOUR = 18

TONIGHT_WEE_HOURS_END = 4
TONIGHT_EVENING_START_HOUR = 18

# Words/phrases that trigger "available now" pathway
IMMEDIATE_KEYWORDS = {
    'now', 'soon', 'asap', 'right now', 'right this second',
    'immediately', 'straight away', 'straight up', 'urgent',
    'tonight if possible', 'this evening if possible', 'as soon as',
    'can you do now', 'can you do soon'
}

# Words/phrases that trigger the 8-hour availability window (from the moment of enquiry).
# Covers both "later" intent and immediate-availability intent ("soon", "asap", "shortly").
LATER_KEYWORDS = {
    'later', 'a bit later', 'sometime later', 'later on',
    'later today', 'later tonight', 'later this evening',
    'a little later', 'bit later',
    # Immediate-window synonyms \u2014 same 8-hour window, start = now
    'soon', 'asap', 'shortly',
}

# Words/phrases indicating "tonight" \u2014 used for slot window clamping
TONIGHT_KEYWORDS = {
    'tonight', 'this evening', 'free tonight', 'available tonight',
    'later tonight', 'later this evening', 'tonight if possible',
    'this evening if possible',
}

# 24-hour values that fall within the service-night (21:00-03:45)
_SN_HOURS_24 = {21, 22, 23, 0, 1, 2, 3}

# "at 9 Cantle St" — unit/street number must not become 9pm when no real clock time is present.
_AT_HOUR_STREET_TAIL_RE = re.compile(
    r"(?:[a-z0-9]+\s+){0,4}"
    r"(?:st|street|road|rd|avenue|ave|drive|dr|terrace|tce|crescent|cres|close|cl|place|pl|"
    r"court|ct|way|parade|pde|boulevard|blvd|lane|ln)\b",
    re.IGNORECASE,
)

# Compact clock glued to am/pm: "330am", "930pm" (no space before am/pm).
_COMPACT_HHMMAP_RE = re.compile(
    r"\b(1[0-2]|0?[1-9])([0-5]\d)(am|pm)\b",
    re.IGNORECASE,
)
_DURATION_UNIT_TAIL_RE = re.compile(
    r"\s*(?:h(?:ours?)?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)


def _bare_at_hour_followed_by_street(msg: str, match: re.Match) -> bool:
    tail = msg[match.end() :].strip()
    if not tail:
        return False
    return bool(_AT_HOUR_STREET_TAIL_RE.match(tail))


def _bare_digit_is_street_unit_before_street_type(msg: str, match: re.Match) -> bool:
    """
    True if this bare \\d{1,2} match is a unit/street number (e.g. '9' in '9 Cantle St'),
    not a clock hour. Uses the same tail heuristic as 'at 9 Cantle St'.
    """
    tail = msg[match.end() :].strip()
    if not tail:
        return False
    return bool(_AT_HOUR_STREET_TAIL_RE.match(tail))


def _numeric_token_looks_like_duration(msg: str, token_end: int) -> bool:
    """True when the number token is immediately followed by duration wording (e.g. '1 hour')."""
    if token_end < 0 or token_end > len(msg):
        return False
    return bool(_DURATION_UNIT_TAIL_RE.match(msg[token_end:]))


def _in_service_night(dt: datetime) -> bool:
    """Return True if *dt* is within a service-night (21:00-03:45)."""
    h, m = dt.hour, dt.minute
    if h >= _SN_START:
        return True
    if h < _SN_END_H:
        return True
    if h == _SN_END_H and m <= _SN_END_M:
        return True
    return False


def _in_late_night(dt: datetime) -> bool:
    """Return True if *dt* is in 00:00-03:45."""
    h, m = dt.hour, dt.minute
    return h < _SN_END_H or (h == _SN_END_H and m <= _SN_END_M)


def _hour_in_sn(h24: int) -> bool:
    return h24 in _SN_HOURS_24


def is_immediate_request(message: str) -> bool:
    """Return True if the message signals an immediate / urgent booking."""
    if not message:
        return False
    msg = message.lower().strip()
    return any(kw in msg for kw in IMMEDIATE_KEYWORDS)


def is_later_request(message: str) -> bool:
    """Return True if the message signals a 'later' availability window request."""
    if not message:
        return False
    msg = message.lower().strip()
    return any(kw in msg for kw in LATER_KEYWORDS)


def infer_time_from_hour(hour_provided: int, current_dt: datetime) -> tuple[date_type, int]:
    """
    Infer the closest future date+hour for a given hour.

    If hour_provided is already a 24h value (0, or 13-23), the AM/PM is known
    and we just pick today vs tomorrow. For bare 1-11 values, default to PM.

    Args:
        hour_provided: Hour as extracted \u2014 either 24h (0 or 13-23) or bare 12h (1-12).
        current_dt:    Current timezone-aware datetime.

    Returns:
        (date, resolved_24h_hour) tuple.
    """
    # If AM/PM was explicit (parse_time_from_message returns 24h), skip inference
    if hour_provided == 0 or hour_provided >= 13:
        today = current_dt.date()
        tomorrow = today + timedelta(days=1)
        if hour_provided > current_dt.hour or (hour_provided == current_dt.hour and current_dt.minute == 0):
            return today, hour_provided
        else:
            return tomorrow, hour_provided

    h = current_dt.hour
    m = current_dt.minute
    today = current_dt.date()
    tomorrow = today + timedelta(days=1)

    # For ambiguous bare hours, prefer PM for 1-11.
    # This matches booking intent better than defaulting to morning.
    if 1 <= hour_provided <= 11:
        pm_h = hour_provided + 12
        if pm_h > h or (pm_h == h and m == 0):
            return today, pm_h
        return tomorrow, pm_h

    # 12 stays as 12 (noon) unless AM/PM was explicit in parse_time_from_message.
    if hour_provided > h:
        return today, hour_provided
    return tomorrow, hour_provided


def _hhmm_bare_is_likely_street_number(msg: str, match: re.Match) -> bool:
    """
    True if a hhmm_bare regex match should be ignored — e.g. "150" on "150 Raglan Ave"
    was matching as 1:50. Real colonless times like "930 pm" are kept.

    Heuristics:
    - 3-digit run in 100–199 → almost always a street number band (AU).
    - 3-digit run in 200–999 only if followed by a letter (street name), and not "am"/"pm".
    """
    start, end = match.span()
    left = start
    while left > 0 and msg[left - 1].isdigit():
        left -= 1
    while end < len(msg) and msg[end].isdigit():
        end += 1
    run = msg[left:end]
    if len(run) != 3 or not run.isdigit():
        return False
    val = int(run)
    rest = msg[end:].lstrip()
    if rest and re.match(r"(am|pm)\b", rest, re.IGNORECASE):
        return False
    if 100 <= val <= 199:
        return True
    if 200 <= val <= 999 and rest and rest[0].isalpha():
        if rest.lower().startswith(("pm", "am")):
            return False
        return True
    return False


def parse_time_from_message(message: str) -> int | None:
    """
    Extract a bare 12-hour number from a booking message.

    Returns the extracted hour (1-12) or None.
    Am/pm resolution is left to the caller (use infer_time_from_hour).
    """
    if not message:
        return None

    msg = message.lower()

    # Named time words (midnight = 0, noon = 12) with strict token matching.
    for word in ('midnight', 'mid-night', 'mid night', 'midnite'):
        if re.search(r'\b' + re.escape(word) + r'\b', msg):
            return 0
    for word in ('midday', 'mid-day', 'mid day', 'noon'):
        if re.search(r'\b' + re.escape(word) + r'\b', msg):
            return 12

    # First: HH:MMam/pm format (e.g. "5:30pm", "9:15am") \u2014 must match before bare am/pm
    hhmm_match = re.search(r'\b(\d{1,2}):(\d{2})\s*(am|pm)\b', msg)
    if hhmm_match:
        hour = int(hhmm_match.group(1))
        ampm = hhmm_match.group(3)
        if 1 <= hour <= 12:
            if ampm == 'pm' and hour != 12:
                return hour + 12   # 5:30pm \u2192 17
            elif ampm == 'am' and hour == 12:
                return 0           # 12:00am \u2192 0
            else:
                return hour        # 9:30am \u2192 9, 12:00pm \u2192 12

    # Compact HHMM + am/pm with no space (e.g. "330am", "1145pm") — must run before bare "9 pm"
    compact_ampm = _COMPACT_HHMMAP_RE.search(msg)
    if compact_ampm:
        hour = int(compact_ampm.group(1))
        ampm = compact_ampm.group(3).lower()
        if 1 <= hour <= 12:
            if ampm == "pm" and hour != 12:
                return hour + 12
            elif ampm == "am" and hour == 12:
                return 0
            else:
                return hour

    # Next: bare am/pm (e.g. "11pm", "at 9am") \u2014 convert to 24h so caller doesn't infer wrong direction
    am_pm_match = re.search(r'\b(\d{1,2})\s*(am|pm)\b', msg)
    if am_pm_match:
        hour = int(am_pm_match.group(1))
        ampm = am_pm_match.group(2)
        if 1 <= hour <= 12:
            if ampm == 'pm' and hour != 12:
                return hour + 12   # 9pm \u2192 21
            elif ampm == 'am' and hour == 12:
                return 0           # 12am \u2192 0
            else:
                return hour        # 9am \u2192 9, 12pm \u2192 12

    # 4-digit HHMM without colon or am/pm (e.g. "1130", "at 1130", "930")
    hhmm_bare = re.search(r'\b(0?[1-9]|1[0-2])([0-5]\d)\b', msg)
    if hhmm_bare and _hhmm_bare_is_likely_street_number(msg, hhmm_bare):
        hhmm_bare = None
    if hhmm_bare:
        hour_12 = int(hhmm_bare.group(1))
        # Return as 24h using the same PM-bias logic: if current hour >= hour_12, assume PM
        try:
            from utils.timezone import get_current_datetime
            _now = get_current_datetime()
            _cur_h = _now.hour
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            try:
                from utils.timezone import get_current_datetime
                _cur_h = get_current_datetime().hour
            except Exception:
                _cur_h = 12
        if 1 <= hour_12 <= 11 and _cur_h >= hour_12:
            return hour_12 + 12  # e.g. "1130" at 8pm → 23
        return hour_12

    # Fuzzy: "9ish", "around 9ish", "nineish" — treat as that hour
    ish_match = re.search(r'\b(\d{1,2})ish\b', msg)
    if ish_match:
        h = int(ish_match.group(1))
        if 1 <= h <= 12:
            return h

    # Fuzzy: "between 9 and 10", "between 9-10" — use the first (earlier) hour
    between_match = re.search(r'\bbetween\s+(\d{1,2})\s*(?:and|-)\s*(\d{1,2})\b', msg)
    if between_match:
        h = int(between_match.group(1))
        if 1 <= h <= 12:
            return h

    # Fuzzy: "sometime after 8", "any time after 8", "after 8" — use that hour as minimum
    after_match = re.search(r'\b(?:sometime?\s+)?after\s+(\d{1,2})\b', msg)
    if after_match:
        if _numeric_token_looks_like_duration(msg, after_match.end(1)):
            return None
        h = int(after_match.group(1))
        if 1 <= h <= 12:
            return h

    patterns = [
        r'at\s+(\d{1,2})\b',
        r'around\s+(\d{1,2})\b',
        r'by\s+(\d{1,2})\b',
        r'(?:can|could)\s+you\s+(?:do|see|come)\s+(\d{1,2})\b',
        r'what\s+about\s+(\d{1,2})\b',
        r'how\s+about\s+(\d{1,2})\b',
        r'\b(\d{1,2})\b',
    ]

    for pattern in patterns:
        match = re.search(pattern, msg)
        if match:
            if pattern == r'at\s+(\d{1,2})\b' and _bare_at_hour_followed_by_street(msg, match):
                continue
            if pattern == r'\b(\d{1,2})\b' and _bare_digit_is_street_unit_before_street_type(msg, match):
                continue
            if _numeric_token_looks_like_duration(msg, match.end(1)):
                continue
            hour = int(match.group(1))
            if 1 <= hour <= 12:
                return hour

    return None


def match_colonless_booking_hhmm(message: str) -> tuple[int, int] | None:
    """
    Colonless 12h clock + minutes, no am/pm (e.g. "330", "at 945").
    Excludes street-number runs and compact ``330am`` (handled separately).
    """
    if not message or not message.strip():
        return None
    msg = message.lower().strip()
    if _COMPACT_HHMMAP_RE.search(msg):
        return None
    m = re.search(r"\b(0?[1-9]|1[0-2])([0-5]\d)\b", msg)
    if not m or _hhmm_bare_is_likely_street_number(msg, m):
        return None
    h12, mi = int(m.group(1)), int(m.group(2))
    if 1 <= h12 <= 12 and 0 <= mi < 60:
        return h12, mi
    return None


def _is_vague_tonight_bare_hour(message: str) -> bool:
    """
    Late-night slot-list UX: ``tonight ... at H`` with no :MM, no am/pm, no colonless hhmm.
    """
    low = (message or "").lower().strip()
    if not is_tonight_request(message):
        return False
    if _COMPACT_HHMMAP_RE.search(low):
        return False
    if re.search(r"\d{1,2}:\d{2}", low):
        return False
    if match_colonless_booking_hhmm(message):
        return False
    return bool(re.search(r"\btonight\b.*\bat\s+\d{1,2}\b(?!\s*:)", low))


def _message_has_time_booking_anchor(message: str) -> bool:
    """Bare hour is intentional when tied to booking phrasing (not a stray digit)."""
    low = (message or "").lower()
    return bool(
        re.search(r"\b(?:at|around|by)\s+\d", low)
        or re.search(r"\b(?:can|could)\s+i\s+book\s+(?:you\s+)?at\s+\d", low)
        or re.search(r"\b(?:can|could)\s+you\s+(?:do|see|come)\s+\d", low)
        or re.search(r"\bbook(?:ed|ing)?\s+(?:you\s+)?(?:at|for)\s+\d", low)
    )


def _nearest_ambiguous_12h_clock(now: datetime, hour12: int, minute: int) -> datetime:
    """
    Golden rule for ambiguous 12h clocks (colonless "430", bare hour with anchor, etc.):

    Among the valid AM/PM interpretations on nearby calendar days, pick the **soonest
    local datetime >= now** (minimum forward delta). This matches SMS shorthand where the
    client omits am/pm but means the next sensible occurrence (e.g. "430" mid-morning → pm).
    """
    if not (1 <= hour12 <= 12 and 0 <= minute < 60):
        return now
    tz = now.tzinfo
    cur = now.replace(second=0, microsecond=0)

    def _combine(day: date_type, h24: int, mi: int) -> datetime:
        naive = datetime.combine(day, time_type(h24, mi))
        if tz:
            try:
                return tz.localize(naive)
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
                return naive.replace(tzinfo=tz)
        return naive

    candidates: list[datetime] = []
    for delta in range(-1, 6):
        day = now.date() + timedelta(days=delta)
        for am_like in (True, False):
            if hour12 == 12:
                h24 = 0 if am_like else 12
            elif am_like:
                h24 = hour12
            else:
                h24 = hour12 + 12
            candidates.append(_combine(day, h24, minute))

    future = [d for d in candidates if d >= cur]
    pool = future if future else candidates
    return min(pool, key=lambda d: (d - cur).total_seconds())


def parse_minutes_from_message(message: str) -> int:
    """Minute component when the client named an explicit clock (``:MM`` or compact ``330am``). Else ``0``."""
    if not message or not message.strip():
        return 0
    raw = message.strip()
    # Do not require \\b after MM — "1:45am" has no word boundary between 5 and "a".
    colon = re.search(r"\b\d{1,2}:(\d{2})", raw)
    if colon:
        v = int(colon.group(1))
        return v if 0 <= v < 60 else 0
    compact = _COMPACT_HHMMAP_RE.search(raw.lower())
    if compact:
        v = int(compact.group(2))
        return v if 0 <= v < 60 else 0
    cl = match_colonless_booking_hhmm(raw)
    if cl:
        return cl[1]
    return 0


def message_has_explicit_ampm(message: str) -> bool:
    """True when am/pm is explicit, including compact ``330am`` (no space)."""
    if not message or not message.strip():
        return False
    msg = message.strip().lower()
    if _COMPACT_HHMMAP_RE.search(msg):
        return True
    return bool(re.search(r"\b\d{1,2}(?::\d{2})?\s*(am|pm)\b", message, re.IGNORECASE))


def get_inferred_booking_time(message: str, current_dt: datetime) -> tuple | None:
    """
    Parse message for a time and infer its closest future occurrence.

    Returns (date, hour, minute) or None.
    """
    hour = parse_time_from_message(message)
    if hour is None:
        return None
    inferred_date, inferred_hour = infer_time_from_hour(hour, current_dt)
    return (inferred_date, inferred_hour, 0)


def get_time_reference_string(hour: int) -> str:
    """Convert a 24h hour to a human-readable string like '9pm' or '2am'."""
    if hour == 0:
        return "12:00am"
    if hour < 12:
        return f"{hour}:00am"
    if hour == 12:
        return "12:00pm"
    return f"{hour - 12}:00pm"


def is_tonight_request(message: str) -> bool:
    """Return True if the message contains a 'tonight' availability reference."""
    if not message:
        return False
    msg = message.lower().strip()
    return any(kw in msg for kw in TONIGHT_KEYWORDS)


def is_tomorrow_request(message: str) -> bool:
    """Return True if the message clearly asks for tomorrow.

    Keep keyword list in sync with config.TOMORROW_WORDS (field_collector / booking copy).
    """
    if not message:
        return False
    msg = (message or "").lower()
    return any(
        k in msg
        for k in (
            "tomorrow",
            "tmrw",
            "tmr",
            "tommorow",
            "tommorrow",
            "tomoz",
            "2moro",
            "2morrow",
        )
    )


_WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

def _tomorrow_paired_explicit_wee_hours_clock(message: str) -> bool:
    """
    True when \"tomorrow\" should bind to the *next* calendar day's early morning.

    Vague \"tomorrow\" before 04:00 stays on the same calendar date for evening-slot UX;
    explicit \"tomorrow at 3am\" / \"midnight\" must not snap to that same date's 03:00.
    """
    msg = (message or "").lower().strip()
    if re.search(r"\bmidnight\b", msg):
        return True
    if not message_has_explicit_clock(message):
        return False
    # Bare \"at 3\" without am/pm stays ambiguous — do not force calendar tomorrow.
    if not message_has_explicit_ampm(message) and not _COMPACT_HHMMAP_RE.search(msg):
        return False
    ch = parse_time_from_message(message)
    if ch is None:
        return False
    try:
        hi = int(ch)
    except (TypeError, ValueError):
        return False
    # Service-night tail hours on the *next* morning (after midnight).
    return 0 <= hi <= 4


_WEEKDAY_ALIASES = {
    "monday": ("monday", "mon", "mondy", "monay", "monnday"),
    "tuesday": ("tuesday", "tues", "tue", "teusday", "tuseday", "tuesdy"),
    "wednesday": ("wednesday", "weds", "wed", "wednsday", "wendsday", "wensday"),
    "thursday": ("thursday", "thurs", "thur", "thu", "thurday", "thirsday", "thurdsay", "thrusday"),
    "friday": ("friday", "fri", "firday", "fridayy"),
    "saturday": ("saturday", "sat", "saterday", "satuday", "saturdy", "saterdy"),
    "sunday": ("sunday", "sun", "sunady", "sundey"),
}


def get_requested_day_start(
    now: datetime, message: str
) -> tuple[datetime | None, str | None, datetime | None]:
    """
    Return (start_dt, label, end_dt_optional) when the user explicitly requests a day.

    See GOLDEN_TIME_RULES. Weekday names get end_dt=None; "tomorrow" gets an 11:00–20:00
    listing window (end_dt exclusive at 21:00 on target_date).
    """
    msg = (message or "").lower()
    requested_label = None
    target_date = None

    if is_tomorrow_request(msg):
        requested_label = "tomorrow"
        if now.hour < 4:
            if _tomorrow_paired_explicit_wee_hours_clock(message):
                target_date = now.date() + timedelta(days=1)
            else:
                target_date = now.date()
        else:
            target_date = now.date() + timedelta(days=1)
    else:
        for day_name, day_idx in _WEEKDAY_NAMES.items():
            aliases = _WEEKDAY_ALIASES.get(day_name, (day_name,))
            if any(re.search(r"\b" + re.escape(alias) + r"\b", msg) for alias in aliases):
                delta = (day_idx - now.weekday()) % 7
                target_date = now.date() + timedelta(days=delta)
                requested_label = day_name
                break

    if not target_date:
        return None, None, None

    # Midnight–3am + "tomorrow": window is 18:00–00:00 same date
    # All other times: standard 11:00–21:00 window on target_date
    start_dt = datetime.combine(target_date, time_type(TOMORROW_WINDOW_START_HOUR, 0))

    if getattr(now, "tzinfo", None):
        try:
            start_dt = now.tzinfo.localize(start_dt)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            start_dt = start_dt.replace(tzinfo=now.tzinfo)

    end_dt = None
    if requested_label == "tomorrow":
        end_dt = datetime.combine(target_date, time_type(TOMORROW_WINDOW_END_EXCLUSIVE_HOUR, 0))
        if getattr(now, "tzinfo", None):
            try:
                end_dt = now.tzinfo.localize(end_dt)
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
                end_dt = end_dt.replace(tzinfo=now.tzinfo)

    return start_dt, requested_label, end_dt


def get_tonight_slot_window(now: datetime) -> tuple[datetime | None, datetime | None]:
    """
    Return (start_from, end_by) for slot generation when the client asks about "tonight".

    Does not depend on schedule settings; see GOLDEN_TIME_RULES.
    """
    h = now.hour
    today = now.date()
    tz = now.tzinfo
    next_day = today + timedelta(days=1)

    def _make(d, hour, minute=0):
        dt = datetime.combine(d, time_type(hour, minute))
        if tz:
            try:
                return tz.localize(dt)
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
                return dt.replace(tzinfo=tz)
        return dt

    if h < TONIGHT_WEE_HOURS_END:
        return None, _make(today, TONIGHT_WEE_HOURS_END, 0)
    if TONIGHT_WEE_HOURS_END <= h < TONIGHT_EVENING_START_HOUR:
        return _make(today, TONIGHT_EVENING_START_HOUR, 0), _make(next_day, 0, 0)
    if TONIGHT_EVENING_START_HOUR <= h < 21:
        return None, _make(next_day, 0, 0)
    if 21 <= h <= 23:
        return None, _make(next_day, TONIGHT_WEE_HOURS_END, 0)
    return None, None


def message_has_explicit_clock(message: str) -> bool:
    """
    True when the client named a concrete clock, not just vague words like "tonight".

    Covers optional am/pm and HH:MM with minutes (e.g. "1:45", "01:45 tonight") so we do not
    drop into generic "next 3 slots" flows that skip ✅/❌ for the requested time.
    ``330`` / ``book at 3`` count as explicit; ``tonight at 11`` (bare hour) stays vague for slot UX.
    """
    if not message or not message.strip():
        return False
    if _is_vague_tonight_bare_hour(message):
        return False
    msg = message.strip()
    low = msg.lower()
    if _COMPACT_HHMMAP_RE.search(low):
        return True
    if re.search(r"\b\d{1,2}(?::\d{2})?\s*(am|pm)\b", msg, re.IGNORECASE):
        return True
    if re.search(r"\b\d{1,2}:\d{2}", msg):
        return True
    if match_colonless_booking_hhmm(message):
        return True
    if _message_has_time_booking_anchor(message):
        return True
    return False


_NUMERIC_DATE_LITERAL_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")


def has_invalid_numeric_date_literal(message: str, now: datetime | None = None) -> bool:
    """
    True when the message contains an explicit numeric date token that is invalid.

    Examples flagged:
      - 32/13/2026
      - 31/02
      - 29/02/2025
    """
    if not message or not message.strip():
        return False

    if now is None:
        from utils.timezone import get_current_datetime

        now = get_current_datetime()

    current_year = int(now.year)
    for m in _NUMERIC_DATE_LITERAL_RE.finditer(message):
        day = int(m.group(1))
        month = int(m.group(2))
        year_token = m.group(3)
        year = current_year
        if year_token:
            year = int(year_token) if len(year_token) == 4 else 2000 + int(year_token)

        if day < 1 or day > 31 or month < 1 or month > 12:
            return True
        try:
            datetime(year, month, day)
        except ValueError:
            return True

    return False


def infer_requested_datetime_for_booking(message: str, now: datetime | None = None) -> datetime | None:
    """
    Infer a timezone-aware datetime for messages like "8pm tomorrow" or "7 Friday".

    Mirrors NEW-state specific-time handling in handlers/new_conv/availability.py
    (_stage_avail_specific_time) without the early "tonight" slot-list branches.
    Returns None if no clock time was parsed, or if inference is skipped (wee-hours
    "tonight" path — caller should show slot UI instead).
    """
    import re as _re

    from datetime import datetime as _dt

    from utils.availability_slots import get_business_hours, normalize_business_hours_pair
    from utils.timezone import get_current_datetime

    if not message or not message.strip():
        return None
    now = now or get_current_datetime()
    specific_hour = parse_time_from_message(message)
    if specific_hour is None:
        return None

    inferred_minute = parse_minutes_from_message(message)

    explicit_ampm = message_has_explicit_ampm(message)
    colonless = match_colonless_booking_hhmm(message)
    _colon_clock = bool(_re.search(r"\b\d{1,2}:\d{2}", message))
    bh = normalize_business_hours_pair(get_business_hours()) or (11, 4)
    end_hour = int(bh[1]) if bh and len(bh) > 1 else None
    in_late_night = 0 <= now.hour < end_hour
    _explicit_clock = message_has_explicit_clock(message)

    # Late night + "tonight" without a concrete clock → let caller show next-slot UI.
    if is_tonight_request(message) and in_late_night and not _explicit_clock:
        return None

    inferred_date: date_type
    inferred_hour: int
    out_minute = inferred_minute

    if colonless and not explicit_ampm:
        nearest = _nearest_ambiguous_12h_clock(now, colonless[0], colonless[1])
        inferred_date = nearest.date()
        inferred_hour = nearest.hour
        out_minute = nearest.minute
    elif (
        not explicit_ampm
        and not _colon_clock
        and not colonless
        and _explicit_clock
        and inferred_minute == 0
        and 1 <= int(specific_hour) <= 12
    ):
        nearest = _nearest_ambiguous_12h_clock(now, int(specific_hour), 0)
        inferred_date = nearest.date()
        inferred_hour = nearest.hour
        out_minute = nearest.minute
    elif (
        is_tonight_request(message)
        and not in_late_night
        and not explicit_ampm
        and not _explicit_clock
        and 1 <= int(specific_hour) <= 12
    ):
        inferred_date = now.date()
        inferred_hour = 12 if int(specific_hour) == 12 else int(specific_hour) + 12
    elif explicit_ampm and 1 <= int(specific_hour) <= 11:
        # "1:45am" → parse_time_from_message returns 1 (12h value).
        # infer_time_from_hour would add 12 (PM bias). Since am was explicit, use as-is.
        inferred_hour = int(specific_hour)
        inferred_date = now.date()
        # Bump to tomorrow only when the requested clock time is strictly earlier than "now"
        # (same-hour future minutes like 1:45 when it's 1:30 must stay on today's date).
        if (inferred_hour, inferred_minute) < (now.hour, now.minute):
            inferred_date = now.date() + timedelta(days=1)
        _req_day_start, _, _ = get_requested_day_start(now, message)
        if _req_day_start is not None:
            inferred_date = _req_day_start.date()
    elif (
        _explicit_clock
        and not explicit_ampm
        and _colon_clock
        and 1 <= int(specific_hour) <= 11
    ):
        # "Tonight at 1:45" — colon minutes, no am/pm
        inferred_hour = int(specific_hour)
        inferred_date = now.date()
        if (now.hour, now.minute) > (inferred_hour, inferred_minute):
            inferred_date = now.date() + timedelta(days=1)
        _req_day_start, _, _ = get_requested_day_start(now, message)
        if _req_day_start is not None:
            inferred_date = _req_day_start.date()
    else:
        inferred_date, inferred_hour = infer_time_from_hour(specific_hour, now)

    _req_day_start, _, _ = get_requested_day_start(now, message)
    if _req_day_start is not None:
        inferred_date = _req_day_start.date()

    try:
        from utils.timezone import get_local_timezone

        _escort_tz = get_local_timezone()
        extracted_time = _escort_tz.localize(
            _dt.combine(inferred_date, _dt.min.time().replace(hour=inferred_hour, minute=out_minute))
        )
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        extracted_time = _dt.combine(inferred_date, _dt.min.time().replace(hour=inferred_hour, minute=out_minute))

    try:
        now_tz = getattr(now, "tzinfo", None)
        ext_tz = getattr(extracted_time, "tzinfo", None)
        if now_tz and ext_tz is None:
            try:
                extracted_time = now_tz.localize(extracted_time)
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
                extracted_time = extracted_time.replace(tzinfo=now_tz)
        elif ext_tz and not now_tz:
            extracted_time = extracted_time.replace(tzinfo=None)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)

    return extracted_time
