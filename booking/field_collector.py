"""
Field Collector - Centralized logic for collecting booking fields.
Extracts date, time, duration, experience_type, incall_outcall from messages.
Uses pattern matching first, then AI extraction as fallback.

Golden date rules (align with utils.time_parser.GOLDEN_TIME_RULES):
- Colonless clock + minutes with no am/pm (e.g. "430") resolves to the nearest *future*
  wall-clock instant among AM/PM readings — same rule as ``_nearest_ambiguous_12h_clock``.
  When no explicit calendar day was extracted, that instant's date is stored so bookings
  do not silently anchor to the wrong day.
- Explicit "tomorrow" / "tomorrow night" (and TOMORROW_WORDS aliases) resolve the
  calendar day via get_requested_day_start — hour < 4 usually keeps *same* calendar day for
  vague evening wording; explicit wee-hours clocks with "tomorrow" (e.g. "tomorrow at 3am")
  use the *next* calendar day (see GOLDEN_TIME_RULES).
- Pattern extraction calls ``_parse_date(..., implicit_today=False)``: vague booking
  messages do **not** persist an inferred calendar day; only explicit date cues become
  ``extracted['date']``. Callers that still need the golden default-to-today behaviour
  can pass ``implicit_today=True`` (see the final branch in ``_parse_date``).
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from utils.log_sanitize import LOG_SUPPRESSED_FMT
from utils.timezone import get_current_datetime, get_local_timezone
from utils.time_parser import (
    get_requested_day_start,
    match_colonless_booking_hhmm,
    message_has_explicit_ampm,
    _nearest_ambiguous_12h_clock,
)

logger = logging.getLogger("escort_chatbot.field_collector")


def _templates_first_enabled() -> bool:
    try:
        from core.settings_manager import get_setting
        return (get_setting("ai_templates_first") or "").lower() == "true"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return False

# After "at 9" / "around 9", detect "9 Cantle St" style tails so we do not read the unit number as 9pm.
_AFTER_AT_HOUR_STREET_RE = re.compile(
    r"(?:[a-z0-9]+\s+){0,4}"
    r"(?:st|street|road|rd|avenue|ave|drive|dr|terrace|tce|crescent|cres|close|cl|place|pl|"
    r"court|ct|way|parade|pde|boulevard|blvd|lane|ln)\b",
    re.IGNORECASE,
)


def _after_bare_hour_looks_like_street_address(text: str, match: re.Match) -> bool:
    tail = text[match.end() :].strip()
    if not tail:
        return False
    return bool(_AFTER_AT_HOUR_STREET_RE.match(tail))

# Vague phrases that are NOT real outcall addresses (cannot be geocoded)
_VAGUE_ADDRESS_PHRASES = frozenset({
    'my place', 'my home', 'my house', 'my apartment', 'my apt', 'my flat',
    'my location', 'my address', 'my hotel', 'my room', 'my unit', 'home',
    'my place atm', 'here', 'my area',
})

_DATE_TOKEN_NORMALIZATION = {
    # Weekday misspellings
    "monay": "monday",
    "monda": "monday",
    "mondy": "monday",
    "monnday": "monday",
    "teusday": "tuesday",
    "tuesdy": "tuesday",
    "tuseday": "tuesday",
    "wednsday": "wednesday",
    "wendsday": "wednesday",
    "wensday": "wednesday",
    "thurday": "thursday",
    "thirsday": "thursday",
    "thurdsay": "thursday",
    "thrusday": "thursday",
    "fridayy": "friday",
    "firday": "friday",
    "saterday": "saturday",
    "satuday": "saturday",
    "saturdy": "saturday",
    "saterdy": "saturday",
    "sunady": "sunday",
    "sundey": "sunday",
    # Month misspellings
    "janurary": "january",
    "januaray": "january",
    "febuary": "february",
    "februrary": "february",
    "marhc": "march",
    "aprill": "april",
    "juen": "june",
    "juley": "july",
    "jully": "july",
    "augest": "august",
    "septemper": "september",
    "septeber": "september",
    "setember": "september",
    "octomber": "october",
    "novemeber": "november",
    "novembar": "november",
    "decemeber": "december",
    "decembar": "december",
    # Common SMS typos (novel sim NE027)
    "tomorow": "tomorrow",
    "tommorrow": "tomorrow",
}

_AI_CORRECTION_CUES = (
    "no i mean",
    "i mean",
    "i meant",
    "actually",
    "instead",
    "change it",
    "change to",
    "make it",
    "not ",
)


def _is_vague_address(address: str) -> bool:
    """Returns True if address is a generic/vague phrase, not a geocodable location."""
    return (address or '').strip().lower() in _VAGUE_ADDRESS_PHRASES


def _is_non_address_text(address: str) -> bool:
    """Return True for values that look like time/booking text rather than a location."""
    value = (address or "").strip().lower()
    if not value:
        return True
    if _is_vague_address(value):
        return True

    # Time-only phrases (e.g. "7pm", "7:30 pm tonight", "at 9pm tomorrow")
    if re.fullmatch(
        r"(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?(?:\s+(?:today|tonight|tomorrow|now|asap))?",
        value,
        re.IGNORECASE,
    ):
        return True

    # Booking-intent phrases accidentally returned by AI instead of address.
    booking_tail_markers = (
        "come to my place", "come to me", "my place", "book", "booking", "session",
        "tonight", "tomorrow", "today", "asap", "now",
    )
    has_location_marker = bool(
        re.search(
            r"\d+\s+[a-z]|hotel|street|st\b|road|rd\b|avenue|ave\b|drive|dr\b|terrace|tce\b|adelaide|quest|oaks|hilton",
            value,
            re.IGNORECASE,
        )
    )
    if any(marker in value for marker in booking_tail_markers) and not has_location_marker:
        return True

    return False


def _normalize_date_tokens(text: str) -> str:
    """Normalize common weekday/month misspellings before date parsing."""
    normalized = (text or "").lower()
    # Spanish day cues (lightweight — keeps mañana bookings from falling through silently)
    normalized = normalized.replace("mañana", "tomorrow").replace("manana", "tomorrow")
    normalized = re.sub(r"\ba\s+las\s+ocho\s+pm\b", "8pm", normalized)
    normalized = re.sub(r"\ba\s+las\s+ocho\b", "8pm", normalized)
    normalized = re.sub(r"\ba\s+las\s+siete\s+pm\b", "7pm", normalized)
    normalized = re.sub(r"\ba\s+las\s+siete\b", "7pm", normalized)
    for wrong, right in sorted(_DATE_TOKEN_NORMALIZATION.items(), key=lambda kv: -len(kv[0])):
        normalized = re.sub(r"\b" + re.escape(wrong) + r"\b", right, normalized)
    return normalized


def _normalize_sms_booking_typos(text: str) -> str:
    """Fix frequent autocorrect/OCR-style typos before pattern extraction."""
    if not text:
        return text
    out = text
    out = re.sub(r"\b(\d{1,2})\s*pn\b", r"\1pm", out, flags=re.IGNORECASE)
    out = re.sub(r"\bincsll\b", "incall", out, flags=re.IGNORECASE)
    out = re.sub(r"\boutsll\b", "outcall", out, flags=re.IGNORECASE)
    return out


def _strip_log_timestamp_fragments(text: str) -> str:
    """Remove pasted admin/log datetimes so embedded HH:MM is never parsed as the client's booking time."""
    if not text:
        return text
    out = text
    # AU-style "05/05/2026, 23:00:00" or "5/5/2026, 6:35:18"
    out = re.sub(
        r"\b\d{1,2}/\d{1,2}/\d{4}\s*,\s*\d{1,2}:\d{2}(?::\d{2})?\b",
        " ",
        out,
        flags=re.IGNORECASE,
    )
    # ISO-ish "2026-05-05T06:35:18Z" / "2026-05-05 06:35:18"
    out = re.sub(
        r"\b\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b",
        " ",
        out,
        flags=re.IGNORECASE,
    )
    return out


class FieldCollector:
    """Collects and validates booking fields from messages."""

    def __init__(self, config, ai_service=None, message_history=None):
        """
        Initialize field collector.

        Args:
            config: Configuration module with business rules
            ai_service: Optional AI service for enhanced extraction
            message_history: Optional recent conversation turns for AI extraction context
        """
        self.config = config
        self.ai_service = ai_service
        self.message_history = message_history or []

    def extract_fields(self, message: str, current_fields: dict[str, Any] = None) -> dict[str, Any]:
        """
        Extract booking fields from message.
        Uses pattern matching first, then AI extraction as fallback.

        Args:
            message: User message
            current_fields: Current booking fields

        Returns:
            Dict with extracted fields
        """
        message = _normalize_sms_booking_typos(message or "")
        # Step 1: Try pattern-based extraction (fast, reliable)
        extracted = self._extract_with_patterns(message, current_fields)

        # Step 2: If pattern matching found nothing or incomplete, try AI extraction (unless templates-first is on)
        correction_hint = self._has_correction_intent(message)
        if (
            self.ai_service
            and not _templates_first_enabled()
            and (correction_hint or not extracted or self._should_try_ai(message, extracted))
        ):
            ai_extracted = self._extract_with_ai(message)
            if ai_extracted:
                # Merge AI results, preferring pattern matches if both exist
                for key, value in ai_extracted.items():
                    if key == 'outcall_address' and value and _is_non_address_text(str(value)):
                        logger.info(f"AI extracted non-address text for outcall_address '{value}' - ignoring")
                        continue
                    # Never let AI overwrite a pattern-derived time unless the client is correcting.
                    if (
                        key == "time"
                        and extracted.get("time") is not None
                        and not correction_hint
                    ):
                        continue
                    if (
                        correction_hint
                        and key in {"date", "time", "duration"}
                        and value is not None
                    ):
                        extracted[key] = value
                        logger.info(f"AI correction override applied: {key} = {value}")
                    elif key not in extracted and value is not None:
                        extracted[key] = value
                        logger.info(f"AI extraction added field: {key} = {value}")

        return extracted

    def _extract_with_patterns(self, message: str, current_fields: dict[str, Any] = None) -> dict[str, Any]:
        """Extract fields using pattern matching (original method).

        Detection priority: duration \u2192 date \u2192 time.
        Duration numbers are identified first so they are not misread as times.
        """
        extracted = {}

        # 1. Duration FIRST \u2014 prevents round numbers (30, 60, 90) being mis-read as times/dates
        duration = self._parse_duration(message)
        if duration:
            extracted['duration'] = duration

        # 2. Date
        date = self._parse_date(message, implicit_today=False)
        if date:
            extracted['date'] = date

        # 3. Time (with duration context so bare-number resolution can skip duration matches)
        time = self._parse_time(message, duration_minutes=duration)
        if time:
            extracted['time'] = time
            if not extracted.get('date'):
                cl = match_colonless_booking_hhmm(message)
                if cl and not message_has_explicit_ampm(message):
                    nearest = _nearest_ambiguous_12h_clock(get_current_datetime(), cl[0], cl[1])
                    if nearest.hour == time[0] and nearest.minute == time[1]:
                        extracted['date'] = nearest.date()

        # Extract experience type
        experience = self._parse_experience_type(message)
        if experience:
            extracted['experience_type'] = experience

        # Extract incall/outcall
        incall_outcall = self._parse_incall_outcall(message)
        if incall_outcall:
            extracted['incall_outcall'] = incall_outcall

        # Extract outcall address (if outcall)
        if incall_outcall == 'outcall' or (current_fields and current_fields.get('incall_outcall') == 'outcall'):
            address = self._parse_outcall_address(message)
            if address:
                logger.info(f"[EXTRACT] Outcall address extracted: '{address}' from message: '{message}'")
                extracted['outcall_address'] = address
            else:
                logger.info(f"[EXTRACT] No outcall address found in message: '{message}'")

        return extracted

    def _should_try_ai(self, message: str, pattern_extracted: dict[str, Any]) -> bool:
        """
        Determine if AI extraction should be attempted.
        Returns True if:
        - No fields extracted by patterns
        - Message seems complex/messy
        - Message is long (>50 chars) but few fields extracted
        """
        # No fields extracted - definitely try AI
        if not pattern_extracted:
            return True

        if self._has_correction_intent(message):
            return True

        # Short "N hour(s) is fine" confirmations — pattern already got duration,
        # AI would misread "1" as a time (1am). Skip AI for these.
        if pattern_extracted.get("duration") and not pattern_extracted.get("time") and not pattern_extracted.get("date"):
            _remaining = re.sub(
                r"\b\d+(?:\.\d+)?\s*(?:hours?|hrs?|hr|mins?|minutes?|min)\b",
                "",
                message,
                flags=re.IGNORECASE,
            ).strip(" ,.!?")
            _dur_confirm = re.fullmatch(
                r"(?:(?:is|are|that(?:'?s)?|that\s+is|it(?:'?s)?|sounds?)\s+)?"
                r"(?:yes|yeah|yep|yup|sure|ok|okay|fine|perfect|great|good|alright|"
                r"no\s+worries?|that\s+works?|works?\s+for\s+me)\s*[!.]*",
                _remaining,
                re.IGNORECASE,
            )
            if _dur_confirm:
                return False

        # Message is long but few fields extracted - might be complex
        if len(message) > 50 and len(pattern_extracted) <= 1:
            return True

        # Message contains booking-related keywords but pattern matching missed them
        booking_keywords = [
            'book', 'appointment', 'available', 'time', 'date',
            'tomorrow', 'tonight', 'hour', 'minute', 'duration',
            'gfe', 'pse', 'incall', 'outcall', 'hotel', 'address',
            'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
            'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun',
        ]
        has_keywords = any(keyword in message.lower() for keyword in booking_keywords)
        if has_keywords and len(pattern_extracted) <= 1:
            return True

        return False

    def _has_correction_intent(self, message: str) -> bool:
        """Return True when the message looks like a correction to prior booking details."""
        msg = (message or "").strip().lower()
        if not msg:
            return False
        return any(cue in msg for cue in _AI_CORRECTION_CUES)

    def _extract_with_ai(self, message: str) -> dict[str, Any]:
        """Extract fields using AI service."""
        if not self.ai_service:
            return {}

        try:
            tz = get_local_timezone()
            current_date = datetime.now(tz)

            ai_extracted = self.ai_service.extract_booking_fields(
                message, current_date, history=self.message_history or None
            )
            return ai_extracted

        except Exception as e:
            logger.warning(f"AI extraction failed: {e}")
            return {}

    def _parse_date(self, message: str, *, implicit_today: bool = True) -> datetime | None:
        """Parse date from message with service-night-aware logic.

        Service-night: 21:00 – 03:45.

        "Tomorrow", "tomorrow night", and config.TOMORROW_WORDS aliases use
        utils.time_parser.get_requested_day_start (see GOLDEN_TIME_RULES): if local hour < 4,
        target calendar day is usually the same as *now* for vague evening listings,
        unless the client named an explicit wee-hours clock with \"tomorrow\" (then next day).

        If ``implicit_today`` is True and no explicit date cue matches, returns *today*
        (timezone-aware). If False, returns None so callers do not persist a fake booking day.
        """
        message_lower = _normalize_date_tokens(message)

        tz = get_local_timezone()
        today = datetime.now(tz)

        current_hour = today.hour
        current_minute = today.minute

        # Service-night: 21:00-03:45
        is_service_night = (
            current_hour >= 21
            or current_hour < 3
            or (current_hour == 3 and current_minute <= 45)
        )

        # \u2500\u2500 "tomorrow night" before TODAY_WORDS so it is not masked by "this evening" \u2500\u2500
        if "tomorrow night" in message_lower or "tomorrow nite" in message_lower:
            start_dt, requested_label, _ = get_requested_day_start(today, message)
            if requested_label == "tomorrow":
                td = start_dt.date()
                return today.replace(year=td.year, month=td.month, day=td.day)

        # \u2500\u2500 Today / tonight keywords (before plain "tomorrow" so e.g. "today" wins) \u2500\u2500
        for word in self.config.TODAY_WORDS:
            if word in message_lower:
                if ("midnight" in message_lower or "12am" in message_lower) and word in ("tonight", "tonite", "2nite"):
                    return today + timedelta(days=1)
                return today

        if is_service_night:
            for word in self.config.TONIGHT_WORDS:
                if word in message_lower:
                    return today

        # \u2500\u2500 "tomorrow" / TOMORROW_WORDS aliases (tomorrow night handled above) \u2500\u2500
        start_dt, requested_label, _ = get_requested_day_start(today, message)
        if requested_label == "tomorrow":
            td = start_dt.date()
            return today.replace(year=td.year, month=td.month, day=td.day)

        # Next week patterns
        if "next week" in message_lower or "this time next week" in message_lower:
            # Check for specific day
            days = {
                "monday": 0, "mon": 0,
                "tuesday": 1, "tue": 1, "tues": 1,
                "wednesday": 2, "wed": 2,
                "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
                "friday": 4, "fri": 4,
                "saturday": 5, "sat": 5,
                "sunday": 6, "sun": 6
            }

            for day_name, day_num in days.items():
                if day_name in message_lower:
                    # Find next occurrence of this day in next week
                    days_ahead = (7 - today.weekday()) + day_num
                    if days_ahead < 7:
                        days_ahead += 7
                    return today + timedelta(days=days_ahead)

            # "this time next week" without specific day = same day next week
            return today + timedelta(days=7)

        # Australian Public Holidays
        current_year = today.year
        holidays = self._get_australian_holidays(current_year)

        for holiday_name, holiday_date in holidays.items():
            if holiday_name in message_lower:
                # If holiday has passed this year, assume next year
                if holiday_date.date() < today.date():
                    next_year_holidays = self._get_australian_holidays(current_year + 1)
                    holiday_date = next_year_holidays[holiday_name]
                return tz.localize(holiday_date)

        # Day names (Monday, Tuesday, etc.) - these are FUTURE days (next occurrence)
        days = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
            "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6
        }

        for day_name, day_num in days.items():
            if day_name in message_lower:
                days_ahead = day_num - today.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                return today + timedelta(days=days_ahead)

        # Date formats: dd/mm/yyyy, dd/mm/yy, dd/mm, dd-mm
        date_patterns = [
            r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})',
            r'(\d{1,2})[/-](\d{1,2})[/-](\d{2})',
            r'(\d{1,2})[/-](\d{1,2})(?:\s|$)'
        ]

        for pattern in date_patterns:
            match = re.search(pattern, message)
            if match:
                try:
                    if match and match.lastindex and match.lastindex >= 2:
                        day = int(match.group(1))
                        month = int(match.group(2))
                    else:
                        day = month = None

                    if not (1 <= day <= 31 and 1 <= month <= 12):
                        continue

                    if len(match.groups()) >= 3 and match.group(3):
                        year_str = match.group(3)
                        year = int(year_str) if len(year_str) == 4 else 2000 + int(year_str)
                    else:
                        year = today.year

                    parsed = datetime(year, month, day)
                    if parsed.date() < today.date():
                        parsed = datetime(year + 1, month, day)

                    return tz.localize(parsed)
                except ValueError:
                    continue

        # \u2500\u2500 Ordinal dates: "15th", "21st March", "March 15", "the 3rd" \u2500\u2500
        # Numbers adjacent to month names or ordinal suffixes are treated as dates.
        month_map = {
            "january": 1, "jan": 1, "february": 2, "feb": 2,
            "march": 3, "mar": 3, "april": 4, "apr": 4,
            "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
            "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
            "october": 10, "oct": 10, "november": 11, "nov": 11,
            "december": 12, "dec": 12,
        }

        # Pattern: "<day> <month>" or "<month> <day>" with optional ordinal suffix
        ordinal_with_month = re.search(
            r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(' + '|'.join(month_map) + r')\b'
            r'|\b(' + '|'.join(month_map) + r')\s+(\d{1,2})(?:st|nd|rd|th)?\b',
            message_lower
        )
        if ordinal_with_month:
            try:
                g = ordinal_with_month.groups()
                if g[0] and g[1]:
                    day, month_name = int(g[0]), g[1]
                else:
                    month_name, day = g[2], int(g[3])
                month_num = month_map[month_name]
                parsed = datetime(today.year, month_num, day)
                if parsed.date() < today.date():
                    parsed = datetime(today.year + 1, month_num, day)
                return tz.localize(parsed)
            except (ValueError, TypeError):
                pass

        # Pattern: standalone ordinal "the 15th" / "15th" (no month \u2014 use current/next month)
        ordinal_only = re.search(r'\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b|\b(\d{1,2})(?:st|nd|rd|th)\b', message_lower)
        if ordinal_only:
            try:
                day = int((ordinal_only.group(1) or ordinal_only.group(2)) if ordinal_only and ordinal_only.lastindex and ordinal_only.lastindex >= 2 else 0)
                if 1 <= day <= 31:
                    parsed = datetime(today.year, today.month, day)
                    if parsed.date() <= today.date():
                        # Advance to next month
                        next_month = today.month % 12 + 1
                        next_year = today.year + (1 if next_month == 1 else 0)
                        parsed = datetime(next_year, next_month, day)
                    return tz.localize(parsed)
            except (ValueError, TypeError):
                pass

        # GOLDEN default: no recognised date phrase → today only when caller asks for it.
        if implicit_today:
            logger.debug(
                "No explicit date found in message, assuming TODAY (service_night=%s)",
                is_service_night,
            )
            return today
        return None

    def _get_australian_holidays(self, year: int) -> dict:
        """
        Get Australian public holidays for a given year.

        Returns:
            Dict mapping holiday keywords to datetime objects
        """
        # Fixed date holidays
        holidays = {
            "new years day": datetime(year, 1, 1),
            "new year": datetime(year, 1, 1),
            "australia day": datetime(year, 1, 26),
            "anzac day": datetime(year, 4, 25),
            "christmas": datetime(year, 12, 25),
            "christmas day": datetime(year, 12, 25),
            "xmas": datetime(year, 12, 25),
            "boxing day": datetime(year, 12, 26),
        }

        # Easter (dynamic calculation using dateutil)
        try:
            from dateutil.easter import easter as _easter_date
            _ed = _easter_date(year)
            easter_sunday = datetime(_ed.year, _ed.month, _ed.day)
            holidays["easter"] = easter_sunday
            holidays["easter sunday"] = easter_sunday
            holidays["good friday"] = easter_sunday - timedelta(days=2)
            holidays["easter monday"] = easter_sunday + timedelta(days=1)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)

        # Queen's Birthday (second Monday in June for most states)
        june_first = datetime(year, 6, 1)
        days_until_monday = (7 - june_first.weekday()) % 7
        first_monday = june_first + timedelta(days=days_until_monday)
        second_monday_june = first_monday + timedelta(days=7)
        holidays["queens birthday"] = second_monday_june
        holidays["queen's birthday"] = second_monday_june

        return holidays

    def _parse_time(self, message: str, duration_minutes: int | None = None) -> tuple[int, int] | None:
        """Parse time from message. Returns (hour, minute) in 24-hour format.

        During service-night (21:00-03:45): bare hours are resolved using the
        unique-occurrence-in-service-night rule (e.g. "10" \u2192 10 pm, "2" \u2192 2 am).
        Outside service-night: assume PM if the current time is already past that hour.

        Args:
            duration_minutes: If a duration was already extracted from this message,
                              bare-number matching skips values that match common duration
                              multiples (15, 30, 45, 60, 90, 120).
        """
        message_lower = message.lower()
        # Strip address-tail content so street numbers (e.g. "158 raglan ave ...")
        # are never mistaken for shorthand times like 1:58.
        _time_parse_text = re.sub(
            r"\b(?:my\s+address\s+is|address\s+is|located\s+at|staying\s+at)\b.*$",
            "",
            message_lower,
            flags=re.IGNORECASE,
        )
        _time_parse_text = _strip_log_timestamp_fragments(_time_parse_text)
        now = get_current_datetime()
        current_hour = now.hour
        current_minute = now.minute

        # Service-night boundaries
        _SN_START = 21
        _SN_END_H, _SN_END_M = 3, 45
        in_service_night = (
            current_hour >= _SN_START
            or current_hour < _SN_END_H
            or (current_hour == _SN_END_H and current_minute <= _SN_END_M)
        )
        # 24h values that are inside the service-night window
        _SN_24 = {21, 22, 23, 0, 1, 2, 3}
        # Daypart cues should force bare 1-11 hours to PM (e.g. "tomorrow afternoon around 4").
        _prefer_pm_for_bare_hour = bool(
            re.search(r'\b(afternoon|arvo|evening|tonight|tonite|nite|night)\b', _time_parse_text)
        )

        def _resolve_bare_hour(hour_12: int, minute: int) -> tuple[int, int]:
            """
            Resolve a bare 12h hour to its 24h equivalent using service-night logic.

            During service-night: if only one of {am_h, pm_h} falls in the
            service-night window, use that (unique-occurrence rule).
            Outside service-night: use PM if current hour is already past this hour.
            """
            am_h = 0 if hour_12 == 12 else hour_12
            pm_h = 12 if hour_12 == 12 else hour_12 + 12

            if in_service_night:
                am_in = am_h in _SN_24
                pm_in = pm_h in _SN_24
                if am_in and not pm_in:
                    return (am_h, minute)
                if pm_in and not am_in:
                    return (pm_h, minute)
                # Both or neither in window \u2014 fall back to standard logic
            if _prefer_pm_for_bare_hour and 1 <= hour_12 <= 11:
                return (hour_12 + 12, minute)
            if _prefer_pm_for_bare_hour and hour_12 == 12:
                return (12, minute)
            if hour_12 == 12:
                # Bare "12" / "12:xx" defaults to noon unless AM/PM was explicit.
                return (12, minute)
            # Standard: past this hour \u2192 assume PM
            if 1 <= hour_12 <= 12:
                if current_hour >= hour_12:
                    return (hour_12 + 12 if hour_12 < 12 else 12, minute)
                return (hour_12 if hour_12 < 12 else 12, minute)
            return (hour_12, minute)

        # Alias for the old name used in pattern-matching below
        _assume_pm_if_past_hour = _resolve_bare_hour

        # Duration numbers to skip (already claimed as duration)
        _DURATION_MULTIPLES = {15, 30, 45, 60, 90, 120, 150, 180, 240}
        _skip_bare = duration_minutes is not None

        # Midday/noon (strict token match so "afternoon" does not trigger "noon")
        for word in self.config.MIDDAY_WORDS:
            if re.search(r"\b" + re.escape(word) + r"\b", message_lower):
                return (12, 0)

        # Midnight (strict token match)
        for word in self.config.MIDNIGHT_WORDS:
            if re.search(r"\b" + re.escape(word) + r"\b", message_lower):
                return (0, 0)

        # 4-digit time without colon or am/pm: 1030, 930, 1130 (10:30, 9:30, 11:30)
        four_digit = re.search(r'\b(0?[1-9]|1[0-2])(\d{2})\b', _time_parse_text)
        if four_digit and not re.search(r'\d{3,4}\s*(am|pm)\b', _time_parse_text):
            # Ignore 3-digit hhmm-like street numbers (e.g. "158 Raglan Ave").
            if (
                len(four_digit.group(0)) == 3
                and self._bare_number_looks_like_street_number(_time_parse_text, four_digit.end())
            ):
                four_digit = None
        if four_digit and not re.search(r'\d{3,4}\s*(am|pm)\b', _time_parse_text):
            try:
                if four_digit and four_digit.lastindex and four_digit.lastindex >= 2:
                    hour_12 = int(four_digit.group(1))
                    minute = int(four_digit.group(2))
                else:
                    hour_12 = minute = None
                if 0 <= minute <= 59:
                    if not message_has_explicit_ampm(_time_parse_text):
                        # Golden rule: bare colonless times → nearest-future 12h interpretation
                        nearest = _nearest_ambiguous_12h_clock(now, hour_12, minute)
                        return (nearest.hour, nearest.minute)
                    h24, m = _assume_pm_if_past_hour(hour_12, minute)
                    if 0 <= h24 <= 23:
                        return (h24, m)
            except (ValueError, IndexError):
                pass

        # Explicit am/pm and HH:MM must win over bare "at 9 ..." (street unit numbers).
        # 4-digit time with am/pm (1130pm, 0930am) - MUST be before 3-digit so "1130pm" -> 11:30pm not 1:30pm
        four_digit_ampm = re.search(r'\b(1[0-2]|0?[1-9])(\d{2})\s*(am|pm)\b', _time_parse_text)
        if four_digit_ampm:
            try:
                if four_digit_ampm and four_digit_ampm.lastindex and four_digit_ampm.lastindex >= 3:
                    hour_12 = int(four_digit_ampm.group(1))
                    minute = int(four_digit_ampm.group(2))
                    ampm = four_digit_ampm.group(3).lower()
                else:
                    hour_12 = minute = None
                    ampm = ''
                if 0 <= minute <= 59:
                    if ampm == 'pm' and hour_12 < 12:
                        hour_24 = hour_12 + 12
                    elif ampm == 'pm':
                        hour_24 = 12  # 12pm
                    elif ampm == 'am' and hour_12 == 12:
                        hour_24 = 0  # 12am
                    else:
                        hour_24 = hour_12  # 1-11am
                    if 0 <= hour_24 <= 23:
                        return (hour_24, minute)
            except (ValueError, IndexError):
                pass

        # 3-digit shorthand times like "830pm" \u2192 8:30pm, "930am" \u2192 9:30am
        shorthand_match = re.search(r'(\d)(\d{2})\s?(am|pm)', _time_parse_text)
        if shorthand_match:
            try:
                if shorthand_match and shorthand_match.lastindex and shorthand_match.lastindex >= 3:
                    hour = int(shorthand_match.group(1))
                    minute = int(shorthand_match.group(2))
                    ampm = shorthand_match.group(3).lower()
                else:
                    hour = minute = None
                    ampm = ''
                if ampm == 'pm' and hour < 12:
                    hour += 12
                elif ampm == 'am' and hour == 12:
                    hour = 0
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    return (hour, minute)
            except (ValueError, IndexError):
                pass

        # Bare "7pm" / "11 am" must win over colon times (and never pair a colon clock with unrelated am/pm elsewhere).
        _explicit_ampm = re.search(r'\b(\d{1,2})\s*(am|pm)\b', _time_parse_text)
        if _explicit_ampm:
            try:
                if _explicit_ampm and _explicit_ampm.lastindex and _explicit_ampm.lastindex >= 2:
                    hour_12 = int(_explicit_ampm.group(1))
                    ampm = _explicit_ampm.group(2).lower()
                else:
                    hour_12 = None
                    ampm = ''
                if 1 <= hour_12 <= 12:
                    if ampm == "pm" and hour_12 < 12:
                        hour = hour_12 + 12
                    elif ampm == "pm":
                        hour = 12
                    elif ampm == "am" and hour_12 == 12:
                        hour = 0
                    else:
                        hour = hour_12
                    if 0 <= hour <= 23:
                        return (hour, 0)
            except (ValueError, IndexError):
                pass

        # Colon times — optional am/pm only on this token (group 3). No global am/pm scan.
        _colon = re.search(r'\b(\d{1,2}):(\d{2})\s*(am|pm)?\b', _time_parse_text)
        if _colon:
            try:
                if _colon and _colon.lastindex and _colon.lastindex >= 3:
                    hour = int(_colon.group(1))
                    minute = int(_colon.group(2))
                    ampm_g = (_colon.group(3) or "").strip().lower()
                else:
                    hour = minute = None
                    ampm_g = ''
                if ampm_g in ("am", "pm"):
                    if ampm_g == "pm" and hour < 12:
                        hour += 12
                    elif ampm_g == "pm" and hour == 12:
                        hour = 12
                    elif ampm_g == "am" and hour == 12:
                        hour = 0
                else:
                    hour, minute = _assume_pm_if_past_hour(hour, minute)
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    return (hour, minute)
            except (ValueError, IndexError):
                pass

        # Military HHMM (e.g. 1500)
        _mil = re.search(r'\b(\d{4})(?:\s|$)', _time_parse_text)
        if _mil:
            try:
                if _mil and _mil.lastindex and _mil.lastindex >= 1:
                    raw = _mil.group(1)
                    hour = int(raw[:2])
                    minute = int(raw[2:])
                else:
                    hour = minute = None
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    return (hour, minute)
            except (ValueError, IndexError):
                pass

        # "at around 4", "around 4"
        around_hour_match = re.search(r'\b(?:at\s+)?around\s+(\d{1,2})\b', _time_parse_text)
        if around_hour_match:
            try:
                hour_12 = int(around_hour_match.group(1)) if around_hour_match and around_hour_match.lastindex and around_hour_match.lastindex >= 1 else None
                if 1 <= hour_12 <= 12:
                    h24, m = _assume_pm_if_past_hour(hour_12, 0)
                    if 0 <= h24 <= 23:
                        return (h24, m)
            except (ValueError, IndexError):
                pass

        # "at 10", "at 9" (bare hour) — skip when "at 9 Cantle St" etc.
        at_hour_match = re.search(r'\bat\s+(\d{1,2})\b', _time_parse_text)
        if at_hour_match and not _after_bare_hour_looks_like_street_address(_time_parse_text, at_hour_match):
            try:
                hour_12 = int(at_hour_match.group(1)) if at_hour_match and at_hour_match.lastindex and at_hour_match.lastindex >= 1 else None
                if 1 <= hour_12 <= 12:
                    h24, m = _assume_pm_if_past_hour(hour_12, 0)
                    if 0 <= h24 <= 23:
                        return (h24, m)
            except (ValueError, IndexError):
                pass

        # Selection / confirmation patterns: "yeah 11", "11 suits me", "11 works", "the 11 one"
        # Handles client picking from a list of offered time slots e.g. "yeah 11 suits me"
        _sel = re.search(
            r'\byeah\s+(\d{1,2})\b'
            r'|\b(\d{1,2})\s+(?:suits?\s+(?:me|us)?|works?\s*(?:for\s*(?:me|us))?|sounds?\s+good|is\s+(?:fine|good|perfect)|would\s+work)\b'
            r'|\bthe\s+(\d{1,2})\s+(?:one|slot|time)\b',
            _time_parse_text
        )
        if _sel:
            _sel_hour = int(next(g for g in _sel.groups() if g is not None))
            if 1 <= _sel_hour <= 12 and not (_skip_bare and _sel_hour in _DURATION_MULTIPLES):
                h24, m = _assume_pm_if_past_hour(_sel_hour, 0)
                if 0 <= h24 <= 23:
                    return (h24, m)

        return None

    def _parse_duration(self, message: str) -> int | None:
        """Parse duration from message. Returns duration in minutes."""
        message_lower = message.lower()
        # Normalize European comma-as-decimal (e.g. "1,5 hrs" \u2192 "1.5 hrs")
        message_lower = re.sub(r'(\d),(\d)', r'\1.\2', message_lower)
        # SMS typo: "1 hout" → "1 hour" (must run before hour/half patterns)
        message_lower = re.sub(r"\bhout\b", "hour", message_lower)

        # Pattern: "N hour(s) and a half" OR "N and a half hours" (must come before plain hour patterns)
        _word_to_num = {"one": 1, "two": 2, "three": 3, "four": 4}
        half_match = re.search(
            r'(\d+|one|two|three|four)\s+(?:hours?\s+and\s+a\s+half|and\s+a\s+half\s+(?:hours?|hrs?))',
            message_lower
        )
        if half_match:
            raw = half_match.group(1) if half_match and half_match.lastindex and half_match.lastindex >= 1 else None
            n = _word_to_num.get(raw, None) if raw else None
            if n is None:
                try:
                    n = int(raw)
                except ValueError:
                    n = None
            if n is not None:
                return n * 60 + 30

        # Pattern: "X hours" or "X hr(s)" or "Xh" (word boundary on hr so "240" is not consumed as "24" + "0h")
        hour_patterns = [
            r'(\d+\.?\d*)\s*hours?',
            r'(\d+\.?\d*)\s*hrs?',
            r'(\d+\.?\d*)\s*hr\b',
            r'(\d+\.?\d*)h(?:\s|$)',
        ]

        for pattern in hour_patterns:
            match = re.search(pattern, message_lower)
            if match:
                try:
                    if match and match.lastindex and match.lastindex >= 1:
                        hours = float(match.group(1))
                        return int(hours * 60)
                except ValueError:
                    continue

        # Pattern: "X minutes" or "X mins"
        min_patterns = [
            r'(\d+)\s*minutes?',
            r'(\d+)\s*mins?',
        ]

        for pattern in min_patterns:
            match = re.search(pattern, message_lower)
            if match:
                try:
                    if match and match.lastindex and match.lastindex >= 1:
                        return int(match.group(1))
                except ValueError:
                    continue

        # Common durations mentioned directly
        duration_keywords = {
            "half hour": 30,
            "half an hour": 30,
            "30 min": 30,
            "30min": 30,
            "one hour": 60,
            "1 hour": 60,
            "1hr": 60,
            "hour": 60,  # Generic "hour" = 1 hour
            "1.5 hour": 90,
            "1.5hr": 90,
            "one and a half": 90,
            "1 and a half": 90,
            "1 hour and a half": 90,
            "one hour and a half": 90,
            "90 min": 90,
            "two hour": 120,
            "2 hour": 120,
            "2hr": 120,
            "two and a half": 150,
            "2 and a half": 150,
            "2.5 hour": 150,
            "three hour": 180,
            "3 hour": 180,
            "3hr": 180,
            "three and a half": 210,
            "3 and a half": 210,
            "four hour": 240,
            "4 hour": 240,
        }

        for keyword, duration in duration_keywords.items():
            if keyword in message_lower:
                return duration

        # Bare round numbers treated as durations (15, 30, 45, 60, 90, 120 …)
        # Only match when the number is NOT followed by an ordinal suffix (which would be a date)
        # and NOT followed by am/pm (which would be a time), and NOT adjacent to a colon.
        # Scan all candidates: the first number may be a street number (e.g. "240 St … 60").
        _BARE_DURATION_MULTIPLES = {15, 30, 45, 60, 90, 120, 150, 180, 240}
        _bare_re = re.compile(
            r'(?<![:/])\b(\d{1,3})\b(?!\s*(?:st|nd|rd|th|am|pm|:|hours?|hrs?|hr\b|mins?|minutes?))',
        )
        for bare in _bare_re.finditer(message_lower):
            try:
                num = int(bare.group(1)) if bare and bare.lastindex and bare.lastindex >= 1 else None
                if num is not None and num not in _BARE_DURATION_MULTIPLES:
                    continue
                if self._bare_number_looks_like_street_number(message_lower, bare.end()):
                    continue
                return num
            except ValueError:
                continue

        return None

    def _bare_number_looks_like_street_number(self, message_lower: str, after_digits: int) -> bool:
        """True when text after a bare number looks like '… Name Street/Ave/…' (address, not minutes)."""
        tail = message_lower[after_digits : after_digits + 120]
        return bool(
            re.match(
                r"\s+[a-z0-9][a-z0-9\-']*(?:\s+[a-z0-9][a-z0-9\-']*){0,4}\s+"
                r"(?:st|street|avenue|ave|road|rd|lane|ln|drive|dr|place|way|court|ct|"
                r"crescent|terrace|tce|close|parade|pde|boulevard|blvd|highway|hwy|circ|cct)\b",
                tail,
            )
        )

    def _parse_experience_type(self, message: str) -> str | None:
        """Parse experience type (DGFE/GFE/PSE/massage).

        Prefer an explicit target after \"switch to\" / \"change to\" so phrases like
        \"switch to PSE not GFE\" resolve to PSE (plain \\bgfe\\b would wrongly match \"not gfe\").
        """
        message_lower = (message or "").lower()

        explicit = re.search(
            r"\b(?:switch(?:\s+me)?\s+to|change(?:\s+it)?\s+to|want\s+)\s*(dgfe|gfe|pse|massage|pse\s*filming|filming)\b",
            message_lower,
        )
        if explicit:
            tok_raw = explicit.group(1).strip().lower() if explicit and explicit.lastindex and explicit.lastindex >= 1 else ''
            tok = tok_raw.upper() if tok_raw else ''
            if "filming" in tok_raw:
                return "pse_filming"
            if tok == "DGFE":
                return "DGFE"
            if tok == "GFE":
                return "GFE"
            if tok == "PSE":
                return "PSE"
            return "massage"

        scrubbed = message_lower
        scrubbed = re.sub(r"\bnot\s+(dgfe|gfe|pse|massage)\b", " ", scrubbed)
        scrubbed = re.sub(r"\binstead\s+of\s+(dgfe|gfe|pse|massage)\b", " ", scrubbed)

        if re.search(r"\bdgfe\b", scrubbed):
            return "DGFE"
        if re.search(r"\bgfe\b", scrubbed):
            return "GFE"
        if re.search(r"\bpse\b", scrubbed):
            if re.search(r"\bfilming\b", scrubbed):
                return "pse_filming"
            return "PSE"
        if re.search(r"\bfilming\b", scrubbed):
            return "pse_filming"
        if re.search(r"\bmassage\b", scrubbed):
            return "massage"

        return None

    def _parse_incall_outcall(self, message: str) -> str | None:
        """Parse incall/outcall preference."""
        message_lower = message.lower()

        # Outcall patterns
        outcall_patterns = [
            r'\boutcall\b', r'\bout.?call\b',
            r'\bmy (place|hotel|address|location|apartment|room|airbnb|unit|suite)\b',
            r'\bcome to (me|my)\b', r'\bcome over\b', r'\bvisit me\b',
            r'\bstaying at\b', r'\bi\'?m at\b', r'\bi am at\b',
            r'\bcan you come\b', r'\byou come to\b',
            r'\b(?:i\'?m\s+)?located\s+at\b', r'\bmy address is\b',
        ]

        for pattern in outcall_patterns:
            if re.search(pattern, message_lower):
                return "outcall"

        # Incall patterns
        incall_patterns = [
            r'\bincall\b', r'\bin.?call\b',
            r'\byour (place|hotel|address|location)\b',
            r'\bcome to you\b', r'\bvisit you\b'
        ]

        for pattern in incall_patterns:
            if re.search(pattern, message_lower):
                return "incall"

        return None

    def _parse_outcall_address(self, message: str) -> str | None:
        """Parse outcall address from message. Prefer location phrase (e.g. after 'located at') over full message for geocoding."""
        msg = (message or "").strip()
        message_lower = msg.lower()

        def _clean_candidate(candidate: str) -> str | None:
            phrase = (candidate or "").replace("\n", " ").strip()
            if not phrase:
                return None

            phrase = re.sub(
                r"^(?:i\s*(?:am|'m)\s+)?(?:located\s+at|staying\s+at|at)\s+",
                "",
                phrase,
                flags=re.IGNORECASE,
            )
            phrase = re.sub(
                r"^(?:my\s+)?(?:hotel|location|address)\s*[:\-]?\s*",
                "",
                phrase,
                flags=re.IGNORECASE,
            )
            phrase = re.sub(
                r"\bfor\s+(?:an?\s+|one\s+|two\s+|three\s+|\d+(?:\.\d+)?\s*)?(?:hours?|hrs?|mins?|minutes?)\b.*$",
                "",
                phrase,
                flags=re.IGNORECASE,
            )
            # Trailing "1 hour" / "2 hrs" without "for" (e.g. "9 Cantle St 1 hour")
            phrase = re.sub(
                r"\s+(?:\d+(?:\.\d+)?|one|two|three)\s*(?:hours?|hrs?|hr)\s*$",
                "",
                phrase,
                flags=re.IGNORECASE,
            )
            phrase = re.sub(
                r"\b(?:for\s+the\s+booking|for\s+booking|for\s+session)\b.*$",
                "",
                phrase,
                flags=re.IGNORECASE,
            )
            # Strip "and I/im/I'm ..." that introduces non-address sentence text
            # e.g. "6 Kintore Avenue, Prospect and im keen to book for 1,5 hrs"
            phrase = re.sub(
                r"\s+and\s+(?:i(?:'m|\s+am|\s+\w)|\bim\b).*$",
                "",
                phrase,
                flags=re.IGNORECASE,
            )
            # Also strip "and I'll/I will ..." tails that include booking details.
            phrase = re.sub(
                r"\s+and\s+i(?:'ll|\s+will|\s*ll)\b.*$",
                "",
                phrase,
                flags=re.IGNORECASE,
            )
            # Strip trailing booking details after "and", e.g.
            # "158 raglan ave sth plympton and 1 hour is fine"
            phrase = re.sub(
                r"\s+and\s+(?:(?:\d+(?:\.\d+)?)\s*(?:hours?|hrs?|hr|minutes?|mins?|min)\b.*|"
                r"(?:for\s+)?(?:gfe|dgfe|pse)\b.*|duration\b.*)$",
                "",
                phrase,
                flags=re.IGNORECASE,
            )
            phrase = re.sub(r"\s+", " ", phrase).strip(" .,:;!-")

            if re.fullmatch(r"(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\d{3,4})", phrase, re.IGNORECASE):
                return None
            if re.fullmatch(r"(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)", phrase, re.IGNORECASE):
                return None
            # Reject if candidate starts with a time expression \u2014 this means we matched
            # something like "at 7pm tonight to come to my place" instead of a real address
            if re.match(r'^\d{1,2}(?::\d{2})?\s*(?:am|pm)\b', phrase, re.IGNORECASE):
                return None
            # Reject time-of-day words that the "at ..." pattern can accidentally capture
            _time_words = {
                'midnight', 'midnite', 'noon', 'midday', 'tonight', 'tonite',
                'today', 'tomorrow', 'morning', 'afternoon', 'evening', 'night',
                'now', 'later', 'soon', 'asap',
            }
            if phrase.lower().rstrip('?!., ') in _time_words:
                return None

            return phrase if len(phrase) >= 3 else None

        # Extract location phrase after common patterns (better for geocoding than full message)
        location_after_patterns = [
            r"(?:my\s+)?address\s+is\s+(.+)",
            r"(?:i'?m\s+)?located\s+at\s+(.+?)(?:\.|,|$|\s+and\s+)",
            r"(?:i'?m\s+)?(?:at|staying\s+at)\s+([^.,]+?)(?:\.|,|$|\s+and\s+)",
            r"^(?:address|location)\s*[:\-]?\s+(.+?)(?:\.|,|$)",
            r"^(?:my\s+)?hotel\s*[:\-]?\s+(.+?)(?:\.|,|$)",
            r"at\s+([A-Za-z0-9\s]+(?:hotel|st|street|road|rd|ave|drive)\s*[^.,]*?)(?:\.|,|$)",
        ]
        for pattern in location_after_patterns:
            match = re.search(pattern, msg, re.IGNORECASE | re.DOTALL)
            if match:
                phrase = _clean_candidate(match.group(1)) if match and match.lastindex and match.lastindex >= 1 else None
                if phrase and len(phrase) <= 200:
                    return phrase

        # Common hotel chains - return extracted phrase if we have one, else full message
        hotels = [
            "hilton", "marriott", "hyatt", "intercontinental", "sheraton",
            "majestic", "stamford", "crown", "pullman", "novotel", "rydges", "sofitel",
            "westin", "four seasons", "voco", "ibis", "doubletree", "holiday inn",
            "hampton inn", "w hotel", "aloft", "radisson", "como", "langham",
            "renaissance", "courtyard", "mercure",
        ]
        for hotel in hotels:
            if hotel in message_lower:
                # Prefer phrase after "at" / "located at" containing this hotel
                for pat in [
                    r"(?:located\s+at|staying\s+at|at)\s+([^.,\n]*" + re.escape(hotel) + r"[^.,\n]*)",
                    r"(" + re.escape(hotel) + r"[^.,\n]*)",
                ]:
                    m = re.search(pat, msg, re.IGNORECASE)
                    if m:
                        phrase = _clean_candidate(m.group(1)) if m and m.lastindex and m.lastindex >= 1 else None
                        if phrase:
                            return phrase
                cleaned_msg = _clean_candidate(msg)
                if cleaned_msg:
                    return cleaned_msg

        venue_markers = [
            "hotel", "hotels", "suite", "suites", "apartment", "apartments",
            "quest", "oaks", "meriton", "mantra", "peppers", "soho", "embassy",
            "westin", "voco", "ibis", "doubletree", "hampton",
        ]
        cleaned_msg = _clean_candidate(msg)
        if cleaned_msg and any(marker in message_lower for marker in venue_markers):
            return cleaned_msg

        # Street address pattern (e.g. "123 Main Street" or "32 Waterman Terrace")
        address_pattern = (
            r'\d+\s+[A-Za-z][A-Za-z\s]+'
            r'(?:street|st|road|rd|avenue|ave|drive|dr|terrace|tce|crescent|cres|'
            r'close|cl|place|pl|court|ct|way|parade|pde|boulevard|blvd|lane|ln|'
            r'circuit|cct|grove|gve|rise|row|square|sq)\b[^.,]*'
        )
        match = re.search(address_pattern, message, re.IGNORECASE)
        if match:
            # Include suburb component (everything after matched street address up to end)
            full_address = message[match.start():].strip().rstrip(" .,:;!-")
            cleaned = _clean_candidate(full_address)
            if cleaned:
                return cleaned

        # Bare number+name pattern with suburb (e.g. "32 Waterman Terrace, Mitchell Park")
        bare_address_pattern = r'^\s*\d+\s+[A-Za-z][A-Za-z\s]+(?:,\s*[A-Za-z][A-Za-z\s]+)+\s*$'
        if re.match(bare_address_pattern, msg, re.IGNORECASE):
            return _clean_candidate(msg)

        # If message mentions "address" or "hotel" or "location", try to get phrase after "is" or ":" (not bare "at" to avoid time colons)
        if any(word in message_lower for word in ["address", "hotel", "location"]):
            m = re.search(r"(?:address|location|hotel)\s+(?:is|at|:)\s*([^.\n]+)", msg, re.IGNORECASE)
            if m:
                phrase = _clean_candidate(m.group(1)) if m and m.lastindex and m.lastindex >= 1 else None
                if phrase:
                    return phrase
            return _clean_candidate(msg)

        return None

    def get_missing_fields(self, current_fields: dict[str, Any]) -> list:
        """
        Get list of missing mandatory fields.

        Args:
            current_fields: Current booking fields

        Returns:
            List of missing field names
        """
        missing = []

        for field in self.config.MANDATORY_FIELDS:
            if not current_fields.get(field):
                missing.append(field)

        # Additional checks — dinner dates use restaurant as outcall destination after date/time is known
        if current_fields.get('incall_outcall') == 'outcall' and not current_fields.get('outcall_address'):
            if 'outcall_address' not in missing:
                from utils.dinner_date import is_dinner_date_booking

                if is_dinner_date_booking(current_fields):
                    if current_fields.get('date') and current_fields.get('time'):
                        missing.append('outcall_address')
                else:
                    missing.append('outcall_address')

        return missing

    def is_complete(self, current_fields: dict[str, Any]) -> bool:
        """Check if all mandatory fields are present."""
        return len(self.get_missing_fields(current_fields)) == 0
