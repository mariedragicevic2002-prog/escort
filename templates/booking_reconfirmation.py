"""

Booking Reconfirmation Templates
Templates for showing collected booking details before final confirmation.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


from config import get_account_name, get_current_incall_location, get_payid

import logging

logger = logging.getLogger("escort_chatbot.booking_reconfirmation")

EXPERIENCE_URL = "(experience_url)"


def _normalized_client_name(booking_fields: dict) -> str:
    """Return a safe client name; blank when missing/invalid (e.g. 'midday')."""
    try:
        from templates.greetings import is_valid_client_name
        candidate = (booking_fields.get('client_name') or '').strip()
        return candidate if is_valid_client_name(candidate) else ""
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        return (booking_fields.get('client_name') or '').strip()

_EXPERIENCE_DISPLAY = {
    "couples_mff": "Couples MFF",
    "couples mff": "Couples MFF",
    "doubles_mff": "Doubles MFF",
    "doubles mff": "Doubles MFF",
    "doubles_mmf": "Doubles MMF",
    "Doubles MMF": "Doubles MMF",
    "doubles mmf": "Doubles MMF",
    "dinner_date": "Dinner Date",
    "dinner date": "Dinner Date",
    "gfe": "GFE",
    "pse": "PSE",
    "dgfe": "DGFE",
    "massage": "Massage",
}


def _format_experience(experience_type: str | None) -> str:
    """Return a human-readable display label for an experience type value."""
    if not experience_type:
        return ""
    raw = experience_type.strip()
    return _EXPERIENCE_DISPLAY.get(raw.lower(), raw)


def _format_date_ordinal(date) -> str:
    """Format date with ordinal suffix (e.g., 'Thursday, 8th May 2026')."""
    if not hasattr(date, 'strftime'):
        return str(date)
    try:
        day = date.day
        if 11 <= day <= 13:
            suffix = 'th'
        else:
            suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
        return date.strftime(f"%A, {day}{suffix} %B %Y")
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        return str(date)


def _experience_prompt(experience_type=None):
    """Return optional experience hint if experience not yet set, else empty string."""
    if experience_type:
        return ""
    return (
        f"\n\nIf you'd like, you can also include your preferred experience (eg. YES & PSE)"
        f"\n\nUnsure what experience to choose: {EXPERIENCE_URL}"
    )


def _title_case_address(address: str) -> str:
    """Apply title case to a user-provided address when it appears to be all lowercase."""
    if not address or address == 'Not set':
        return address
    stripped = address.strip()
    if stripped == stripped.lower():
        return stripped.title()
    return stripped


def _compose_incall_location(city: str, hotel: str) -> str:
    """Compose incall location without repeating city (e.g. 'Adelaide, Adelaide')."""
    city_text = (city or "").strip()
    hotel_text = (hotel or "").strip()

    if city_text and hotel_text:
        if city_text.lower() in hotel_text.lower():
            return hotel_text
        return f"{hotel_text}, {city_text}"
    if city_text:
        return city_text
    if hotel_text:
        return hotel_text
    return "my incall location"


def dedupe_incall_address_line(addr: str, city: str) -> str:
    """Avoid 'Street Name Adelaide, Adelaide' when the last city segment repeats suburb or city field."""
    addr_line = (addr or "").strip()
    city_text = (city or "").strip()
    if not addr_line or not city_text:
        return addr_line
    parts = [p.strip() for p in addr_line.split(",")]
    if len(parts) < 2:
        return addr_line
    last = parts[-1]
    prev = ", ".join(parts[:-1])
    if last.lower() != city_text.lower():
        return addr_line
    if city_text.lower() in prev.lower():
        return prev
    return addr_line


def build_booking_reconfirmation(booking_fields: dict, include_yes_prompt: bool = True, skip_optional_deposit: bool = False) -> str:
    """
    Build a booking reconfirmation message showing all collected details.

    This is the ONLY case where we show all fields after collection -
    for final confirmation before locking in the booking.

    When the client has only responded with experience (e.g. GFE), we treat that as
    confirmation and omit the "Reply YES to confirm" line so no extra step is needed.

    Args:
        booking_fields: Dict with all booking fields
        include_yes_prompt: If False, omit "Reply YES to confirm..." (used when GFE-only is accepted as confirm)
        skip_optional_deposit: If True, do not add optional deposit paragraph (e.g. for available-now incall, no deposit ask)

    Returns:
        Reconfirmation message string
    """
    emoji_date = "\U0001F4C5"
    emoji_time = "\u23F0"
    emoji_duration = "\u23F1\uFE0F"
    emoji_experience = "\U0001F3AD"
    emoji_location = "\U0001F4CD"
    emoji_money = "\U0001F4B0"

    # Opening: Thanks (client name), just to confirm you would like to book for:
    client_name = _normalized_client_name(booking_fields)
    if client_name:
        reconfirm = f"Thanks {client_name}, just to confirm you would like to book for:\n\n"
    else:
        reconfirm = "Thanks! Just to confirm you would like to book for:\n\n"

    # Date
    date = booking_fields.get('date')
    if date:
        if hasattr(date, 'strftime'):
            date_str = _format_date_ordinal(date)
        else:
            date_str = str(date)
        reconfirm += f"{emoji_date} Date: {date_str}\n"
    else:
        reconfirm += f"{emoji_date} Date: Not set\n"

    # Time
    import datetime as _dt
    time = booking_fields.get('time')
    if time:
        if isinstance(time, _dt.time):
            hour, minute = time.hour, time.minute
        elif isinstance(time, (tuple, list)) and len(time) == 2:
            hour, minute = int(time[0]), int(time[1])
        else:
            hour, minute = None, None
        if hour is not None:
            period = "pm" if hour >= 12 else "am"
            display_hour = hour if hour <= 12 else hour - 12
            if display_hour == 0:
                display_hour = 12
            time_str = f"{display_hour}:{minute:02d}{period}" if minute else f"{display_hour}{period}"
        else:
            time_str = str(time)
        reconfirm += f"{emoji_time} Time: {time_str}\n"
    else:
        reconfirm += f"{emoji_time} Time: Not set\n"

    # Duration
    duration = booking_fields.get('duration')
    if duration:
        if duration >= 60:
            hours = duration // 60
            mins = duration % 60
            if mins > 0:
                duration_str = f"{hours}h {mins}min"
            else:
                duration_str = f"{hours}h"
        else:
            duration_str = f"{duration}min"
        reconfirm += f"{emoji_duration} Duration: {duration_str}\n"
    else:
        reconfirm += f"{emoji_duration} Duration: Not set\n"

    # Experience
    experience_type = booking_fields.get('experience_type')
    if experience_type and str(experience_type).strip():
        reconfirm += f"{emoji_experience} Experience: {_format_experience(experience_type)}\n"

    # Location: Incall @ Location: [City] - [Hotel Name]; Outcall: Location: [address]
    incall_outcall = (booking_fields.get('incall_outcall') or '').lower()
    if incall_outcall == "incall":
        try:
            location = get_current_incall_location()
            city = location.get('city', '')
            hotel = location.get('display_name') or location.get('hotel_name') or location.get('address', '')
            location_str = _compose_incall_location(city, hotel)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            try:
                from core.settings_manager import get_setting
                city = get_setting('city', '')
                hotel = get_setting('hotel_name', '')
                location_str = _compose_incall_location(city, hotel)
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
                location_str = "my incall location"
        reconfirm += f"{emoji_location} Incall @ Location: {location_str}\n"
    elif incall_outcall == "outcall":
        address = _title_case_address(booking_fields.get('outcall_address') or 'Not set')
        reconfirm += f"{emoji_location} Location: {address}\n"
    else:
        try:
            location = get_current_incall_location()
            city = location.get('city', '')
            hotel = location.get('display_name') or location.get('hotel_name') or location.get('address', '')
            location_str = _compose_incall_location(city, hotel)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            location_str = "my incall location"
        reconfirm += f"{emoji_location} Incall @ Location: {location_str}\n"

    # Total price
    try:
        from templates.confirmations import calculate_price
        _dur = booking_fields.get('duration') or 60
        _exp = booking_fields.get('experience_type')
        _loc = booking_fields.get('incall_outcall') or 'incall'
        if _exp:
            total = calculate_price(_dur, _exp, _loc, booking_fields)
            reconfirm += f"{emoji_money} Total: ${total}\n"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)

    # Non-mandatory deposit advice: incall only. Never for outcalls (they have already paid a mandatory deposit).
    # Skip for available-now incall (skip_optional_deposit=True) so we do not ask for deposit.
    try:
        if skip_optional_deposit:
            pass  # Do not add optional deposit paragraph
        else:
            from core.feature_flags import optional_deposit_enabled
            payid = get_payid()
            account_name = get_account_name()
            deposit_required = booking_fields.get('deposit_required', False)
            phone_number = booking_fields.get('phone_number')  # Get phone number if available
            
            # Incall only: optional deposit. Outcalls use same confirmation template but skip this block.
            if (
                optional_deposit_enabled()
                and incall_outcall == "incall"
                and not deposit_required
                and payid
                and account_name
            ):
                # Use the exact non-mandatory deposit template from old folder
                from templates.deposit_templates import get_non_mandatory_deposit_template
                non_mandatory_msg = get_non_mandatory_deposit_template(phone_number=phone_number)
                reconfirm += f"\n\n{non_mandatory_msg}\n"
                reconfirm += "\n(Please note if you change your experience that will also reflect total cost of booking)\n"
    except Exception as e:
        logger.warning("Failed to add non-mandatory deposit advice: %s", e)

    if include_yes_prompt:
        booking_type = (booking_fields.get('booking_type') or '').lower()
        has_time = bool(booking_fields.get('time'))
        has_name = bool(_normalized_client_name(booking_fields))
        has_experience = bool((booking_fields.get('experience_type') or '').strip())
        if booking_type == 'overnight' and not has_time:
            reconfirm += "\nTo confirm please advise what TIME you wish to start and reply YES to confirm."
        elif has_name:
            if has_experience:
                reconfirm += "\nTo confirm please respond with the word YES."
            else:
                reconfirm += "\nTo confirm please respond with YES (you can also include your experience — e.g. YES GFE)."
        else:
            if has_experience:
                reconfirm += "\nTo confirm please respond with your first name and YES.\n(eg. John YES)"
            else:
                reconfirm += "\nTo confirm please respond with your first name and YES.\n(eg. John GFE YES)"

    return reconfirm


def build_incall_preconfirm_summary(booking_fields: dict, webform_url: str = "") -> str:
    """
    Booking summary sent AFTER calendar check passes, BEFORE client confirms.
    Shows Date / Time / Duration / Location (and Experience line only if already set).
    Asks for YES (or first name + YES) only—experience can be added via the booking webform;
    we do not prompt for experience type here.
    """
    emoji_date = "\U0001F4C5"
    emoji_time = "\u23F0"
    emoji_duration = "\u23F1\uFE0F"
    emoji_experience = "\U0001F3AD"
    emoji_location = "\U0001F4CD"
    emoji_money = "\U0001F4B0"

    experience_type = booking_fields.get('experience_type')
    has_experience = bool(experience_type and str(experience_type).strip())

    msg = "\u2705 Your booking summary:\n\n"

    # Date
    date = booking_fields.get('date')
    if date:
        date_str = _format_date_ordinal(date) if hasattr(date, 'strftime') else str(date)
        msg += f"{emoji_date} Date: {date_str}\n"
    else:
        msg += f"{emoji_date} Date: Not set\n"

    # Time
    time_val = booking_fields.get('time')
    if time_val:
        import datetime as _dt
        if isinstance(time_val, _dt.time):
            hour, minute = time_val.hour, time_val.minute
        elif isinstance(time_val, (tuple, list)) and len(time_val) == 2:
            hour, minute = int(time_val[0]), int(time_val[1])
        else:
            hour, minute = None, None
        if hour is not None:
            period = "pm" if hour >= 12 else "am"
            display_hour = (hour if hour <= 12 else hour - 12) or 12
            time_str = f"{display_hour}:{minute:02d}{period}" if minute else f"{display_hour}{period}"
        else:
            time_str = str(time_val)
        msg += f"{emoji_time} Time: {time_str}\n"
    else:
        msg += f"{emoji_time} Time: Not set\n"

    # Duration
    duration = booking_fields.get('duration')
    if duration:
        if duration >= 60:
            h, m = divmod(duration, 60)
            duration_str = f"{h}h {m}min" if m else f"{h}h"
        else:
            duration_str = f"{duration}min"
        msg += f"{emoji_duration} Duration: {duration_str}\n"
    else:
        msg += f"{emoji_duration} Duration: Not set\n"

    # Experience (only shown if already set)
    if has_experience:
        msg += f"{emoji_experience} Experience: {_format_experience(experience_type)}\n"

    # Location \u2014 incall or outcall
    incall_outcall = (booking_fields.get('incall_outcall') or '').lower()
    if incall_outcall == 'outcall':
        address = _title_case_address(booking_fields.get('outcall_address') or 'Not set')
        msg += f"{emoji_location} Outcall @ Location: {address}\n"
    else:
        try:
            location = get_current_incall_location()
            city = location.get('city', '')
            hotel = location.get('display_name') or location.get('hotel_name') or location.get('address', '')
            location_str = _compose_incall_location(city, hotel)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            location_str = "my incall location"
        msg += f"{emoji_location} Incall @ Location: {location_str}\n"

    # Total price
    try:
        from templates.confirmations import calculate_price
        _dur = booking_fields.get('duration') or 60
        _exp = booking_fields.get('experience_type')
        _loc = booking_fields.get('incall_outcall') or 'incall'
        if _exp:
            total = calculate_price(_dur, _exp, _loc, booking_fields)
            msg += f"{emoji_money} Total: ${total}\n"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)

    # Closing prompt — include experience request if not yet set
    has_name = bool(_normalized_client_name(booking_fields))
    if has_experience and has_name:
        msg += "\nTo confirm please respond with the word YES."
    elif has_experience:
        msg += "\nTo confirm please respond with your first name and YES.\n(eg. John YES)"
    elif has_name:
        msg += "\nReply YES to confirm (you can also include your experience — e.g. YES GFE)."
    else:
        msg += "\nTo confirm please respond with your name, experience type and YES.\n(eg. John GFE YES)\nExperience and name are optional — just YES also works."

    if webform_url:
        msg += f"\n\nTo change your booking please fill out the booking webform:\n{webform_url}"

    return msg


def build_available_now_outcall_reconfirmation(booking_fields: dict, webform_url: str = "") -> str:
    """
    Build the booking summary for available-now outcall only.
    Closing: "Reply YES to confirm, or to make a change fill out the booking webform: (url)".

    Args:
        booking_fields: Dict with all booking fields
        webform_url: URL for the booking webform

    Returns:
        Reconfirmation message string
    """
    emoji_date = "\U0001F4C5"
    emoji_time = "\u23F0"
    emoji_duration = "\u23F1\uFE0F"
    emoji_experience = "\U0001F3AD"
    emoji_location = "\U0001F4CD"
    emoji_money = "\U0001F4B0"

    client_name = _normalized_client_name(booking_fields)
    if client_name:
        reconfirm = f"Thanks {client_name}, just to confirm you would like to book for:\n\n"
    else:
        reconfirm = "Thanks! Just to confirm you would like to book for:\n\n"

    # Date
    date = booking_fields.get('date')
    if date:
        if hasattr(date, 'strftime'):
            date_str = _format_date_ordinal(date)
        else:
            date_str = str(date)
        reconfirm += f"{emoji_date} Date: {date_str}\n"
    else:
        reconfirm += f"{emoji_date} Date: Not set\n"

    # Time
    import datetime as _dt
    time = booking_fields.get('time')
    if time:
        if isinstance(time, _dt.time):
            hour, minute = time.hour, time.minute
        elif isinstance(time, (tuple, list)) and len(time) == 2:
            hour, minute = int(time[0]), int(time[1])
        else:
            hour, minute = None, None
        if hour is not None:
            period = "pm" if hour >= 12 else "am"
            display_hour = hour if hour <= 12 else hour - 12
            if display_hour == 0:
                display_hour = 12
            time_str = f"{display_hour}:{minute:02d}{period}" if minute else f"{display_hour}{period}"
        else:
            time_str = str(time)
        reconfirm += f"{emoji_time} Time: {time_str}\n"
    else:
        reconfirm += f"{emoji_time} Time: Not set\n"

    # Duration
    duration = booking_fields.get('duration')
    if duration:
        if duration >= 60:
            hours = duration // 60
            mins = duration % 60
            if mins > 0:
                duration_str = f"{hours}h {mins}min"
            else:
                duration_str = f"{hours}h"
        else:
            duration_str = f"{duration}min"
        reconfirm += f"{emoji_duration} Duration: {duration_str}\n"
    else:
        reconfirm += f"{emoji_duration} Duration: Not set\n"

    # Experience
    experience_type = booking_fields.get('experience_type')
    if experience_type and str(experience_type).strip():
        reconfirm += f"{emoji_experience} Experience: {_format_experience(experience_type)}\n"

    # Location (outcall only)
    address = (booking_fields.get('outcall_address') or 'Not set')
    reconfirm += f"{emoji_location} Location: {address}\n"

    # Total price
    try:
        from templates.confirmations import calculate_price
        _dur = booking_fields.get('duration') or 60
        _exp = booking_fields.get('experience_type')
        _loc = booking_fields.get('incall_outcall') or 'outcall'
        if _exp:
            total = calculate_price(_dur, _exp, _loc, booking_fields)
            reconfirm += f"{emoji_money} Total: ${total}\n"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)

    has_name = bool(_normalized_client_name(booking_fields))
    if has_name:
        reconfirm += "\nReply YES to confirm, or to make a change fill out the booking webform: "
    else:
        reconfirm += (
            "\nReply with your first name and YES to confirm.\n(eg. John YES)\n\n"
            "To make a change fill out the booking webform: "
        )
    reconfirm += (webform_url or "link in my profile")
    return reconfirm


def build_available_now_confirm_prompt(client_name: str = None, webform_url: str = "") -> str:
    """
    Build the confirmation prompt for available-now when the requested slot is free.
    Asks for YES or first name + YES only (experience may be set elsewhere; not prompted here).
    """
    has_name = bool(client_name and client_name.strip())
    if has_name:
        prompt = "\n\nTo confirm this booking please respond with the word YES."
    else:
        prompt = "\n\nTo confirm, reply with your first name and YES.\n(eg. John YES)"
    prompt += "\n\nTo change your booking please fill out the booking webform: " + (webform_url or "link in my profile")
    return prompt

