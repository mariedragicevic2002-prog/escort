"""
Templates for special booking types: Doubles Experience (MFF) bookings, Overnight bookings, Dinner dates, MSOG, Location, and Rate negotiation.
"""


def get_threesome_clarification_template(
    *,
    client_name: str = "",
    webform_url: str = "",
) -> str:
    """Clarify ambiguous threesome wording (MMF vs MFF) before collecting booking fields."""
    name_part = f" {client_name}" if client_name else ""
    webform_line = (
        f"\n\nI STRONGLY recommend making your booking through my webform for doubles bookings: {webform_url}"
        if webform_url
        else ""
    )
    return (
        f"Hi{name_part} when you say doubles/threesome do you mean you and a friend for a Doubles MMF booking? (2 men + myself)\n\n"
        f"Or are you looking to book myself + another escort together for a MFF booking? (1 man + 2 girls)\n\n"
        "I prefer that you organise the other escort. However if you need me to organise one for you I require a minimum 4 hours notice.\n\n"
        "Please confirm what type of doubles booking your wanting (Doubles MMF or Doubles MFF) and if you need me to organise the other person/escort?"
        f"{webform_line}"
    )


def _format_available_slot_lines(time_slots) -> str:
    """Format slot tuples as bullet lines for SMS."""
    if not time_slots:
        return "Please contact for current availability"
    return "\n".join(f"\u2022 {slot_str}" for _, slot_str in time_slots)


def _cbd_label(city: str = "") -> str:
    """Perth CBD / touring city — never a hardcoded default."""
    c = (city or "").strip()
    if c:
        return f"{c} CBD"
    try:
        from config import get_cbd_label_for_messages

        return get_cbd_label_for_messages()
    except Exception:
        return "the CBD where I'm based"


def _special_location_display(city: str = "", hotel_name: str = "", address: str = "") -> str:
    """Return full incall location phrase: 'I'm located at [hotel], [address] [city]'."""
    city_s = (city or "").strip()
    hotel_s = (hotel_name or "").strip()
    addr_s = (address or "").strip()
    hotel_addr = " ".join(p for p in [hotel_s, addr_s] if p)
    city_already_in_addr = city_s and city_s.lower() in hotel_addr.lower()
    if hotel_addr and city_s and not city_already_in_addr:
        return f"I'm located at {hotel_addr} {city_s}"
    if hotel_addr:
        return f"I'm located at {hotel_addr}"
    if city_s:
        return f"I'm currently in {city_s}"
    return ""


def build_couples_available_now_message(
    *,
    client_name: str = "",
    time_slots=None,
    profile_url: str = "",
    webform_url: str = "",
    city: str = "",
    hotel_name: str = "",
    address: str = "",
    is_outcall: bool = False,
    surcharge: int = 100,
    deposit: int = 200,
) -> str:
    """Available-now slot template for Couples MFF."""
    name_part = f" {client_name}" if client_name else ""
    slots_list = _format_available_slot_lines(time_slots)
    cbd = _cbd_label(city)
    profile_block = profile_url or "(profile_url)"

    if is_outcall:
        policy_line = (
            f"I only do outcalls to hotels or apartments within 15km of {cbd}. "
            f"There is a ${surcharge} surcharge + ${deposit} deposit required for all couples bookings."
        )
        closing_line = "Which time works for you, and what's your address?"
    else:
        location_phrase = _special_location_display(city=city, hotel_name=hotel_name, address=address)
        if location_phrase:
            policy_line = (
                f"{location_phrase}. A ${deposit} mandatory deposit "
                "is required for all couples bookings."
            )
        else:
            policy_line = f"A ${deposit} mandatory deposit is required for all couples bookings."
        closing_line = "Which time works for you, and how long would you like to book for?"

    webform_line = (
        "I STRONGLY recommend booking through my webform - just select 'Couples MFF' as the experience type:\n"
        f"{webform_url}"
        if webform_url
        else "I STRONGLY recommend booking through my webform."
    )

    return (
        f"Hi{name_part} that sounds amazing, couples bookings are one of my favourites!\n\n"
        f"Here are my closest available times:\n\n"
        f"{slots_list}\n\n"
        f"{profile_block}\n\n"
        f"{policy_line}\n\n"
        f"{webform_line}\n\n"
        f"{closing_line}"
    )


def build_doubles_available_now_message(
    *,
    client_name: str = "",
    doubles_type: str = "",
    time_slots=None,
    profile_url: str = "",
    webform_url: str = "",
    city: str = "",
    hotel_name: str = "",
    address: str = "",
    is_outcall: bool = False,
    surcharge: int = 100,
    deposit: int = 200,
    intro_style: str = "default",
    escort_sources_second_partner: bool = False,
) -> str:
    """Available-now slot template for doubles MMF/MFF.

    intro_style:
        ``default`` — "perfect - for MMF doubles..."
        ``love`` — first-turn opener when client already implied they supply the other person
        ("Hi {name} I love MMF doubles bookings..." + times available).
    """
    name_part = f" {client_name}" if client_name else ""
    slots_list = _format_available_slot_lines(time_slots)
    profile_block = profile_url or "(profile_url)"
    cbd = _cbd_label(city)

    dtype = (doubles_type or "").strip().lower()
    if dtype == "mmf":
        intro_label = "MMF doubles"
    elif dtype == "mff":
        intro_label = "MFF doubles"
    else:
        intro_label = "doubles"

    _style = (intro_style or "default").strip().lower()
    if _style == "love":
        head = (
            f"Hi{name_part} I love {intro_label} bookings!\n\n"
            f"Here are the times I have available:\n\n"
            f"{slots_list}"
        )
    else:
        head = (
            f"Hi{name_part} perfect - for {intro_label} here are my closest available times:\n\n"
            f"{slots_list}"
        )

    if is_outcall:
        policy_line = (
            f"I only do outcalls to hotels or apartments within 15km of {cbd}. "
            f"There is a ${surcharge} surcharge + ${deposit} deposit required for all doubles bookings."
        )
        closing_line = "Which time works for you, and what's your address?"
    else:
        location_phrase = _special_location_display(city=city, hotel_name=hotel_name, address=address)
        if location_phrase:
            policy_line = (
                f"{location_phrase}. "
                f"A ${deposit} mandatory deposit is required for all doubles bookings."
            )
        else:
            policy_line = f"A ${deposit} mandatory deposit is required for all doubles bookings."
        closing_line = "Which time works for you, and how long would you like to book for?"

    webform_line = (
        f"I STRONGLY recommend booking through my webform for all doubles bookings:\n{webform_url}"
        if webform_url
        else "I STRONGLY recommend booking through my webform for all doubles bookings."
    )

    pair_travel_notice = ""
    if escort_sources_second_partner and is_outcall:
        from core.rates_from_config import format_doubles_escort_arranges_second_outcall_travel_notice

        pair_travel_notice = "\n\n" + format_doubles_escort_arranges_second_outcall_travel_notice()

    return (
        f"{head}\n\n"
        f"{profile_block}\n\n"
        f"{policy_line}"
        f"{pair_travel_notice}\n\n"
        f"{webform_line}\n\n"
        f"{closing_line}"
    )


def build_doubles_escort_supply_slots_message(
    *,
    time_slots=None,
    profile_url: str = "",
    webform_url: str = "",
    city: str = "",
    hotel_name: str = "",
    address: str = "",
    intro_line: str = "No worries, I can organise the other escort for you. Here are the times I have available:",
    include_pair_outcall_travel_notice: bool = False,
) -> str:
    """Template when escort is asked to organise the second person for doubles."""
    slot_lines = "\n".join(
        f"\u2022 {slot_str}" for _, slot_str in (time_slots or [])
    )
    if not slot_lines:
        slot_lines = "Please contact for current availability"

    profile_block = profile_url or "(profile_url)"
    location_line = _special_location_display(city=city, hotel_name=hotel_name, address=address)
    webform_line = (
        f"I STRONGLY recommend booking through my webform for all doubles bookings:\n{webform_url}"
        if webform_url
        else "I STRONGLY recommend booking through my webform for all doubles bookings."
    )

    parts = [
        intro_line,
        slot_lines,
        profile_block,
    ]
    if location_line:
        parts.append(location_line)
    if include_pair_outcall_travel_notice:
        from core.rates_from_config import format_doubles_escort_arranges_second_outcall_travel_notice

        parts.append(format_doubles_escort_arranges_second_outcall_travel_notice())
    parts.extend(
        [
            "What time were you thinking?",
            "A $200 deposit is also required for all doubles bookings",
            webform_line,
        ]
    )
    return "\n\n".join(parts)


def get_overnight_booking_template(location_info: str = "") -> str:
    """
    Template for overnight booking enquiry. Rates and deposit from Rates page.
    """
    from core.rates_from_config import format_overnight_rates_text, get_deposit_overnight
    rates_text = format_overnight_rates_text()
    deposit_overnight = get_deposit_overnight()
    message = f"""Overnight bookings available! \U0001F319

Rates:
{rates_text}

Important:
\u2022 A ${deposit_overnight} deposit is required for overnight bookings
\u2022 These bookings need manual review - I'll personally confirm once deposit is received

What date were you thinking?"""
    
    if location_info:
        message = f"{message}\n\n{location_info}"
    
    return message



def get_dinner_date_template(client_name: str = "", profile_url: str = "",
                             city: str = "",
                             address: str = "", webform_url: str = "") -> str:
    """Template for dinner date booking enquiry. Rates from Rates page."""
    from core.rates_from_config import format_dinner_date_rates_text
    rates_text = format_dinner_date_rates_text()

    name_part = f" {client_name}" if client_name else ""

    return f"""Hi{name_part} I love dinner dates

Here's what you need to know:

{rates_text}

Dinner/social time counts toward the booking. You cover food and drinks separately

{profile_url}

I'm located at {address} {city}

I STRONGLY recommend booking through my webform:

{webform_url}"""


def format_requested_time_for_sms(requested_time) -> str:
    """Format requested time as SMS-friendly text (e.g. 8pm / 8:30pm)."""
    import datetime as _dt

    if isinstance(requested_time, str):
        t = requested_time.strip()
        return t if t else "that time"

    hour = None
    minute = None
    if isinstance(requested_time, _dt.datetime):
        hour, minute = requested_time.hour, requested_time.minute
    elif isinstance(requested_time, _dt.time):
        hour, minute = requested_time.hour, requested_time.minute
    elif isinstance(requested_time, (tuple, list)) and len(requested_time) >= 2:
        try:
            hour, minute = int(requested_time[0]), int(requested_time[1])
        except (TypeError, ValueError):
            hour = None
            minute = None

    if hour is None or minute is None:
        return "that time"

    period = "am" if hour < 12 else "pm"
    h12 = hour % 12 or 12
    if minute:
        return f"{h12}:{minute:02d}{period}"
    return f"{h12}{period}"


def build_dinner_date_requested_time_unavailable_full_message(
    *,
    client_name: str = "",
    slot_display_lines: list[str] | None,
    rates_text: str,
    profile_url: str,
    webform_url: str,
    city: str = "",
    requested_time=None,
    deposit: int = 100,
) -> str:
    """
    Full SMS when the client's requested dinner start time is not available:
    ❌ line + alternative slots + rates + profile + webform + 15km/deposit + closing question.

    Matches handlers/new_conv/enquiries.py _handle_dinner_date_enquiry_impl (busy path).
    """
    name_part = f" {client_name}" if client_name else ""
    lines = slot_display_lines or []
    if lines:
        slot_block = "\n".join(f"\u2022 {line}" for line in lines)
    else:
        slot_block = f"Please suggest another day or use the booking webform ({webform_url})."

    requested_time_display = format_requested_time_for_sms(requested_time)
    rates_line = (rates_text or "").strip()
    if rates_line:
        rates_line = rates_line[0].lower() + rates_line[1:]
        rates_intro_line = f"Here is what you need to know {rates_line}"
    else:
        rates_intro_line = "Here is what you need to know"

    cbd = _cbd_label(city)
    parts = [
        (
            f"Hi{name_part} I love dinner dates but \u274c Unfortunately {requested_time_display} isn't available"
        ),
        rates_intro_line,
        "Dinner/social time counts toward the booking. You cover the cost of food and drinks separately",
        f"Here are my closest available times:\n\n{slot_block}",
    ]
    if (profile_url or "").strip():
        parts.append((profile_url or "").strip())
    parts.append(
        f"I only eat at restaurants within 15km of {cbd}. There is a mandatory ${deposit} deposit also required."
    )
    parts.append(f"I STRONGLY recommend booking through my webform: {webform_url}")
    parts.append("Which time works for you, and where do you want to go to eat?")
    return "\n\n".join(parts)


def get_dinner_restaurant_prompt() -> str:
    return (
        "Which restaurant would you like to meet at? (Name and suburb or area is perfect.)\n\n"
        "Dinner dates are a 2-hour booking; I travel to meet you there."
    )


def get_dinner_food_preference_quick_reply() -> str:
    """Short answer when the client asks about favourite food/cuisine before a venue is chosen."""
    return (
        "I'm not a fussy eater babe — I pretty much eat anything 🍆 You're paying and you're the man, "
        "so you can pick where we go to eat."
    )


def get_dinner_pick_time_prompt(state: dict | None) -> str:
    """After a restaurant is confirmed, ask for a time (uses offered slots from state when present)."""
    st = state or {}
    hours = st.get("offered_slot_hours") or []
    minutes = st.get("offered_slot_minutes") or []
    if not hours:
        return (
            "Which time were you thinking? "
            "Start time must be between 5pm and 9pm (not after 9pm); the 2-hour booking can finish later."
        )
    lines = []
    for i, h in enumerate(hours):
        mi = int(minutes[i]) if minutes and i < len(minutes) else 0
        h12 = int(h) % 12 or 12
        ampm = "am" if int(h) < 12 else "pm"
        suf = f":{mi:02d}" if mi else ""
        slot = f"{h12}{suf}{ampm}"
        lines.append(f"\u2022 {slot}")
    return "Which of these times works for you?\n\n" + "\n".join(lines)


def get_dinner_after_prompt() -> str:
    """Hotel vs client's place, plus address request for their place — one SMS."""
    return (
        "After dinner, would you like to come back to my hotel with me, or head to your place?\n\n"
        "If you want to go to your place, can you please let me know your address?"
    )


def get_dinner_client_address_prompt() -> str:
    return (
        "What's your address for after dinner? I only use this to plan travel time from the restaurant to your place."
    )


def build_dinner_booking_confirmation_message(
    booking_fields: dict,
    *,
    client_home_outside_15km: bool = False,
) -> str:
    """
    Final dinner-date summary before YES: restaurant, time, 2h, Dinner Date, $1000.
    If client home is outside 15km, adds notice that after dinner you return to the escort hotel.
    """
    import datetime as _dt

    from templates.booking_reconfirmation import _format_experience, _normalized_client_name
    from templates.confirmations import calculate_price

    emoji_date = "\U0001F4C5"
    emoji_time = "\u23F0"
    emoji_duration = "\u23F1\uFE0F"
    emoji_experience = "\U0001F3AD"
    emoji_location = "\U0001F4CD"
    emoji_money = "\U0001F4B0"

    parts: list[str] = []

    if client_home_outside_15km:
        parts.append(
            "Unfortunately as your address is too far away we would need to go back to my hotel after dinner\n\n"
        )

    parts.append("Just to confirm you would like to book for:\n\n")

    date = booking_fields.get("date")
    if date:
        if hasattr(date, "strftime"):
            d0 = int(date.strftime("%d"))
            if 11 <= d0 <= 13:
                ord_suf = "th"
            else:
                ord_suf = {1: "st", 2: "nd", 3: "rd"}.get(d0 % 10, "th")
            date_str = date.strftime(f"%A {d0}{ord_suf} %B %Y")
        else:
            date_str = str(date)
        parts.append(f"{emoji_date} Date: {date_str}\n")
    else:
        parts.append(f"{emoji_date} Date: Not set\n")

    time_val = booking_fields.get("time")
    if time_val:
        if isinstance(time_val, _dt.time):
            hour, minute = time_val.hour, time_val.minute
        elif isinstance(time_val, (tuple, list)) and len(time_val) >= 2:
            hour, minute = int(time_val[0]), int(time_val[1])
        else:
            hour, minute = None, None
        if hour is not None:
            period = "pm" if hour >= 12 else "am"
            display_hour = hour % 12 or 12
            time_str = f"{display_hour}:{minute:02d}{period}" if minute else f"{display_hour}{period}"
        else:
            time_str = str(time_val)
        parts.append(f"{emoji_time} Time: {time_str}\n")
    else:
        parts.append(f"{emoji_time} Time: Not set\n")

    parts.append(f"{emoji_duration} Duration: 2h\n")
    parts.append(f"{emoji_experience} Experience: {_format_experience(booking_fields.get('experience_type')) or 'Dinner Date'}\n")

    venue = (booking_fields.get("dinner_restaurant") or booking_fields.get("outcall_address") or "").strip()
    parts.append(f"{emoji_location} Location: {venue}\n")

    total = calculate_price(
        int(booking_fields.get("duration") or 120),
        booking_fields.get("experience_type"),
        booking_fields.get("incall_outcall") or "outcall",
        booking_fields,
    )
    parts.append(f"{emoji_money} Total: ${total}\n")

    has_name = bool(_normalized_client_name(booking_fields))
    parts.append("\n")
    if has_name:
        parts.append("To confirm please respond with the word YES.")
    else:
        parts.append(
            "To confirm please respond with your first name and YES.\n"
            "(eg. John YES)"
        )

    return "".join(parts)


def get_msog_template() -> str:
    """
    Template for MSOG (Multiple Shots On Goal) enquiry.
    
    Returns:
        Formatted message template
    """
    return """MSOG is included babe... multiple shots on goal means you can cum more than once during our session.

No extra charge - it's all part of the experience. When were you thinking of booking?"""


def get_location_enquiry_template(location_info: str = "") -> str:
    """
    Template for location enquiry.
    
    Args:
        location_info: Location information string
        
    Returns:
        Formatted message template
    """
    message = f"{location_info}\n\n" if location_info else ""
    message += "I'm available for both incall at my hotel and outcall to yours. What were you looking for babe?"
    
    return message


def get_rate_negotiation_template(client_name: str = "", profile_url: str = "",
                                   experience_url: str = "", webform_url: str = "") -> str:
    """
    Template for rate negotiation attempts.

    Returns:
        Formatted message template
    """
    name_part = f" {client_name}" if client_name else ""
    return (
        f"\u274c Hi{name_part} I'm sorry, I don't negotiate my price!\n\n"
        f"{profile_url}\n\n"
        f"For further information on my rates and what I offer check out my experience page {experience_url}\n\n"
        f"If you would like to make a booking use the booking webform: {webform_url}"
    )

