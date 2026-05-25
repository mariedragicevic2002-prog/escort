# ruff: noqa: E402

from utils.log_sanitize import LOG_SUPPRESSED_FMT
"""
Booking collection message templates - COLLECTING state client-facing SMS strings.
"""

# Shared wording blocks so handlers/templates use one source of truth.

import logging
import re

logger = logging.getLogger("escort_chatbot.booking_collection_messages")


def hi_name_spaced_lead(client_name: str | None) -> str:
    """Between ``Hi`` and the following word — avoids ``Hi  here`` when the name is blank."""
    cn = (client_name or "").strip()
    return f" {cn} " if cn else " "


# Prepended to slot-list SMS when "tonight" search finds nothing left before shift end / cutoff.
FULLY_BOOKED_TONIGHT_NOTICE = (
    "I'm fully booked for the rest of tonight — there's no availability left before I wrap up.\n\n"
)


DURATION_AND_EXPERIENCE_QUESTION = "How long a booking are you after and what type of experience? (GFE / DGFE / PSE)"
EXPERIENCE_TYPE_QUESTION = "What type of experience are you after? (GFE / DGFE / PSE)"
REPLY_WITH_BOTH_EXAMPLE = "Please reply with both \u2014 e.g. \"1 hr PSE\""
REPLY_WITH_ONE_EXAMPLE = "Please reply with one \u2014 e.g. \"PSE\""
OUTCALL_MINIMUM_NOTE = "(Minimum 1 hour for outcalls)"

# Brands/lines commonly duplicated across AU states — only these get extra address + chain wording.
AU_MULTI_STATE_HOTEL_CHAIN_SUBSTRINGS: tuple[str, ...] = (
    "pan pacific",
    "pullman",
    "novotel",
    "sofitel",
    "mercure",
    "ibis",
    "mantra",
    "quest",
    "adina",
    "rendezvous",
    "holiday inn",
    "crowne plaza",
    "intercontinental",
    "westin",
    "sheraton",
    "marriott",
    "hilton",
    "hyatt",
    "doubletree",
    "double-tree",
    "four points",
    "ritz-carlton",
    "ritz carlton",
    "best western",
    "langham",
    "grand hyatt",
    "parkroyal",
    "park royal",
    "swissotel",
    "accor",
    "travelodge",
    "metro hotels",
)


def venue_is_au_multistate_hotel_chain(venue_label: str) -> bool:
    """True when the venue looks like a chain property commonly reused across Australian cities."""
    v = (venue_label or "").strip().lower()
    if not v:
        return False
    return any(chain.strip() in v for chain in AU_MULTI_STATE_HOTEL_CHAIN_SUBSTRINGS)


def _venue_label_is_city_or_region_only(label: str, booking_city: str = "") -> bool:
    """Geocoder sometimes returns 'Perth WA' instead of a hotel name — treat as non-venue."""
    la = re.sub(r"[\s,]+", " ", (label or "").strip().lower())
    if not la:
        return True
    bc = re.sub(r"[\s,]+", " ", (booking_city or "").strip().lower())
    if bc and la == bc:
        return True
    if bc and la.startswith(bc):
        tail = la[len(bc) :].strip()
        if tail in ("", "wa", "nsw", "vic", "qld", "sa", "tas", "act", "nt"):
            return True
        if tail in ("western australia", "new south wales", "victoria", "queensland", "south australia", "tasmania"):
            return True
    two = la.split()
    if bc:
        b0 = bc.split()[0]
        if len(two) == 2 and two[0] == b0 and two[1] in ("wa", "nsw", "vic", "qld", "sa", "tas", "act", "nt"):
            return True
    return False


def append_outcall_duration_minimum_if_needed(text: str, is_outcall: bool) -> str:
    """Append one-line minimum-duration reminder when asking duration on outcall; avoid duplicates."""
    if not is_outcall or not (text or "").strip():
        return text
    tl = text.lower()
    if "minimum" in tl and ("1 hour" in tl or "1 hr" in tl or "one hour" in tl):
        return text
    return f"{text.rstrip()}\n\n{OUTCALL_MINIMUM_NOTE}"


_SERVICE_STYLE_EXP_RE = re.compile(r"\b(?:gfe|dgfe|pse)\b", re.IGNORECASE)


def service_style_experience_is_set(experience_type: str | None) -> bool:
    """True when the client named standard service style GFE, DGFE, or PSE."""
    return bool(_SERVICE_STYLE_EXP_RE.search(str(experience_type or "")))


def special_booking_skip_gfe_style_prompt(fields: dict | None) -> bool:
    """
    Doubles, couples, dinner date, etc. — booking kind already implies experience;
    do not push the separate GFE/DGFE/PSE trio + /experience URL.
    """
    if not fields:
        return False
    from utils.dinner_date import is_dinner_date_booking

    if is_dinner_date_booking(fields):
        return True
    bt = str(fields.get("booking_type") or "").strip().lower()
    if bt in ("doubles_mff", "couples_booking"):
        return True
    et = str(fields.get("experience_type") or "").strip().lower()
    if et in ("Doubles MMF", "doubles_mff", "couples_mff"):
        return True
    if et.startswith("doubles_") or et.startswith("couples_"):
        return True
    dt = str(fields.get("doubles_type") or "").strip().lower()
    if dt in ("mmf", "mff"):
        return True
    bs = str(fields.get("booking_status") or "").strip().lower()
    if bs and "doubles" in bs:
        return True
    return False


def experience_already_set_for_gfe_prompt(fields: dict | None) -> bool:
    """
    When True, COLLECTING prompts should not ask for GFE/DGFE/PSE (already chosen
    or superseded by a special booking flow).
    """
    if not fields:
        return False
    if service_style_experience_is_set(fields.get("experience_type")):
        return True
    return special_booking_skip_gfe_style_prompt(fields)


OUTCALL_TIME_AND_ADDRESS_QUESTION = "What time suits you and what's your address?"
INCALL_TIME_QUESTION = "Which time works for you?"
INCALL_TIME_SUITS_QUESTION = "Which time suits you and how long did you want to book for?"
NO_SLOTS_CONTACT_FALLBACK = "\n- Please contact for availability"
NO_SLOTS_CURRENT_AVAILABILITY_FALLBACK = "\n- Please contact for current availability"
MOOD_EXPERIENCE_PROMPT = "What are you in the mood for - GFE,DGFE or PSE?"
LOCATION_CHOICE_PROMPT = "My place or yours? (LOCATION)"
ADDRESS_LOCATION_PROMPT = "Where should I come to? Give me your LOCATION..."
SHORT_LOCATION_CHOICE_PROMPT = "My place or yours?"
SHORT_ADDRESS_PROMPT = "Where should I come to?"
INCALL_TIME_BEST_QUESTION = "What time works best for you?"


def build_webform_cta(webform_url: str, prefix: str = "To request a different time fill in the booking webform") -> str:
    """Standard webform call-to-action line."""
    return f"{prefix} {webform_url}"


def format_slot_list_for_sms(time_slots) -> str:
    """Format slot tuples for SMS; fallback to contact line when empty."""
    if time_slots:
        return "\n" + "\n".join(f"\u2022 {slot_str}" for _, slot_str in time_slots)
    return NO_SLOTS_CONTACT_FALLBACK


def format_slot_list_for_sms_current_availability(time_slots) -> str:
    """Format slot tuples with current-availability fallback wording."""
    if time_slots:
        return "\n" + "\n".join(f"\u2022 {slot_str}" for _, slot_str in time_slots)
    return NO_SLOTS_CURRENT_AVAILABILITY_FALLBACK


def get_availability_window_label(time_slots, now=None) -> str:
    """Pick a natural availability label from the offered slots."""
    if not time_slots:
        return "today"
    if now is None:
        from utils.timezone import get_current_datetime
        now = get_current_datetime()

    slot_datetimes = [dt for dt, _ in time_slots if hasattr(dt, "date") and hasattr(dt, "hour")]
    if not slot_datetimes:
        return "today"

    first_slot_date = min(dt.date() for dt in slot_datetimes)
    if first_slot_date != now.date():
        return "soon"

    today_hours = [dt.hour for dt in slot_datetimes if dt.date() == now.date()]
    if not today_hours:
        return "today"
    if max(today_hours) < 12:
        return "this morning"
    if min(today_hours) >= 18:
        return "tonight"
    return "today"


# First-contact / availability templates centralized here for reuse across handlers.
FIRST_CONTACT_INCALL_TEMPLATE = f"""Hi{{name}} if you want to make a booking I'm available at these times:
{{slots_list}}

{{profile_url}}
{{location_block}}
{INCALL_TIME_SUITS_QUESTION}

Or if these times don't suit fill out this webform: {{webform_url}}"""

REQUESTED_TIME_NOT_AVAILABLE_OUTCALL = """Hi{hi_lead}❌ Unfortunately {requested_time} isn't available.

Here are my closest available times:

{slots_list}

{outcall_policy_line}

{outcall_time_question}

{request_different_time_cta}"""

REQUESTED_TIME_NOT_AVAILABLE_INCALL = """Hi{hi_lead}❌ Unfortunately {requested_time} isn't available.

Here are my closest available times:

{slots_list}

{profile_url}

{location_line}

Which time works for you and how long did you want to book for?

Please reply with both - (e.g. "1st time available 1hr")

{request_different_time_cta}"""

AVAILABLE_TONIGHT_INCALL = """Hi{hi_lead}{tonight_unavailable_notice}here are the times I have available {availability_window}:

{slots_list}

{profile_url}

{location_prompt_line}

{or_fill_webform_cta}"""

AVAILABLE_TONIGHT_OUTCALL = """Hi{hi_lead}{tonight_unavailable_notice}here are the times I have available {availability_window}:

{slots_list}

{profile_url}

{outcall_policy_line}

{outcall_time_question}

{request_different_time_cta}"""

# Incall "requested time is free" copy is built in greetings.get_time_requested_available_message
# via build_incall_duration_experience_prompt_after_time_free (same wording as COLLECTING flow).

TIME_REQUESTED_AVAILABLE_OUTCALL = """{yes_free_line}

{profile_url}

{outcall_policy_line}

What's your address and how long would you like to book for? (Minimum 1 hour for outcalls)

Please reply with both — e.g. "Hilton Adelaide 1 hr"

{book_different_time_cta}"""

# Ask client how long a booking they want + experience type, after a time slot has been reserved
ASK_DURATION_FOR_SLOT = (
    "Hi{name_part} before I reserve {time_str} for you can you let me know how long a booking you were after "
    "and what type of experience? (GFE / DGFE / PSE)\n\n"
    "Please reply with both \u2014 e.g. \"1 hr PSE\""
)

# Nudge client when no structured prompt could be built (only core fields missing)
ASK_TIME_AND_DURATION_NUDGE = (
    "What time, duration and experience type were you thinking? (eg. 9pm for 1 hr PSE)\n\n"
    "Or you can fill out this webform: {webform_url}"
)

# Re-exported from deposit_flow_messages \u2014 single source for "client backed out, invite to rebook"
from templates.deposit_flow_messages import BOOKING_CANCELLED_NO_PROBLEM  # noqa: F401


def build_slot_reservation_prompt(
    time_str: str,
    client_name: str = "",
    experience_already_set: bool = False,
    is_outcall: bool = False,
) -> str:
    """Prompt after a slot is selected, asking only for fields still missing."""
    name_part = f" {client_name.strip()}" if client_name and client_name.strip() else ""
    if experience_already_set:
        msg = (
            f"Hi{name_part} before I reserve {time_str} for you can you let me know how long a booking you were after?\n\n"
            "Please reply with duration only — e.g. \"1 hr\""
        )
        return append_outcall_duration_minimum_if_needed(msg, is_outcall)
    try:
        from config import get_base_url
        exp_url = f"{get_base_url()}/experience"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        exp_url = "https://www.adella-allure.com.au/experience"
    msg = (
        f"Hi{name_part} before I reserve {time_str} for you can you let me know how long a booking you were after "
        f"and what type of experience? (GFE / DGFE / PSE)\n\n"
        f"{exp_url}\n\n"
        f"Please reply with both \u2014 e.g. \"1 hr PSE\""
    )
    return append_outcall_duration_minimum_if_needed(msg, is_outcall)


def build_time_available_prompt(
    time_str: str,
    client_name: str = "",
    experience_already_set: bool = False,
    is_outcall: bool = False,
) -> str:
    """Prompt after time-availability confirmation, asking only for fields still missing."""
    if experience_already_set:
        name_part = f" {client_name.strip()}" if client_name and client_name.strip() else ""
        msg = (
            f"✅ Hi{name_part} Your time of {time_str} is available!\n\n"
            "How long would you like to book for?\n\n"
            "Please reply with duration only — e.g. \"1 hr\""
        )
        return append_outcall_duration_minimum_if_needed(msg, is_outcall)
    try:
        from config import get_base_url
        exp_url = f"{get_base_url()}/experience"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        exp_url = "https://www.adella-allure.com.au/experience"
    name_part = f" {client_name.strip()}" if client_name and client_name.strip() else ""
    msg = (
        f"✅ Hi{name_part} Your time of {time_str} is available!\n\n"
        f"{DURATION_AND_EXPERIENCE_QUESTION}\n\n"
        f"{exp_url}\n\n"
        f"{REPLY_WITH_BOTH_EXAMPLE}"
    )
    return append_outcall_duration_minimum_if_needed(msg, is_outcall)


def build_requested_time_followup_prompt(
    available_line: str,
    is_outcall: bool = False,
    experience_already_set: bool = False,
) -> str:
    """Prompt after requested time availability check in COLLECTING flow.

    Callers must use build_incall_preconfirm_summary when duration is already known
    so experience type is never asked standalone. This function only handles the
    duration-unknown case.
    """
    try:
        from config import get_base_url
        exp_url = f"{get_base_url()}/experience"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        exp_url = "https://www.adella-allure.com.au/experience"
    if experience_already_set:
        msg = (
            f"{available_line}"
            "How long would you like to book for?\n\n"
            "Please reply with duration only — e.g. \"1 hr\""
        )
    else:
        msg = (
            f"{available_line}"
            "How long would you like to book for and what type of experience are you after? "
            f"(GFE / DGFE / PSE)\n\n"
            f"{exp_url}\n\n"
            f"{REPLY_WITH_BOTH_EXAMPLE}"
        )
    return append_outcall_duration_minimum_if_needed(msg, is_outcall)


def _day_ordinal_suffix(day: int) -> str:
    from utils.time_formatting import get_day_ordinal_suffix
    return get_day_ordinal_suffix(day)


def format_yes_im_free_at_line(booking_date, time_val) -> str:
    """
    One line: ✅ Yes I'm free at 4pm on Monday 6th April
    booking_date: datetime.date or datetime
    time_val: time tuple (h, m), datetime.time, or (h, m) ints
    """
    import datetime as _dt

    from utils.time_formatting import format_time_12h

    if hasattr(booking_date, "date"):
        d = booking_date.date()
    else:
        d = booking_date

    if isinstance(time_val, _dt.time):
        hour, minute = time_val.hour, time_val.minute
    elif isinstance(time_val, (tuple, list)) and len(time_val) >= 2:
        hour, minute = int(time_val[0]), int(time_val[1])
    else:
        hour, minute = 12, 0

    tstr = format_time_12h(hour, minute)

    weekday = d.strftime("%A")
    month = d.strftime("%B")
    day_num = d.day
    suf = _day_ordinal_suffix(day_num)
    return f"✅ Yes I'm free at {tstr} on {weekday} {day_num}{suf} {month}"


def format_yes_time_available_short(hour: int, minute: int = 0) -> str:
    """Single line for first-contact / experience flow: ✅ Yes 12:30pm is available"""
    from utils.time_formatting import format_time_12h

    tstr = format_time_12h(hour, minute)
    return f"✅ Yes {tstr} is available"


def format_hi_yes_time_available_short(hour: int, minute: int = 0, client_name: str | None = "") -> str:
    """COLLECTING opener when a requested clock time is free: Hi {name} ✅ Yes {time} is available."""
    return f"Hi{hi_name_spaced_lead(client_name)}{format_yes_time_available_short(hour, minute)}"


def format_hi_yes_free_at_requested_time_fallback(client_name: str | None = "") -> str:
    """Same opener when the slot is free but we could not resolve hour/minute for wording."""
    return f"Hi{hi_name_spaced_lead(client_name)}✅ Yes I'm free at the requested time"


def format_requested_time_unavailable_line(hour: int, minute: int = 0) -> str:
    """Opening line when a specific requested clock time is busy."""
    from utils.time_formatting import format_time_12h

    tstr = format_time_12h(hour, minute)
    return f"❌ Unfortunately {tstr} isn't available"


def build_incall_duration_experience_prompt_after_time_free(
    experience_url: str,
    booking_date,
    time_val,
    experience_already_set: bool = False,
    is_outcall: bool = False,
) -> str:
    """
    After calendar confirms the slot is free: confirmation line + duration + experience + URL + reply-with-both.

    For outcalls, duration examples omit sub-hour suggestions (minimum 1 hour).
    """
    line1 = format_yes_im_free_at_line(booking_date, time_val)
    dur_line = (
        'How long do you want to book for? — e.g. "1 hr", "90 mins", or "2 hours"\n\n'
        f"{OUTCALL_MINIMUM_NOTE}"
        if is_outcall
        else 'How long do you want to book for? - (e.g. "30 mins, 1 or 2 hours")'
    )
    if experience_already_set:
        return f"{line1}\n\n{dur_line}"
    return (
        f"{line1}\n\n"
        f"{dur_line}\n\n"
        "What type of experience are you after? (GFE/DGFE/PSE)\n\n"
        f"{experience_url}\n\n"
        'Please reply with both — e.g. "1 hr PSE"'
    )


def format_outcall_location_check_ack(
    *,
    city: str = "",
    venue_name: str = "",
    verified_address: str = "",
) -> str:
    """
    Opening line after outcall address validates — city + property name.

    Extra verification line (resolved street address + chain note) only when the
    venue matches :func:`venue_is_au_multistate_hotel_chain`.
    """
    city = (city or "").strip()
    venue = (venue_name or "").strip()
    verified_address = (verified_address or "").strip()
    if venue and city:
        lines = [f"Just checking you're in {city} at {venue}?"]
        if verified_address and venue_is_au_multistate_hotel_chain(venue):
            lines.append(
                f"I have you at {verified_address.strip()} — some chains use the same name "
                "in several cities, so I double-check I've got the right property."
            )
        return "\n\n".join(lines) + "\n\n"
    if verified_address:
        return f"I found your location at {verified_address.strip()} — is that right?\n\n"
    return ""


def pick_outcall_venue_display_name(
    verified_info: dict | None,
    client_typed_address: str = "",
    *,
    booking_city: str = "",
) -> str:
    """
    Label for SMS copy when confirming city + venue: prefer client/hotel wording over a
    street-number-first geocoder line (which reads oddly as a 'hotel name').
    """
    client_typed_address = (client_typed_address or "").strip()
    if not verified_info:
        return client_typed_address
    bcity = (booking_city or verified_info.get("city") or "").strip()
    vh = (verified_info.get("verified_hotel_name") or "").strip()
    orig = (verified_info.get("original_address") or client_typed_address or "").strip()
    if _venue_label_is_city_or_region_only(vh, bcity):
        vh = ""
    elif vh and re.match(r"^\d+\s", vh):
        return orig if orig else vh
    if vh:
        return vh
    return orig


def build_wrong_city_outcall_abort_message(
    *,
    client_name: str,
    escort_city: str,
    claimed_city: str,
) -> str:
    """Client confirms they're in a different city than the escort's Location — can't proceed."""
    name = (client_name or "").strip()
    ec = (escort_city or "").strip() or "my city"
    cc = (claimed_city or "").strip() or "your city"
    lead = f"Sorry {name}, " if name else "Sorry, "
    return (
        f"{lead}I'm in {ec} right now — we won't be able to continue with this booking.\n\n"
        f"If you'd like a text when I'm next touring {cc}, reply with the word TOURING "
        "and I'll add you to my notification list."
    )


def build_verified_address_prompt(
    verified_address: str = "",
    *,
    city: str = "",
    venue_name: str = "",
) -> str:
    """Prompt after outcall address has been validated."""
    from config import get_base_url
    exp_url = f"{get_base_url()}/experience"
    addr_line = format_outcall_location_check_ack(
        city=city,
        venue_name=venue_name,
        verified_address=verified_address,
    )
    return (
        f"{addr_line}"
        'How long do you want to book for? — e.g. "1 hr", "90 mins", or "2 hours"\n\n'
        f"{OUTCALL_MINIMUM_NOTE}\n\n"
        "What type of experience are you after? (GFE / DGFE / PSE)\n\n"
        f"{exp_url}\n\n"
        'Please reply with both \u2014 (e.g. "1 hr PSE")'
    )


def build_experience_followup_suffix() -> str:
    """Shared suffix when asking for missing experience details."""
    try:
        from config import get_base_url
        exp_url = f"{get_base_url()}/experience"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        exp_url = "https://www.adella-allure.com.au/experience"
    return f"\n\n{EXPERIENCE_TYPE_QUESTION}\n\n{exp_url}\n\n{REPLY_WITH_BOTH_EXAMPLE}"


def build_outcall_policy_line(
    surcharge: int,
    deposit_outcall: int,
    city: str = "",
    location_name: str = "",
) -> str:
    """Shared outcall policy/disclaimer line.

    Never discloses the escort's street address. ``location_name`` is ignored (legacy).
    """
    _ = location_name  # do not reveal street / venue name as "current location"
    try:
        from config import get_cbd_label_for_messages

        cbd_phrase = get_cbd_label_for_messages(city)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        cbd_phrase = (city or "").strip() + " CBD" if (city or "").strip() else "the CBD where I'm based"
    if surcharge <= 0:
        return (
            f"I only do outcalls to hotels or apartments within 15km of {cbd_phrase}. "
            f"There is a ${deposit_outcall} deposit required."
        )
    return (
        f"I only do outcalls to hotels or apartments within 15km of {cbd_phrase}. "
        f"There is a ${surcharge} surcharge + ${deposit_outcall} deposit required."
    )


def build_outcall_slots_message(
    heading: str,
    name_str: str,
    time_slots_formatted: str,
    profile_url: str,
    policy_line: str,
    webform_url: str,
    *,
    tonight_unavailable_notice: str = "",
) -> str:
    """Shared outcall slot-list message body used by NEW outcall flows."""
    cta = build_webform_cta(
        webform_url,
        prefix="To book a different time fill in my booking webform:",
    )
    _lead = (tonight_unavailable_notice or "").rstrip()
    _lead_blk = f"{_lead}\n\n" if _lead else ""
    return (
        f"✅ Hi{name_str} {_lead_blk}{heading}:\n{time_slots_formatted}\n\n"
        f"{profile_url}\n\n"
        f"{policy_line}\n\n"
        f"{OUTCALL_TIME_AND_ADDRESS_QUESTION}\n\n"
        f"{cta}"
    )


def message_looks_like_duration_attempt(message: str) -> bool:
    """True when the client likely included a duration (e.g. '1 hr', '1 hout') even if parsing failed."""
    if not (message or "").strip():
        return False
    m = message.lower()
    if re.search(r"\b\d+\s*(?:hout|hours?|hrs?|hr)\b", m):
        return True
    if re.search(r"\b\d+\s*m(?:ins?|inutes?)\b", m):
        return True
    # "1 h" at end of message (avoid matching unrelated words)
    if re.search(r"\b\d+\s+h\b(?!\w)", m):
        return True
    return False


def build_outcall_address_confirmed_message(
    client_name: str = "",
    verified_address: str = "",
    ask_experience: bool = True,
    acknowledge_unparsed_duration: bool = False,
    *,
    city: str = "",
    venue_name: str = "",
) -> str:
    """Standard message sent after confirming a client's outcall address is within range."""
    from config import get_base_url

    exp_url = f"{get_base_url()}/experience"
    _n = (client_name or "").strip()
    lead = f"Hi {_n}, " if _n else ""
    addr_line = format_outcall_location_check_ack(
        city=city,
        venue_name=venue_name,
        verified_address=verified_address,
    )
    _dur_line = (
        'I didn\'t quite catch the booking length from your last message — how long would you like to book for? '
        '— e.g. "1 hr", "90 mins", or "2 hours"\n\n'
        f"{OUTCALL_MINIMUM_NOTE}\n\n"
        if acknowledge_unparsed_duration
        else (
            'Please let me know how long you would like to book for — e.g. "1 hr", "90 mins", or "2 hours"\n\n'
            f"{OUTCALL_MINIMUM_NOTE}\n\n"
        )
    )
    if ask_experience:
        return (
            f"{lead}"
            f"{addr_line}"
            f"{_dur_line}"
            "What type of experience are you after? (GFE / DGFE / PSE)\n\n"
            f"{exp_url}\n\n"
            'Please reply with both \u2014 (e.g. "1 hr PSE")'
        )
    return (
        f"{lead}"
        f"{addr_line}"
        f"{_dur_line}"
    )
