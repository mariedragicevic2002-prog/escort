"""

Greeting templates - First contact messages.
Uses escort_name from config so the name can be changed in admin.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import re
from typing import Any

from templates.booking_collection_messages import (
    AVAILABLE_TONIGHT_INCALL,
    AVAILABLE_TONIGHT_OUTCALL,
    FULLY_BOOKED_TONIGHT_NOTICE,
    FIRST_CONTACT_INCALL_TEMPLATE,
    INCALL_TIME_QUESTION,
    OUTCALL_TIME_AND_ADDRESS_QUESTION,
    REQUESTED_TIME_NOT_AVAILABLE_INCALL,
    REQUESTED_TIME_NOT_AVAILABLE_OUTCALL,
    TIME_REQUESTED_AVAILABLE_OUTCALL,
    build_outcall_policy_line,
    build_webform_cta,
    format_slot_list_for_sms_current_availability,
    format_hi_yes_free_at_requested_time_fallback,
    format_hi_yes_time_available_short,
    get_availability_window_label,
    hi_name_spaced_lead,
)

import logging

logger = logging.getLogger("adella_chatbot.greetings")

_DEFAULT_PROFILE_URL = "(profile_url)"

_AU_STATE_RE = re.compile(r',?\s*(NSW|VIC|QLD|SA|WA|TAS|NT|ACT)(\s+\d{4})?\s*$', re.IGNORECASE)


def _get_outcall_pricing_defaults() -> tuple[int, int]:
    """Return outcall surcharge/deposit values with centralized defaults."""
    try:
        from core.rates_from_config import get_deposit_outcall, get_surcharge

        return int(get_surcharge()), int(get_deposit_outcall())
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        try:
            from core.rates_from_config import get_default_pricing

            defaults = get_default_pricing()
            return int(defaults.get("surcharge", 100)), int(defaults.get("deposit_outcall", 100))
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            return 100, 100

def _strip_state(addr: str) -> str:
    """Remove trailing Australian state abbreviation (and postcode) from an address."""
    return _AU_STATE_RE.sub('', addr).strip().rstrip(',').strip()


def _address_line_fragment(address: str | None, hotel_name: str | None) -> str:
    """Comma-prefixed address snippet for templates when a hotel name is present."""
    _addr = _strip_state((address or "").strip()) if isinstance(address, str) else ""
    if not _addr:
        return ""
    if hotel_name and hotel_name.strip():
        return f", {_addr}"
    return _addr


# Words that are not names when used after "I'm" / "im" (e.g. "im keen to book" -> don't use "Keen";
# "im staying at Intercontinental" -> don't use "Staying")
_IM_NOT_NAME_WORDS = frozenset({
    "hi", "hey", "hello", "hiya", "yo",
    "are", "is", "u",
    "how", "about", "what", "when", "where", "which", "who", "why",
    "thanks", "thank", "thx",
    "keen", "sorry", "good", "fine", "okay", "ok", "interested", "looking", "free",
    "available", "ready", "here", "back", "just", "sure", "curious", "new",
    "single", "tired", "busy", "sick", "happy", "glad", "excited", "me",
    "staying", "at", "the", "in", "coming", "going", "waiting", "located", "based",
    "booked", "booking", "calling", "texting", "messaging", "stuck", "running",
    "wanting", "hoping", "trying", "after", "not", "so", "very", "really",
    "well", "down", "up", "out", "over", "still", "also", "only",
    "thinking", "wondering", "asking", "enquiring", "inquiring",
    "yes", "yep", "yeah", "ya", "yee", "gfe", "pse", "dgfe", "i", "im",
    "meant",
    "no", "nope", "nah", "na", "nah", "cancel",
    "negative", "nada", "stop", "bye",
    "actually", "switch", "instead", "scratch",
    # Modal/auxiliary verbs — never a first name
    "can", "could", "will", "would", "may", "might", "shall", "should", "must",
    "was", "were", "did", "does", "had", "has", "have", "been", "got", "get",
    # Action/intent words that should never be treated as a first name
    "let", "lets", "do", "done", "doing", "did",
    "wanna", "gonna", "gotta",
    "want", "wanted", "wanting",
    "need", "needed", "needing",
    "like", "liked", "liking",
    "make", "making", "made",
    "book", "booked", "booking",
    "confirm", "confirmed", "confirming",
    "check", "checking", "checked",
    "change", "changed", "changing",
    "sort", "sorted", "sorting",
    "reschedule", "rescheduling",
    "prefer", "preferred", "preferring",
    # Agreement / response words — e.g. "Sounds good", "Perfect thanks", "Great see you then"
    "sounds", "perfect", "great", "cool", "nice", "sweet", "awesome",
    "brilliant", "excellent", "fantastic", "wonderful",
    # Demonstratives / determiners — e.g. "This Saturday", "Next week", "That time"
    "this", "next", "last", "that", "these", "those", "any", "some",
    # Number words — used for duration ("one hour", "two hrs"), never a name
    "one", "two", "three", "four", "five", "six",
    "seven", "eight", "nine", "ten", "half",
    # Other common sentence-starters that are never names
    "around", "from", "until", "before", "early", "late", "soon",
    "maybe", "perhaps", "probably", "possibly", "right", "already",
    "either", "both", "rather",
    # Time/day words that should never be treated as a first name
    "midday", "noon", "midnight", "tonight", "tonite", "today", "tomorrow",
    "morning", "afternoon", "evening", "night", "now", "asap",
    "am", "pm",
    # Day-of-week tokens (full and short forms) — never a first name
    "mon", "monday", "tue", "tues", "tuesday", "wed", "weds", "wednesday",
    "thu", "thur", "thurs", "thursday", "fri", "friday",
    "sat", "saturday", "sun", "sunday",
    "tomoz", "tmrw", "tmoro",
    # Hotel brand names — never person names (catches e.g. "Crown Metropol" as outcall address)
    "hilton", "marriott", "sheraton", "westin", "novotel", "sofitel", "mercure",
    "pullman", "rydges", "stamford", "langham", "doubletree", "radisson", "skycity",
    "majestic", "intercontinental", "metropol",
    # Venue descriptor words
    "hotel", "motel", "suites", "resort", "plaza",
    # MMF exploration tags/preferences (never client names)
    "bisexual", "heterosexual", "humiliation", "voyeurism",
})


def is_likely_not_a_name(word: str) -> bool:
    """Return True if word is a common non-name (e.g. staying, keen). Use to avoid storing as client name."""
    return (word or "").strip().lower() in _IM_NOT_NAME_WORDS


def is_valid_client_name(name: str) -> bool:
    """Return True when name looks like a real first/short full name, not a keyword/time word."""
    raw = (name or "").strip()
    if not raw:
        return False
    parts = [p for p in raw.split() if p]
    if len(parts) > 2:
        return False
    for part in parts:
        if not part.isalpha():
            return False
        if len(part) == 1:
            return False
        if _is_not_a_name(part):
            return False
    return True


def _normalize_for_name_check(word: str) -> str:
    """
    Normalize a word for non-name checks.

    Compresses repeated letters so variants like "keeen" become "keen",
    allowing _IM_NOT_NAME_WORDS to catch elongated words like "keeen", "soooorry", etc.
    """
    word = (word or "").strip().lower()
    if not word:
        return word
    normalized_chars = [word[0]]
    for ch in word[1:]:
        if ch != normalized_chars[-1]:
            normalized_chars.append(ch)
    return "".join(normalized_chars)


def _is_not_a_name(word: str) -> bool:
    """Check if word is a non-name by testing both original and normalized forms."""
    lower = (word or "").strip().lower()
    if lower in _IM_NOT_NAME_WORDS:
        return True
    norm = _normalize_for_name_check(lower)
    if norm in _IM_NOT_NAME_WORDS:
        return True
    return False


def build_outcall_policy_message(
    city: str,
    surcharge: int,
    deposit_outcall: int,
    webform_url: str,
    has_duration: bool = False,
) -> str:
    """
    Shown whenever a client mentions outcall or asks the escort to come to their place,
    regardless of where in the conversation it occurs.

    Always shows the 15km/city CBD policy + surcharge/deposit.
    Asks only for what's still missing:
      - Always asks for hotel name / address (this template is only called when address unknown)
      - Asks for duration only if not already provided (has_duration=False)

    Args:
        city:           Current city name from admin / touring (e.g. "Perth").
        surcharge:      Outcall surcharge amount (no $ sign).
        deposit_outcall: Outcall deposit amount (no $ sign).
        webform_url:    Secure personalised booking webform URL.
        has_duration:   True if the client has already given a duration \u2014 omit duration ask.

    Returns:
        SMS-ready string.
    """
    msg = build_outcall_policy_line(
        surcharge=surcharge,
        deposit_outcall=deposit_outcall,
        city=city,
    ) + "\n\n"

    if has_duration:
        msg += "Can you confirm your hotel name or address?"
    else:
        msg += (
            "Can you confirm your hotel name or your address, and the duration? "
            "(e.g. 1 hr — min 1 hr for all outcalls)"
        )

    msg += f"\n\nOr you can fill out this webform: {webform_url}"
    return msg


# Cache for AI name extraction — avoids repeated API calls for the same message text.
# Bounded to 500 entries to prevent unbounded memory growth.
_ai_name_cache: dict[str, str] = {}
_AI_NAME_CACHE_MAX = 500


def _ai_extract_name(message: str) -> str:
    """
    Ask Claude Haiku if the message contains the client's first name.

    Only called when all regex patterns in extract_client_name() fail.
    Uses a tiny Haiku call (max_tokens=10) so cost and latency are minimal.
    Returns '' on any error so callers always get a safe result.
    """
    cache_key = message.strip().lower()
    if cache_key in _ai_name_cache:
        return _ai_name_cache[cache_key]

    try:
        import anthropic
        from config import get_anthropic_api_key

        api_key = get_anthropic_api_key()
        if not api_key:
            return ""

        client = anthropic.Anthropic(api_key=api_key, timeout=5.0)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system=(
                "You extract a client's first name from a single SMS message sent to an escort booking service. "
                "Reply with ONLY the first name (e.g. 'James') if the person clearly states their name. "
                "Reply with 'none' if no name is present. Never guess or infer. Single word only."
            ),
            messages=[{"role": "user", "content": message}],
        )
        raw = (response.content[0].text or "").strip().lower().split()[0]
        result = "" if raw in ("none", "no", "null", "") else raw.capitalize()

        if result and not is_valid_client_name(result):
            result = ""

        if len(_ai_name_cache) >= _AI_NAME_CACHE_MAX:
            _ai_name_cache.clear()
        _ai_name_cache[cache_key] = result
        return result
    except Exception:
        return ""


def extract_client_name(message: str) -> str:
    """
    Extract client name from message.

    First tries fast regex patterns for explicit self-introductions, then
    falls back to a lightweight Claude Haiku call for ambiguous messages.

    Patterns (order matters: "its [Name]" before "im [Name]" so "hi its jacob im keen"
    yields Jacob, not Keen):
    - "It's [Name]" / "its [Name]"
    - "I'm [Name]" / "im [Name]" (skips non-names like keen, sorry, good)
    - "My name is [Name]"
    - "This is [Name]"
    - "[Name] here"
    - "[Name] yes/yep" (booking confirmation reply)
    - AI fallback (Claude Haiku) when all patterns fail

    Args:
        message: Client message

    Returns:
        Client name or empty string if not found
    """
    import re

    message_lower = message.lower()

    # Pattern 1: "it's [Name]" or "its [Name]" (try before "im" so "its jacob im keen" -> Jacob)
    match = re.search(r"(?:it's|its)\s+([a-z]+)", message_lower)
    if match:
        word = match.group(1)
        candidate = word.capitalize()
        if is_valid_client_name(candidate):
            return candidate

    # Pattern 2: "i'm [Name]" or "im [Name]" (skip if word is not a name, e.g. "im keen" / "im keeen")
    match = re.search(r"(?:i'm|\bim\b|i am)\s+([a-z]+)", message_lower)
    if match:
        word = match.group(1)
        candidate = word.capitalize()
        if is_valid_client_name(candidate):
            return candidate

    # Pattern 3: "my name is [Name]"
    match = re.search(r"my name(?:'s| is)\s+([a-z]+)", message_lower)
    if match:
        word = match.group(1)
        candidate = word.capitalize()
        if is_valid_client_name(candidate):
            return candidate

    # Pattern 4: "this is [Name]"
    match = re.search(r"this is\s+([a-z]+)", message_lower)
    if match:
        word = match.group(1)
        candidate = word.capitalize()
        if is_valid_client_name(candidate):
            return candidate

    # Pattern 5: "[Name] here"
    match = re.search(r"^([a-z]+)\s+here", message_lower)
    if match:
        word = match.group(1)
        candidate = word.capitalize()
        if is_valid_client_name(candidate):
            return candidate

    # Pattern 6: "[Name] yes" / "[Name] yep" (booking confirmation reply)
    match = re.search(r"^([a-z]+)\s+(?:yes|yep|yeah|ok|okay)\b", message_lower)
    if match:
        word = match.group(1)
        candidate = word.capitalize()
        if is_valid_client_name(candidate):
            return candidate

    # AI fallback — only reached when all regex patterns fail
    return _ai_extract_name(message)


def get_first_contact_message(city: str, hotel_name: str, location_description: str,
                               available_hours: str, profile_url: str,
                               booking_type: str = "incall", webform_url: str = "",
                               client_name: str = "", escort_name: str = None,
                               address: str = "",
                               persist_slots_for_phone: str | None = None,
                               persist_slots_state_manager: Any = None) -> str:
    """
    Get first contact message based on booking type.
    Uses custom_greeting from settings if set (admin "first response template");
    otherwise uses the built-in incall/outcall templates.

    Args:
        city: Current city
        hotel_name: Hotel name
        location_description: Location description (not used in new format)
        available_hours: Available hours (e.g. "3pm-3am, 7 days a week")
        profile_url: Profile URL
        booking_type: "incall" or "outcall" (defaults to "incall")
        webform_url: Secure webform URL with token
        client_name: Optional client name for personalization
        escort_name: Escort/business name (defaults to config)

    Returns:
        First contact message string
    """
    try:
        from config import get_escort_name as _get_escort_name
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        from core.settings_manager import get_escort_name as _get_escort_name
    if escort_name is None or (isinstance(escort_name, str) and not (escort_name or "").strip()):
        escort_name = _get_escort_name()
    escort_name = escort_name or _get_escort_name()
    name_str = f" {client_name}" if (client_name and isinstance(client_name, str)) else ""
    escort_possessive = "my"
    # Ensure all format values are strings to avoid TypeError/KeyError
    client_name_raw = (client_name or "").strip() if isinstance(client_name, str) else ""
    try:
        from core.rates_from_config import get_deposit_outcall, get_surcharge
        surcharge, deposit_outcall = get_surcharge(), get_deposit_outcall()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        surcharge, deposit_outcall = _get_outcall_pricing_defaults()
    address_line = ""  # Optional address suffix e.g. ", 123 Main St"; first-contact callers don't pass address

    # Generate available time slots for the greeting
    try:
        from utils.availability_slots import get_next_available_time_slots
        from utils.timezone import get_current_datetime
        _now = get_current_datetime()
        _slots = get_next_available_time_slots(
            _now,
            num_slots=3,
            check_calendar=True,
            persist_slots_for_phone=persist_slots_for_phone,
            persist_slots_state_manager=persist_slots_state_manager,
        )
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        _slots = []
    if _slots:
        slots_list_formatted = "\n" + "\n".join(f"\u2022 {slot_str}" for _, slot_str in _slots)
    else:
        slots_list_formatted = format_slot_list_for_sms_current_availability(None)

    _hotel = (hotel_name if hotel_name is not None else "") or ""
    _addr = address_line or ""
    _street = (address if address is not None else "") or ""
    if _hotel and _street:
        _staying_at = f" staying at {_hotel}, {_street}"
    elif _hotel:
        _staying_at = f" staying at {_hotel}"
    elif _street:
        _staying_at = f" staying at {_street}"
    elif _addr:
        _staying_at = f" staying at {_addr}"
    else:
        _staying_at = ""
    _loc_str = ""
    _city_s = (city if city is not None else "") or ""
    _street_has_city = _city_s and _city_s.lower() in _street.lower()
    if _hotel and _street and _city_s and not _street_has_city:
        _loc_str = f"{_hotel} {_street} {_city_s}"
    elif _hotel and _street:
        _loc_str = f"{_hotel} {_street}"
    elif _hotel and _city_s:
        _loc_str = f"{_hotel} {_city_s}"
    elif _hotel:
        _loc_str = _hotel
    elif _street:
        _loc_str = _street
    elif _addr:
        _loc_str = _addr
    location_block = f"\nI'm located at {_loc_str}\n\n" if _loc_str else "\n"
    common = dict(
        name=name_str,
        client_name=client_name_raw,
        city=(city if city is not None else "") or "",
        hotel_name=_hotel,
        address_line=_addr,
        staying_at=_staying_at,
        location_block=location_block,
        location_description=(location_description if location_description is not None else "") or "",
        booking_type=(booking_type if booking_type is not None else "") or "incall",
        available_hours=(available_hours if available_hours is not None else "") or "3pm-3am, 7 days a week",
        profile_url=(profile_url if profile_url is not None else "") or _DEFAULT_PROFILE_URL,
        webform_url=(webform_url if webform_url is not None else "") or "[Webform link]",
        escort_name=escort_name,
        escort_possessive=escort_possessive,
        surcharge=surcharge,
        deposit_outcall=deposit_outcall,
        slots_list=slots_list_formatted,
        incall_time_question=INCALL_TIME_QUESTION,
        request_different_time_cta=build_webform_cta(
            (webform_url if webform_url is not None else "") or "[Webform link]"
        ),
        or_fill_webform_cta=build_webform_cta(
            (webform_url if webform_url is not None else "") or "[Webform link]",
            prefix="Or fill in the booking webform",
        ),
        book_different_time_cta=build_webform_cta(
            (webform_url if webform_url is not None else "") or "[Webform link]",
            prefix="To book a different time fill in my booking webform:",
        ),
    )
    # Use admin-configured first response template if set
    try:
        from core.settings_manager import get_setting
        custom = (get_setting("custom_greeting") or "").strip()
        if custom:
            # Safe substitute only known placeholders so extra {x} in template don't break
            out = custom
            for key, value in common.items():
                out = out.replace("{" + key + "}", str(value))
            return out
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
    return FIRST_CONTACT_INCALL_TEMPLATE.format(**common)


def get_requested_time_not_available_message(
    requested_time_str: str,
    time_slots,
    city: str,
    hotel_name: str,
    client_name: str = "",
    is_outcall: bool = False,
    address: str = None,
    escort_name: str = None,
    webform_url: str = None,
    profile_url: str = None,
) -> str:
    """Build message when client's requested time is not available, offering 3 alternatives."""
    try:
        from config import get_escort_name as _gen
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        from core.settings_manager import get_escort_name as _gen
    if not (escort_name or "").strip():
        escort_name = _gen()
    hi_lead = hi_name_spaced_lead(client_name)
    address_line = _address_line_fragment(address, hotel_name)
    city_s = city or ""
    hotel_s = hotel_name or ""
    try:
        from core.rates_from_config import get_deposit_outcall, get_surcharge
        surcharge, deposit_outcall = get_surcharge(), get_deposit_outcall()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        surcharge, deposit_outcall = _get_outcall_pricing_defaults()
    if not profile_url:
        try:
            from config import get_profile_url
            profile_url = get_profile_url() or _DEFAULT_PROFILE_URL
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            profile_url = _DEFAULT_PROFILE_URL
    if not webform_url:
        try:
            from config import get_base_url
            webform_url = f"{get_base_url()}/booking"
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            from config import DEFAULT_BASE_URL
            webform_url = f"{DEFAULT_BASE_URL}/booking"
    slots_list = (
        "\n".join(f"\u2022 {slot[1]}" for slot in time_slots) if time_slots else "No slots available"
    )
    _raw_addr = address_line.lstrip(", ") if address_line else ""
    _addr_has_city = city_s and city_s.lower() in _raw_addr.lower()
    if hotel_s and _raw_addr and city_s and not _addr_has_city:
        _loc_str = f"{hotel_s} {_raw_addr} {city_s}"
    elif hotel_s and _raw_addr:
        _loc_str = f"{hotel_s} {_raw_addr}"
    elif hotel_s:
        _loc_str = hotel_s
    elif _raw_addr:
        _loc_str = _raw_addr
    else:
        _loc_str = ""
    location_block = f"\nI'm located at {_loc_str}\n\n" if _loc_str else "\n"
    location_line = f"I'm located at {_loc_str}" if _loc_str else ""
    outcall_policy_line = build_outcall_policy_line(
        surcharge=surcharge,
        deposit_outcall=deposit_outcall,
        city=city_s,
    )
    common = dict(
        hi_lead=hi_lead, city=city_s, requested_time=requested_time_str,
        hotel_name=hotel_s, address_line=address_line, location_block=location_block,
        location_line=location_line,
        surcharge=surcharge, deposit_outcall=deposit_outcall,
        outcall_policy_line=outcall_policy_line,
        outcall_time_question=OUTCALL_TIME_AND_ADDRESS_QUESTION,
        profile_url=profile_url, webform_url=webform_url,
        slots_list=slots_list,
        incall_time_question=INCALL_TIME_QUESTION,
        request_different_time_cta=build_webform_cta(webform_url),
    )
    if is_outcall:
        return REQUESTED_TIME_NOT_AVAILABLE_OUTCALL.format(**common)
    return REQUESTED_TIME_NOT_AVAILABLE_INCALL.format(**common)


def get_available_now_message(city: str, hotel_name: str,
                              client_name: str = "", is_outcall: bool = False,
                              address: str = None, escort_name: str = None,
                              has_duration: bool = False, webform_url: str = None,
                              profile_url: str = None, time_slots: list[tuple] | None = None,
                              fully_booked_tonight: bool = False,
                              **_kwargs) -> str:
    """Alias for get_available_now_3slot_message. has_duration and available_hours are ignored."""
    _persist_phone = (
        _kwargs.pop("persist_slots_for_phone", None)
        or _kwargs.pop("phone_number", None)
    )
    _persist_sm = (
        _kwargs.pop("persist_slots_state_manager", None)
        or _kwargs.pop("state_manager", None)
    )
    _booking_fields_for_slots = _kwargs.pop("booking_fields", None)
    return get_available_now_3slot_message(
        time_slots=time_slots,
        city=city,
        hotel_name=hotel_name,
        client_name=client_name,
        is_outcall=is_outcall,
        address=address,
        escort_name=escort_name,
        webform_url=webform_url,
        profile_url=profile_url,
        fully_booked_tonight=fully_booked_tonight,
        persist_slots_for_phone=_persist_phone,
        persist_slots_state_manager=_persist_sm,
        booking_fields_for_slots=_booking_fields_for_slots,
    )


def get_available_now_3slot_message(time_slots, city: str, hotel_name: str,
                                    client_name: str = "", is_outcall: bool = False,
                                    address: str = None, escort_name: str = None,
                                    webform_url: str = None, profile_url: str = None,
                                    fully_booked_tonight: bool = False,
                                    persist_slots_for_phone: str | None = None,
                                    persist_slots_state_manager: Any = None,
                                    booking_fields_for_slots: dict[str, Any] | None = None) -> str:
    """
    Build the "available now / soon / later" response showing up to 3 time slots.

    Args:
        time_slots: List of (datetime, formatted_string) tuples from
                    get_next_available_time_slots(). Pass None to auto-fetch.
        city / hotel_name / client_name / is_outcall / address: as usual.
        escort_name / webform_url / profile_url: as usual.
        fully_booked_tonight: When True, prepend that there is no availability left for the rest of this shift night
            before listing ``time_slots`` (typically the next unconstrained forward slots).

    Returns:
        Formatted message string listing all slots.
    """
    if time_slots is None:
        try:
            from utils.availability_slots import get_next_available_time_slots
            from utils.dinner_date import slot_kwargs_from_booking_state
            from utils.timezone import get_current_datetime
            _now = get_current_datetime()
            _slot_kw = slot_kwargs_from_booking_state(booking_fields_for_slots)
            time_slots = get_next_available_time_slots(
                _now,
                num_slots=3,
                check_calendar=True,
                persist_slots_for_phone=persist_slots_for_phone,
                persist_slots_state_manager=persist_slots_state_manager,
                **_slot_kw,
            )
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            time_slots = []
    try:
        from config import get_escort_name as _get_escort_name
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        from core.settings_manager import get_escort_name as _get_escort_name
    if not (escort_name or "").strip():
        escort_name = _get_escort_name()
    hi_lead = hi_name_spaced_lead(client_name)
    address_line = _address_line_fragment(address, hotel_name)
    escort_possessive = "my"
    city_s = city or ""
    hotel_s = hotel_name or ""
    try:
        from core.rates_from_config import get_deposit_outcall, get_surcharge
        surcharge, deposit_outcall = get_surcharge(), get_deposit_outcall()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        surcharge, deposit_outcall = _get_outcall_pricing_defaults()
    if not profile_url:
        try:
            from config import get_profile_url
            profile_url = get_profile_url() or _DEFAULT_PROFILE_URL
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            profile_url = _DEFAULT_PROFILE_URL
    if not webform_url:
        try:
            from config import get_base_url
            webform_url = f"{get_base_url()}/booking"
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            from config import DEFAULT_BASE_URL
            webform_url = f"{DEFAULT_BASE_URL}/booking"

    slots_list = (
        "\n".join(f"\u2022 {slot[1]}" for slot in time_slots) if time_slots else "No slots available"
    )
    availability_window = get_availability_window_label(time_slots)

    _raw_addr_3 = address_line.lstrip(", ") if address_line else ""
    _addr3_has_city = city_s and city_s.lower() in _raw_addr_3.lower()
    if hotel_s and _raw_addr_3 and city_s and not _addr3_has_city:
        _loc_str_3 = f"{hotel_s} {_raw_addr_3} {city_s}"
    elif hotel_s and _raw_addr_3:
        _loc_str_3 = f"{hotel_s} {_raw_addr_3}"
    elif hotel_s:
        _loc_str_3 = hotel_s
    elif _raw_addr_3:
        _loc_str_3 = _raw_addr_3
    else:
        _loc_str_3 = ""
    location_block_3 = f"\nI'm located at {_loc_str_3}\n\n" if _loc_str_3 else "\n"
    location_prompt_line = (
        f"I'm located at {_loc_str_3}\n\n{INCALL_TIME_QUESTION}"
        if _loc_str_3
        else INCALL_TIME_QUESTION
    )
    outcall_policy_line = build_outcall_policy_line(
        surcharge=surcharge,
        deposit_outcall=deposit_outcall,
        city=city_s,
    )
    common = dict(
        hi_lead=hi_lead,
        tonight_unavailable_notice=(FULLY_BOOKED_TONIGHT_NOTICE if fully_booked_tonight else ""),
        city=city_s,
        escort_name=escort_name, escort_possessive=escort_possessive,
        hotel_name=hotel_s, address_line=address_line,
        location_block=location_block_3,
        location_prompt_line=location_prompt_line,
        surcharge=surcharge, deposit_outcall=deposit_outcall,
        outcall_policy_line=outcall_policy_line,
        outcall_time_question=OUTCALL_TIME_AND_ADDRESS_QUESTION,
        profile_url=profile_url, webform_url=webform_url,
        slots_list=slots_list,
        availability_window=availability_window,
        request_different_time_cta=build_webform_cta(webform_url),
        or_fill_webform_cta=build_webform_cta(
            webform_url,
            prefix="Or fill in the booking webform",
        ),
    )
    if is_outcall:
        return AVAILABLE_TONIGHT_OUTCALL.format(**common)
    return AVAILABLE_TONIGHT_INCALL.format(**common)


def get_available_now_combined_message(
    time_slots,
    city: str,
    hotel_name: str,
    client_name: str = "",
    address: str = None,
    escort_name: str = None,
    webform_url: str = None,
    profile_url: str = None,
) -> str:
    """
    Build the 'available now' response showing soonest times for BOTH incall and outcall.
    Used when the client hasn't specified which type they want (ambiguous ASAP/right now request).
    All slots enforce the 30-minute minimum notice rule from the caller.
    """
    try:
        from config import get_escort_name as _get_escort_name
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        from core.settings_manager import get_escort_name as _get_escort_name
    if not (escort_name or "").strip():
        escort_name = _get_escort_name()

    try:
        from core.rates_from_config import get_deposit_outcall, get_surcharge
        surcharge, deposit_outcall = get_surcharge(), get_deposit_outcall()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        surcharge, deposit_outcall = _get_outcall_pricing_defaults()

    if not profile_url:
        try:
            from config import get_profile_url
            profile_url = get_profile_url() or _DEFAULT_PROFILE_URL
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            profile_url = _DEFAULT_PROFILE_URL

    if not webform_url:
        try:
            from config import get_base_url
            webform_url = f"{get_base_url()}/booking"
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            from config import DEFAULT_BASE_URL
            webform_url = f"{DEFAULT_BASE_URL}/booking"

    name_str = f" {client_name}" if (client_name and isinstance(client_name, str)) else ""

    slots_list = (
        "\n".join(f"\u2022 {slot[1]}" for slot in time_slots) if time_slots else "No slots available"
    )

    # Build incall location string
    _addr = _strip_state((address or "").strip()) if isinstance(address, str) else ""
    hotel_s = hotel_name or ""
    _city_combined = city or ""
    _addr_has_city_c = _city_combined and _city_combined.lower() in _addr.lower()
    if hotel_s and _addr and _city_combined and not _addr_has_city_c:
        _loc_str = f"{hotel_s} {_addr} {_city_combined}"
    elif hotel_s and _addr:
        _loc_str = f"{hotel_s} {_addr}"
    elif hotel_s:
        _loc_str = hotel_s
    elif _addr:
        _loc_str = _addr
    else:
        _loc_str = _city_combined

    incall_line = f"Incall \u2014 {_loc_str}" if _loc_str else "Incall"
    outcall_line = (
        f"Outcall \u2014 hotels & apartments only "
        f"(${surcharge} surcharge + ${deposit_outcall} deposit required)"
    )

    # Pick example time from first slot (last word of label, e.g. "7:30pm")
    _ex_time = "7:30pm"
    if time_slots:
        _parts = (time_slots[0][1] or "").split()
        if _parts:
            _ex_time = _parts[-1]

    or_fill_webform_cta = build_webform_cta(webform_url, prefix="Or fill in my booking webform")

    return (
        f"\u2705 Hi{name_str} here are my soonest available times:\n\n"
        f"{slots_list}\n\n"
        f"{incall_line}\n"
        f"{outcall_line}\n\n"
        f"{profile_url}\n\n"
        f'Reply with time + incall or outcall \u2014 e.g. "{_ex_time} incall"'
        f' or "{_ex_time} outcall + your address"\n\n'
        f"{or_fill_webform_cta}"
    )


def get_time_requested_available_message(requested_datetime, city: str, hotel_name: str,
                                         client_name: str = "", is_outcall: bool = False,
                                         address: str = None, escort_name: str = None,
                                         webform_url: str = None, profile_url: str = None) -> str:
    """
    Get "time requested and available" response.
    Used when client asks for a specific time (e.g., "at 8am") and that time IS available.
    
    Args:
        requested_datetime: datetime object of the requested time
        city: City name
        hotel_name: Hotel name
        client_name: Client's name (if detected)
        is_outcall: Whether this is an outcall request
        address: Address (for outcall)
        escort_name: Escort's name
        webform_url: URL for booking webform
        profile_url: URL to escort profile
    
    Returns:
        Formatted message string
    """
    try:
        from config import get_escort_name as _get_escort_name
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        from core.settings_manager import get_escort_name as _get_escort_name
    if escort_name is None or (isinstance(escort_name, str) and not (escort_name or "").strip()):
        escort_name = _get_escort_name()
    escort_name = escort_name or _get_escort_name()
    
    name_str = f" {client_name}" if (client_name and isinstance(client_name, str)) else ""
    address_line = _address_line_fragment(address, hotel_name)
    escort_possessive = "my"
    city_s = (city if city is not None else "") or ""
    hotel_s = (hotel_name if hotel_name is not None else "") or ""
    
    try:
        from core.rates_from_config import get_deposit_outcall, get_surcharge
        surcharge, deposit_outcall = get_surcharge(), get_deposit_outcall()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        surcharge, deposit_outcall = _get_outcall_pricing_defaults()
    
    if profile_url is None:
        try:
            from config import get_profile_url
            profile_url = get_profile_url() or _DEFAULT_PROFILE_URL
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            profile_url = _DEFAULT_PROFILE_URL
    
    if not webform_url:
        try:
            from config import get_base_url
            webform_url = f"{get_base_url()}/booking"
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            from config import DEFAULT_BASE_URL
            webform_url = f"{DEFAULT_BASE_URL}/booking"
    
    # Format the requested time; "Yes I'm free at … on …" line matches COLLECTING flow.
    hour = minute = 0
    req_date = None
    if hasattr(requested_datetime, 'strftime'):
        req_date = requested_datetime.date()
        hour = requested_datetime.hour
        minute = requested_datetime.minute
    
    _raw_addr_t = address_line.lstrip(", ") if address_line else ""
    _addrt_has_city = city_s and city_s.lower() in _raw_addr_t.lower()
    if hotel_s and _raw_addr_t and city_s and not _addrt_has_city:
        _loc_str_t = f"{hotel_s} {_raw_addr_t} {city_s}"
    elif hotel_s and _raw_addr_t:
        _loc_str_t = f"{hotel_s} {_raw_addr_t}"
    elif hotel_s:
        _loc_str_t = hotel_s
    elif _raw_addr_t:
        _loc_str_t = _raw_addr_t
    else:
        _loc_str_t = ""
    location_block_t = f"\nI'm located at {_loc_str_t}\n\n" if _loc_str_t else "\n"
    outcall_policy_line = build_outcall_policy_line(
        surcharge=surcharge,
        deposit_outcall=deposit_outcall,
        city=city_s,
    )
    book_different_time_cta = build_webform_cta(
        webform_url,
        prefix="To book a different time fill in my booking webform:",
    )

    if not is_outcall:
        try:
            from templates.field_prompts import _get_experience_url
            experience_url = _get_experience_url()
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            experience_url = "https://www.adella-allure.com.au/experience"
        free_line = (
            format_hi_yes_time_available_short(hour, minute, client_name)
            if req_date is not None
            else format_hi_yes_free_at_requested_time_fallback(client_name)
        )
        booking_questions = (
            'How long do you want to book for? - (e.g. "30 mins, 1 or 2 hours")\n\n'
            "What type of experience are you after? (GFE/DGFE/PSE)\n\n"
            f"{experience_url}\n\n"
            'Please reply with both — e.g. "1 hr PSE"'
        )
        # Profile URL and location sit between the free-line and the booking questions
        if _loc_str_t:
            profile_location_block = f"{profile_url}\n\nI'm located at {_loc_str_t}\n\n"
        else:
            profile_location_block = f"{profile_url}\n\n"
        return (
            f"{free_line}\n\n"
            f"{profile_location_block}"
            f"{booking_questions}\n\n"
            f"{book_different_time_cta}"
        )

    yes_free_line = (
        format_hi_yes_time_available_short(hour, minute, client_name)
        if req_date is not None
        else format_hi_yes_free_at_requested_time_fallback(client_name)
    )
    common = dict(
        name=name_str, city=city_s,
        escort_name=escort_name, escort_possessive=escort_possessive,
        hotel_name=hotel_s, address_line=address_line,
        location_block=location_block_t,
        surcharge=surcharge, deposit_outcall=deposit_outcall,
        outcall_policy_line=outcall_policy_line,
        profile_url=profile_url, webform_url=webform_url,
        yes_free_line=yes_free_line,
        book_different_time_cta=book_different_time_cta,
    )

    return TIME_REQUESTED_AVAILABLE_OUTCALL.format(**common)


def build_booking_time_unavailable_message(
    booking_fields: dict,
    requested_time_str: str,
    *,
    city: str = "",
    hotel_name: str = "",
    address: str = "",
    client_name: str = "",
    is_outcall: bool = False,
    escort_name: str | None = None,
    webform_url: str = "",
    profile_url: str = "",
    find_alternative_slots_kwargs: dict | None = None,
) -> tuple[str, None]:
    """
    SMS / COLLECTING: requested slot is busy on calendar — ❌ line + closest alternatives.

    booking_fields: dict passed to find_alternative_slots (expects date, time, duration, etc.).
    requested_time_str: human label for the busy time (e.g. "7:30pm").
    find_alternative_slots_kwargs: optional same_day_only, max_hours_from_requested, max_results, etc.
    """
    from services.calendar_service import find_alternative_slots

    kwargs = dict(find_alternative_slots_kwargs or {})
    max_results = int(kwargs.pop("max_results", 3))
    try:
        raw_alts = find_alternative_slots(booking_fields, max_results=max_results, **kwargs)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        raw_alts = []

    time_slots: list = []
    for dt in raw_alts or []:
        if hasattr(dt, "strftime"):
            time_slots.append((dt, format_slot_today_dd_month_at_time(dt)))
        else:
            time_slots.append((dt, str(dt)))

    msg = get_requested_time_not_available_message(
        requested_time_str=requested_time_str,
        time_slots=time_slots,
        city=city,
        hotel_name=hotel_name,
        client_name=client_name,
        is_outcall=is_outcall,
        address=address,
        escort_name=escort_name,
        webform_url=webform_url,
        profile_url=profile_url,
    )
    return msg, None


def format_slot_today_dd_month_at_time(dt) -> str:
    """
    Format a datetime as "Mon 04 March at 7:00pm" (3-letter weekday + date + time).
    Used for "slots open" messages when time isn't available.
    """
    if not hasattr(dt, "strftime"):
        return str(dt)
    from utils.availability_slots import weekday_abbrev_3

    weekday = weekday_abbrev_3(dt)
    date_str = dt.strftime("%d %B")  # 04 March (day + full month)
    hour, minute = dt.hour, dt.minute
    period = "am" if hour < 12 else "pm"
    if hour in (0, 12):
        display_hour = 12
    elif hour > 12:
        display_hour = hour - 12
    else:
        display_hour = hour
    time_str = f"{display_hour}:{minute:02d}{period}"
    return f"{weekday} {date_str} at {time_str}"


def get_slots_open_message(
    slot_datetimes: list,
    webform_url: str,
    client_name: str = None,
    ask_for_time_and_name: bool = False,
) -> str:
    """
    Build the "Unfortunately that time isn't available, but I have these slots open:"
    message with "Mon DD Month at 7:00pm" style lines and webform CTA.

    Args:
        slot_datetimes: List of datetime objects (e.g. from find_alternative_slots).
        webform_url: Full URL for the booking webform.
        client_name: If provided and ask_for_time_and_name True, we still ask for name when empty.
        ask_for_time_and_name: If True, closing asks to "respond with the TIME you want to book"
            and "your name if you haven't already", then webform for alternative.

    Returns:
        Full message string.
    """
    if not slot_datetimes:
        return (
            f"\u274C Unfortunately that time isn't available. Please suggest another day or use the booking webform ({webform_url})."
        )
    lines = [format_slot_today_dd_month_at_time(dt) for dt in slot_datetimes[:10]]
    times_str = "\n".join(f"\u2022 {line}" for line in lines)
    if ask_for_time_and_name:
        name_bit = " (and your name if you haven't already provided it)" if not (client_name and str(client_name).strip()) else ""
        closing = (
            f"To confirm your booking please respond back with the TIME you wanted to book{name_bit}.\n\n"
            f"If you wish to book an alternative time please fill out the booking webform: {webform_url}"
        )
    else:
        closing = (
            f"Let me know if any of these work for you. Or to make a booking please advise of TIME and Duration so I can check availability for you. Or use booking webform ({webform_url})"
        )
    return (
        f"\u274C Unfortunately that time isn't available, but I have these slots open:\n\n{times_str}\n\n{closing}"
    )


def format_time_simple(hour: int, minute: int) -> str:
    """
    Format time as simple 12-hour format (e.g., 4PM, 4:30PM).

    Args:
        hour: Hour (0-23)
        minute: Minute (0-59)

    Returns:
        Formatted time string
    """
    period = "PM" if hour >= 12 else "AM"
    display_hour = hour if hour <= 12 else hour - 12
    if display_hour == 0:
        display_hour = 12

    if minute == 0:
        return f"{display_hour}{period}"
    else:
        return f"{display_hour}:{minute:02d}{period}"

