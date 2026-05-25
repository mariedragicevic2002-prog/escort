"""

Utility templates for various system messages.
Includes link resend, cancellation, location updates, and other utility messages.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


from core.deposit_upload_tokens import generate_deposit_upload_token
from core.webform_security import get_webform_url
from templates.booking_collection_messages import (
    INCALL_TIME_BEST_QUESTION,
    INCALL_TIME_SUITS_QUESTION,
    OUTCALL_TIME_AND_ADDRESS_QUESTION,
    build_outcall_policy_line,
)


import logging
logger = logging.getLogger("adella_chatbot.utility_templates")


def _format_incall_location_line(venue_name: str, street_address: str, city: str) -> str:
    """
    Single SMS line for incall location: venue + street, city in parentheses.

    Avoids duplicating the venue when callers pass the same text as both venue and street.
    """
    v = (venue_name or "").strip()
    s = (street_address or "").strip()
    c = (city or "").strip()
    parts: list[str] = []
    if v and (not s or v.lower() not in s.lower()):
        parts.append(v)
    if s:
        parts.append(s)
    core = " ".join(parts).strip()
    if not core:
        return ""
    c_low = c.lower()
    core_low = core.lower()
    if c:
        # Avoid "… Sydney (Sydney)" when venue or address already names the city.
        tokens = [t.strip(".,;") for t in c_low.split() if t.strip(".,;")]
        if tokens and all(tok in core_low for tok in tokens):
            return f"I'm located at {core}"
        if c_low in core_low:
            return f"I'm located at {core}"
        return f"I'm located at {core} ({c})"
    return f"I'm located at {core}"


def get_outside_available_hours_message(
    city: str,
    address: str,
    available_hours: str,
    available_days: str,
    profile_url: str,
    webform_url: str,
    client_name: str = "",
    time_slots=None,
    is_outcall: bool = False,
    requested_booking_time: tuple[int, int] | None = None,
    venue_name: str = "",
    *,
    suppress_time_specific_opener: bool = False,
) -> str:
    """
    When the requested clock time is outside configured working hours/days.

    Opens with a named greeting, decline marker, and plain-language explanation,
    then configured hours, suggested slots (when present), profile link, incall
    location (venue + address + optional city), follow-up question, and webform.

    Set ``suppress_time_specific_opener`` when ``requested_booking_time`` is only
    a synthetic value for boundary checks (e.g. available-now flows) — the opener
    will not imply the client asked for that clock time.
    """
    hours_stripped = (available_hours or "").strip().replace("\n", " ")
    days_stripped = (available_days or "").strip().replace("\n", " ")
    if hours_stripped and ("," in hours_stripped or "day" in hours_stripped.lower() or "week" in hours_stripped.lower()):
        hours_display = hours_stripped
    elif days_stripped:
        hours_display = f"{hours_stripped}, {days_stripped}".strip(", ") if hours_stripped else days_stripped
    else:
        hours_display = hours_stripped or "3pm-3am, 7 days a week"

    name_part = f" {client_name}" if client_name else ""
    _hours_line = f"My available hours are {hours_display}"

    _emoji = "\u274c"
    if suppress_time_specific_opener:
        msg = (
            f"Hi{name_part} {_emoji} Unfortunately I'm currently not available.\n\n"
            f"{_hours_line}\n\n"
        )
    elif (
        requested_booking_time is not None
        and len(requested_booking_time) >= 2
    ):
        try:
            from utils.time_formatting import format_time_12h

            _rh = int(requested_booking_time[0])
            _rm = int(requested_booking_time[1])
            tstr = format_time_12h(_rh, _rm)
        except (TypeError, ValueError) as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            tstr = None
        if tstr:
            msg = (
                f"Hi{name_part} {_emoji} Unfortunately {tstr} isn't available — "
                f"that's outside when I'm usually taking bookings.\n\n"
                f"{_hours_line}\n\n"
            )
        else:
            msg = (
                f"Hi{name_part} {_emoji} Unfortunately that time is outside when I'm usually taking bookings.\n\n"
                f"{_hours_line}\n\n"
            )
    else:
        msg = (
            f"Hi{name_part} {_emoji} Unfortunately that time is outside when I'm usually taking bookings.\n\n"
            f"{_hours_line}\n\n"
        )
    if time_slots:
        msg += "Here are my next available times:\n\n"
        for slot in time_slots:
            # slot is either a (datetime, str) tuple or a plain string
            if isinstance(slot, tuple):
                slot_display = slot[1]
            else:
                slot_display = slot
            msg += f"\u2022 {slot_display}\n"
        msg += "\n"
    if profile_url:
        msg += f"{profile_url}\n\n"
    if not is_outcall:
        _loc_line = _format_incall_location_line(venue_name, address, city)
        if _loc_line:
            msg += f"{_loc_line}\n\n"
    if is_outcall:
        try:
            from core.rates_from_config import get_deposit_outcall, get_surcharge
            surcharge = get_surcharge()
            deposit_outcall = get_deposit_outcall()
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            from core.rates_from_config import get_default_pricing
            _defaults = get_default_pricing()
            surcharge = int(_defaults.get("surcharge", 100))
            deposit_outcall = int(_defaults.get("deposit_outcall", 100))
        city_str = city or "the"
        msg += build_outcall_policy_line(
            surcharge=surcharge,
            deposit_outcall=deposit_outcall,
            city=city_str,
        ) + "\n\n"
    if time_slots:
        if is_outcall:
            msg += f"{OUTCALL_TIME_AND_ADDRESS_QUESTION}\n\n"
        else:
            msg += f"{INCALL_TIME_SUITS_QUESTION}\n\n"
    else:
        if is_outcall:
            msg += f"{OUTCALL_TIME_AND_ADDRESS_QUESTION}\n\n"
        else:
            msg += f"{INCALL_TIME_BEST_QUESTION}\n\n"
    if webform_url:
        msg += f"Or use my booking webform {webform_url}"
    return msg


def get_outcall_min_duration_booking_message(
    hotel_name: str,
    webform_url: str,
    *,
    requested_time: str = "",
    profile_url: str = "",  # kept for backwards-compat, no longer used in body
) -> str:
    """
    Message when client requests an outcall booking of less than 1 hour.

    This is the dedicated response used whenever an outcall under 60 minutes
    is requested (including available-now outcall flows).
    """
    msg = "❌ Unfortunately the minimum duration for all outcall requests is 1 hour.\n\n"
    if requested_time:
        msg += (
            f"If you're wanting to book for {requested_time} then you would need to come to me. "
            "If so please text back (incall)\n\n"
        )
    else:
        msg += "If you'd still like to proceed you would need to come to me. If so please text back (incall)\n\n"
    if hotel_name:
        msg += f"I'm located at {hotel_name}\n\n"
    msg += "Or if you want me to still come to you (outcall) please respond back with minimum duration (1 hr)\n\n"
    msg += f"To make things easier if you're wanting to make any other changes use my booking webform:\n{webform_url}"
    return msg


def get_cancellation_confirmed_message() -> str:
    """
    Get message for booking cancellation confirmation.
    
    Returns:
        Cancellation confirmed message
    """
    return (
        "I've noted your cancellation. Thank you for letting me know.\n\n"
        "If you'd like to rebook in the future, just send me a message!"
    )


def get_cancellation_with_credit_message(amount: float) -> str:
    """
    Get message for cancellation with deposit credit.
    
    Args:
        amount: Deposit amount to be credited
        
    Returns:
        Cancellation with credit message
    """
    return (
        "I've noted your cancellation.\n\n"
        f"Your ${amount:.0f} deposit has been credited to your account "
        "and can be used towards a future booking.\n\n"
        "Just reach out when you're ready to rebook."
    )


def get_upload_link_success_message(phone_number: str, deposit_amount: int = 100, force_new: bool = False) -> str:
    """
    Get message when upload link is successfully generated/resent.
    
    Args:
        phone_number: Client's phone number
        deposit_amount: Deposit amount
        force_new: When True, always generate a new upload token instead of reusing an active one
        
    Returns:
        Upload link success message
    """
    _token = generate_deposit_upload_token(phone_number, deposit_amount, force_new=force_new)
    upload_url = _token.get('upload_url', '') if _token else ''
    if upload_url:
        return f"Sure thing babe, here's the upload link: {upload_url}"
    else:
        return "Let me generate an upload link for you..."


def get_upload_link_error_message() -> str:
    """
    Get message when upload link generation fails.
    
    Returns:
        Upload link error message
    """
    return "Sorry babe, I couldn't generate a new link. Text me back and I'll sort it out."


def get_webform_link_success_message(phone_number: str) -> str:
    """
    Get message when webform link is successfully generated/resent.
    
    Args:
        phone_number: Client's phone number
        
    Returns:
        Webform link success message
    """
    webform_url = get_webform_url(phone_number)
    
    return f"Sure thing, here's the link: {webform_url}"


def get_webform_link_error_message() -> str:
    """
    Get message when webform link generation fails.
    Alias for get_upload_link_error_message().
    """
    return get_upload_link_error_message()
