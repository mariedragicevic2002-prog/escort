"""

Confirmation Templates
Templates for booking confirmations, deposit requests, and alternative time slots.
Pricing uses the Rates page config (admin) when available.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import logging

logger = logging.getLogger(__name__)

_DATE_FMT = "%A, %d %B %Y"  # Kept for backward compat; use format_date_ordinal_full() instead


def _format_date_with_ordinal(date_val) -> str:
    """Format date with ordinal suffix (e.g., 'Thursday, 8th May 2026')."""
    try:
        if hasattr(date_val, 'strftime'):
            day = date_val.day
            if 11 <= day <= 13:
                suffix = 'th'
            else:
                suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
            return date_val.strftime(f"%A, {day}{suffix} %B %Y")
        return str(date_val)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        return str(date_val)


def _display_experience_label(experience_type: str) -> str:
    """Format internal experience keys into human-readable labels for client-facing SMS."""
    try:
        from templates.booking_reconfirmation import _format_experience
        return _format_experience(experience_type)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        return experience_type


def _duration_minutes_to_str(duration_min: int) -> str:
    if duration_min >= 60:
        hours, mins = duration_min // 60, duration_min % 60
        if mins > 0:
            return f"{hours}h {mins}min"
        return f"{hours}h"
    return f"{duration_min} minutes"


def _experience_line_from_fields(booking_fields: dict) -> str | None:
    exp_raw = (booking_fields.get("experience_type") or "").strip()
    if not exp_raw:
        return None
    if booking_fields.get("booking_type") == "overnight" or "overnight" in exp_raw.lower():
        exp = "Overnight"
    else:
        exp = _display_experience_label(exp_raw)
    return f"\U0001F3AD Experience: {exp} (This can be changed prior to booking or when you arrive)"


def _incall_location_lines() -> list[str]:
    lines: list[str] = []
    try:
        from config import get_current_incall_location
        loc = get_current_incall_location()
        city = loc.get("city", "")
        hotel = loc.get("display_name") or loc.get("hotel_name", "")
        if city and hotel:
            lines.append(f"\U0001F4CD Location: {city} - {hotel}")
        elif city:
            lines.append(f"\U0001F4CD Location: {city}")
        else:
            lines.append(f"\U0001F4CD Location: {hotel or 'Incall'}")
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        from core.settings_manager import get_setting
        city = get_setting("city", "") or ""
        hotel = get_setting("hotel_name", "") or ""
        if city and hotel:
            lines.append(f"\U0001F4CD Location: {city} - {hotel}")
        else:
            lines.append(f"\U0001F4CD Location: {city or hotel or 'Incall'}")
    return lines


def _load_rates_page_pricing() -> dict:
    """Load pricing from the centralized rates module (same structure as admin rates blueprint)."""
    try:
        from core.rates_from_config import (
            _load_pricing,
            get_deposit_incall,
            get_deposit_mff_pair,
            get_deposit_outcall,
            get_deposit_overnight,
            get_incall_pricing,
            get_outcall_pricing,
            get_surcharge,
        )
        full = _load_pricing()
        base_sur = int(full.get("surcharge") or 100)
        doubles_sur = full.get("surcharge_doubles_escort_supplied_outcall")
        if doubles_sur is None:
            doubles_sur_i = max(base_sur * 2, 200)
        else:
            try:
                doubles_sur_i = int(doubles_sur)
            except (TypeError, ValueError):
                doubles_sur_i = max(base_sur * 2, 200)
        return {
            "incall": get_incall_pricing(),
            "outcall": get_outcall_pricing(),
            "surcharge": get_surcharge(),
            "surcharge_doubles_escort_supplied_outcall": doubles_sur_i,
            "deposit_outcall": get_deposit_outcall(),
            "deposit_incall": get_deposit_incall(),
            "deposit_mff_pair": get_deposit_mff_pair(),
            "deposit_overnight": get_deposit_overnight(),
        }
    except Exception as e:
        logger.warning("Could not load centralized pricing config: %s", e)
    try:
        from core.rates_from_config import get_default_pricing

        return get_default_pricing()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        # Last-resort fallback if central pricing module is unavailable
        return {
            "incall": {},
            "outcall": {},
            "surcharge": 100,
            "surcharge_doubles_escort_supplied_outcall": 200,
            "deposit_outcall": 100,
            "deposit_incall": 50,
            "deposit_mff_pair": 200,
            "deposit_overnight": 200,
        }


def format_booking_summary(booking_fields: dict) -> str:
    """Format booking details into a readable summary.

    Args:
        booking_fields: Dict with date, time, duration, experience_type, incall_outcall, etc.

    Returns:
        Formatted booking summary string
    """
    lines: list[str] = []

    if booking_fields.get("date"):
        date_val = booking_fields["date"]
        date_str = date_val.strftime(_DATE_FMT) if hasattr(date_val, "strftime") else str(date_val)
        lines.append(f"\U0001F4C5 Date: {date_str}")

    if booking_fields.get("time"):
        time_val = booking_fields["time"]
        if isinstance(time_val, tuple) and len(time_val) >= 2:
            hour, minute = time_val[0], time_val[1]
            period = "PM" if hour >= 12 else "AM"
            display_hour = hour if hour <= 12 else hour - 12
            if display_hour == 0:
                display_hour = 12
            time_str = f"{display_hour}:{minute:02d}{period}"
        else:
            time_str = str(time_val)
        lines.append(f"\u23F0 Time: {time_str}")

    if booking_fields.get("duration"):
        lines.append(f"\u23F1\uFE0F Duration: {_duration_minutes_to_str(booking_fields['duration'])}")

    exp_line = _experience_line_from_fields(booking_fields)
    if exp_line:
        lines.append(exp_line)

    incall_outcall = (booking_fields.get("incall_outcall") or "").lower()
    if incall_outcall == "incall":
        lines.extend(_incall_location_lines())
    elif incall_outcall == "outcall" and booking_fields.get("outcall_address"):
        lines.append(f"\U0001F4CD Location: {booking_fields['outcall_address']}")
    elif incall_outcall:
        lines.append(f"\U0001F4CD Location: {incall_outcall}")

    return "\n".join(lines)


def _experience_to_prefix(experience_type: str | None) -> str:
    """Map experience_type from booking to Rates page key prefix (e.g. gfe, dgfe, pse, mmf)."""
    if not experience_type:
        return "gfe"
    raw = (experience_type or "").strip().lower()
    norm = raw.replace(" ", "_").replace("-", "_")
    # Doubles MFF must win before generic ``mff`` — otherwise ``doubles_mff`` hits the flat ``mff`` tariff ($1000).
    if "doubles_mff" in norm:
        return "doubles_mff"
    if "dgfe" in raw or "deluxe" in raw:
        return "dgfe"
    if "pse" in raw:
        return "pse"
    if "couples" in raw or (raw == "mff") or (raw == "couples_mff"):
        return "mff"
    if "mmf" in raw:
        return "mmf"
    if "mff" in raw:
        return "mff"
    if "overnight" in raw:
        return "overnight"
    if "weekend" in raw:
        return "weekend"
    if "fly" in raw or "fmty" in raw:
        return "fly_me"
    if "dinner" in raw or "date" in raw:
        return "dinner_date"
    if "filming" in raw:
        return "pse_filming"
    return "gfe"


def _fixed_tariff_price(
    exp_prefix: str,
    bucket: dict,
    incall: dict,
    outcall_defaults: dict,
    incall_defaults: dict,
) -> int | None:
    if exp_prefix == "overnight":
        return int(
            bucket.get("overnight")
            or incall.get("overnight")
            or outcall_defaults.get("overnight")
            or incall_defaults.get("overnight")
            or 5000
        )
    if exp_prefix == "weekend":
        return int(
            bucket.get("weekend")
            or incall.get("weekend")
            or outcall_defaults.get("weekend")
            or incall_defaults.get("weekend")
            or 9000
        )
    if exp_prefix == "fly_me":
        return int(incall.get("fly_me") or incall_defaults.get("fly_me") or 5500)
    if exp_prefix == "dinner_date":
        return int(incall.get("dinner_date") or incall_defaults.get("dinner_date") or 1000)
    if exp_prefix == "pse_filming":
        return int(incall.get("pse_filming") or incall_defaults.get("pse_filming") or 1200)
    return None


def _outcall_tier_price(
    duration_minutes: int,
    incall_price_30: int,
    price_60: int,
    incall_price_60: int,
    surcharge: int,
) -> int | None:
    """Return the outcall price for a given duration, or None for overnight."""
    if duration_minutes <= 30:
        return incall_price_30 + surcharge
    if duration_minutes <= 60:
        return price_60
    if duration_minutes <= 90:
        return incall_price_60 + surcharge
    if duration_minutes <= 120:
        return (incall_price_60 * 2) + surcharge
    if duration_minutes <= 180:
        return (incall_price_60 * 3) + surcharge
    return None


def _duration_tier_price(
    duration_minutes: int,
    loc: str,
    surcharge: int,
    price_30: int,
    price_60: int,
    incall_price_30: int,
    incall_price_60: int,
    bucket: dict,
    incall: dict,
    outcall_defaults: dict,
    incall_defaults: dict,
) -> int:
    if loc == "outcall":
        result = _outcall_tier_price(duration_minutes, incall_price_30, price_60, incall_price_60, surcharge)
        if result is not None:
            return result
    if duration_minutes <= 30:
        return price_30
    if duration_minutes <= 60:
        return price_60
    if duration_minutes <= 90:
        return price_60 + surcharge
    if duration_minutes <= 120:
        return price_60 * 2
    if duration_minutes <= 180:
        return price_60 * 3
    return int(
        bucket.get("overnight")
        or incall.get("overnight")
        or outcall_defaults.get("overnight")
        or incall_defaults.get("overnight")
        or 5000
    )


def calculate_price(
    duration_minutes: int,
    experience_type: str | None = None,
    incall_outcall: str | None = "incall",
    booking_fields: dict | None = None,
) -> int:
    """Calculate price from Rates page config (admin). Uses duration, experience type, and incall/outcall.

    Args:
        duration_minutes: Duration in minutes
        experience_type: e.g. GFE, PSE, DGFE
        incall_outcall: 'incall' or 'outcall'
        booking_fields: Optional booking snapshot (``escort_supply_source``, ``booking_type``) so Doubles MFF
            uses ``mff_supplied_*`` when you arrange the other escort vs ``couples_*`` when the client brings her.
            Couples MFF (prefix ``mff``) uses ``incall.mff`` as the **hourly** rate, pro‑rated by duration.

    Returns:
        Price in dollars
    """
    pricing = _load_rates_page_pricing()
    from core.rates_from_config import get_default_pricing
    defaults = get_default_pricing()
    incall_defaults = defaults.get("incall") or {}
    outcall_defaults = defaults.get("outcall") or {}
    incall = pricing.get("incall") or {}
    outcall = pricing.get("outcall") or {}
    base_surcharge = int(pricing.get("surcharge") or defaults.get("surcharge") or 100)
    doubles_surcharge = int(
        pricing.get("surcharge_doubles_escort_supplied_outcall")
        or defaults.get("surcharge_doubles_escort_supplied_outcall")
        or max(base_surcharge * 2, 200)
    )
    loc = (incall_outcall or "incall").strip().lower()
    bucket = outcall if loc == "outcall" else incall

    from core.rates_from_config import is_doubles_escort_supplies_second_provider

    if loc == "outcall" and booking_fields and is_doubles_escort_supplies_second_provider(booking_fields):
        surcharge = doubles_surcharge
    else:
        surcharge = base_surcharge

    exp_prefix = _experience_to_prefix(experience_type)
    if booking_fields:
        bt = (booking_fields.get("booking_type") or "").strip().lower()
        if bt == "doubles_mff":
            exp_prefix = "doubles_mff"

    # Doubles MFF — duration-tier pricing (never the flat ``mff`` couples single-hour figure).
    if exp_prefix == "doubles_mff":
        bf = booking_fields or {}
        escort_sources = (bf.get("escort_supply_source") or "").strip().lower() == "escort"
        base_key_30 = "mff_supplied_30" if escort_sources else "couples_mff_30"
        base_key_60 = "mff_supplied_60" if escort_sources else "couples_mmf_60"
        default_30 = 800 if escort_sources else 400
        default_60 = 1600 if escort_sources else 800
        price_30 = int(
            bucket.get(base_key_30)
            or incall.get(base_key_30)
            or outcall_defaults.get(base_key_30)
            or incall_defaults.get(base_key_30)
            or default_30
        )
        price_60 = int(
            bucket.get(base_key_60)
            or incall.get(base_key_60)
            or outcall_defaults.get(base_key_60)
            or incall_defaults.get(base_key_60)
            or default_60
        )
        incall_price_30 = int(incall.get(base_key_30) or incall_defaults.get(base_key_30) or default_30)
        incall_price_60 = int(incall.get(base_key_60) or incall_defaults.get(base_key_60) or default_60)
        return _duration_tier_price(
            duration_minutes,
            loc,
            surcharge,
            price_30,
            price_60,
            incall_price_30,
            incall_price_60,
            bucket,
            incall,
            outcall_defaults,
            incall_defaults,
        )

    # Couples MFF — ``incall.mff`` is the hourly rate (pro‑rated), not a flat session price.
    if exp_prefix == "mff":
        hourly = int(
            bucket.get("mff")
            or incall.get("mff")
            or outcall_defaults.get("mff")
            or incall_defaults.get("mff")
            or 1000
        )
        base = int(round(duration_minutes * hourly / 60))
        if loc == "outcall":
            return base + surcharge
        return base

    fixed = _fixed_tariff_price(exp_prefix, bucket, incall, outcall_defaults, incall_defaults)
    if fixed is not None:
        return fixed

    # Duration-based keys: gfe_30, gfe_60, dgfe_30, dgfe_60, pse_30, pse_60, mmf_30, mmf_60
    base_key_30 = f"{exp_prefix}_30" if exp_prefix in ("gfe", "dgfe", "pse") else "gfe_30"
    base_key_60 = f"{exp_prefix}_60" if exp_prefix in ("gfe", "dgfe", "pse") else "gfe_60"
    if exp_prefix == "mmf":
        base_key_30, base_key_60 = "mmf_30", "mmf_60"

    price_30 = int(
        bucket.get(base_key_30)
        or incall.get(base_key_30)
        or outcall_defaults.get(base_key_30)
        or incall_defaults.get(base_key_30)
        or 400
    )
    price_60 = int(
        bucket.get(base_key_60)
        or incall.get(base_key_60)
        or outcall_defaults.get(base_key_60)
        or incall_defaults.get(base_key_60)
        or 700
    )
    incall_price_30 = int(incall.get(base_key_30) or incall_defaults.get(base_key_30) or 400)
    incall_price_60 = int(incall.get(base_key_60) or incall_defaults.get(base_key_60) or 700)

    return _duration_tier_price(
        duration_minutes,
        loc,
        surcharge,
        price_30,
        price_60,
        incall_price_30,
        incall_price_60,
        bucket,
        incall,
        outcall_defaults,
        incall_defaults,
    )


def format_booking_confirmation(booking_fields: dict, price: int) -> str:
    """Format booking confirmation message (used for outcall/reconfirmation step).

    Args:
        booking_fields: Dict with booking info
        price: Total price

    Returns:
        Formatted confirmation message
    """
    summary = format_booking_summary(booking_fields)

    message = f"\U0001F4C5 Booking Summary:\n\n{summary}\n\n"
    message += f"\U0001F4B0 Price: ${price}\n\n"
    
    # Add touring information if currently touring
    if is_currently_touring():
        touring_msg = get_touring_info_message()
        if touring_msg:
            message += f"{touring_msg}\n\n"
    
    client_name = (booking_fields.get('client_name') or '').strip()
    try:
        from templates.greetings import is_valid_client_name
        if not is_valid_client_name(client_name):
            client_name = ""
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
    name_prompt = "" if client_name else " as well as your name"
    message += f"Reply YES{name_prompt} to confirm."

    return message


def get_incall_confirmed_message(booking_fields: dict, price: int) -> str:
    """Incall booking confirmed (no mandatory deposit): Thanks! Your booking has been confirmed for: ...

    Used when incall slot is free and we go straight to CONFIRMED with optional deposit.

    Args:
        booking_fields: Dict with booking info
        price: Total price

    Returns:
        Formatted confirmation message with optional deposit paragraph
    """
    client_name = (booking_fields.get('client_name') or '').strip()
    try:
        from templates.greetings import is_valid_client_name
        if not is_valid_client_name(client_name):
            client_name = ""
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
    name_str = f" {client_name}" if client_name else ""
    message = f"Thanks{name_str} your booking has been reserved.\n\n"

    try:
        from templates.deposit_templates import get_non_mandatory_deposit_template
        phone_number = booking_fields.get('phone_number')
        non_mandatory = get_non_mandatory_deposit_template(phone_number=phone_number)
        message += non_mandatory
    except Exception as e:
        logger.warning("Optional deposit append failed: %s", e)
    return message


def get_deposit_request_message(amount: int, reason: str, phone_number: str = None, upload_url: str = None,
                                mandatory: bool = True, client_name: str = None, outcall_address: str = None,
                                booking_fields: dict | None = None, payment_reference: str | None = None) -> str:
    """Format deposit request message.

    Args:
        amount: Deposit amount required
        reason: Reason for deposit (e.g., "outcall", "doubles_mff", "overnight")
        phone_number: Client's phone number (for generating upload link)
        upload_url: Optional upload URL (if None, will generate one)
        mandatory: True for mandatory deposit, False for optional/non-mandatory
        client_name: Optional client name (used for overnight manual-review message)
        booking_fields: Optional booking dict for full mandatory SMS (date, time, experience_type, etc.)

    Returns:
        Formatted deposit request message
    """
    # Use build_deposit_message for consistency with old folder
    if not mandatory:
        from templates.deposit_templates import build_deposit_message
        return build_deposit_message(
            mandatory=False,
            followup=False,
            upload_url=upload_url,
            phone_number=phone_number,
            deposit_amount=amount,
            payment_reference=payment_reference,
        )

    # Mandatory deposit — full PayID/upload copy is on the booking site
    from templates.deposit_templates import (
        append_doubles_sourcing_deposit_notice_if_needed,
        get_deposit_payment_page_url,
    )

    r = reason or ""

    # Graphite soft-hold: slot available but another client has a pending deposit on it
    if "graphite_hold" in r:
        amount_str = f"{amount}.00" if isinstance(amount, int) else str(amount)
        url = get_deposit_payment_page_url(
            phone_number, mandatory=True, amount=amount, reason=r
        )
        msg = (
            f"Great news — that time is available! There's already a pending enquiry for that slot, "
            f"so to secure it for you I'll need a ${amount_str} deposit. "
            f"Whoever pays first gets the booking.\n\n"
            f"Full details and upload: {url}"
        )
        return append_doubles_sourcing_deposit_notice_if_needed(msg, booking_fields)

    # Overnight: manual review + deposit
    if reason == "overnight":
        return get_overnight_deposit_message(
            client_name=(client_name or "").strip() or "there",
            amount=amount,
            phone_number=phone_number,
            upload_url=upload_url,
            payment_reference=payment_reference,
        )

    # For confirmed bookings with full context (date/time/experience), use the full
    # mandatory template (header + booking line + PayID/account + upload link).
    if booking_fields:
        from templates.deposit_templates import build_mandatory_deposit_sms_message

        try:
            return build_mandatory_deposit_sms_message(
                phone_number=phone_number,
                deposit_amount=amount,
                booking_fields=booking_fields,
                client_name=client_name,
                upload_url=upload_url,
                reason=r,
                payment_reference=payment_reference,
            )
        except Exception as e:
            logger.warning("get_deposit_request_message: full mandatory template failed: %s", e)

    # Outcall deposit — fallback body when booking context is unavailable.
    if "outcall" in r:
        from templates.deposit_templates import build_deposit_message

        body = build_deposit_message(
            mandatory=True,
            followup=False,
            phone_number=phone_number,
            deposit_amount=amount,
            reason=r,
            outcall_address=outcall_address,
            payment_reference=payment_reference,
            booking_fields=booking_fields,
        )
        upload_line = ""
        if phone_number:
            try:
                from core.deposit_upload_tokens import resolve_deposit_upload_and_reference

                u, ref_from_token = resolve_deposit_upload_and_reference(phone_number, amount)
                if u:
                    upload_line = f"\n\nUpload deposit screenshot: {u}"
                if not payment_reference and ref_from_token:
                    payment_reference = ref_from_token
            except Exception as e:
                logger.warning("get_deposit_request_message: upload token failed: %s", e)
        if payment_reference:
            upload_line += f"\nPayment Reference: {payment_reference}"
        return body + upload_line

    # Other mandatory deposit (e.g. doubles_mff)
    url = get_deposit_payment_page_url(
        phone_number, mandatory=True, amount=amount, reason=r
    )
    msg = (
        f"Before I can lock in your booking a ${amount} deposit is required ({reason}).\n\n"
        f"Payment details and upload: {url}"
        + (f"\nUse payment reference: {payment_reference}" if payment_reference else "")
    )
    return append_doubles_sourcing_deposit_notice_if_needed(msg, booking_fields)


def get_overnight_deposit_message(client_name: str, amount: int = None, phone_number: str = None,
                                  upload_url: str = None, payment_reference: str | None = None) -> str:
    """Overnight booking: manual review notice + link to deposit page."""
    if amount is None:
        try:
            from core.rates_from_config import get_deposit_overnight
            amount = get_deposit_overnight()
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            amount = 200
    from templates.deposit_templates import get_deposit_payment_page_url

    name = (client_name or "").strip() or "there"
    url = (upload_url or "").strip() or get_deposit_payment_page_url(
        phone_number, mandatory=True, amount=amount, reason="overnight"
    )
    return (
        f"Thanks {name}, overnight bookings need manual review — I'll confirm personally once your "
        f"${amount} deposit is received.\n\n"
        f"Payment details and upload: {url}"
        + (f"\nUse payment reference: {payment_reference}" if payment_reference else "")
    )


def _format_slot_time(slot_time) -> str:
    """Format a slot time value (datetime, tuple, or other) to a display string."""
    if hasattr(slot_time, "strftime"):
        return slot_time.strftime("%I:%M%p")
    if isinstance(slot_time, tuple):
        hour, minute = slot_time
        period = "AM" if hour < 12 else "PM"
        display_hour = hour if hour <= 12 else hour - 12
        if display_hour == 0:
            display_hour = 12
        return f"{display_hour}:{minute:02d}{period}"
    return str(slot_time)


def _format_one_alternative_slot(slot) -> str:
    if isinstance(slot, dict):
        slot_date = slot.get("date")
        slot_time = slot.get("time")
        if not (slot_date and slot_time):
            return str(slot)
        if hasattr(slot_date, "strftime"):
            day = slot_date.day
            suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
            date_str = slot_date.strftime("%A, ") + f"{day}{suffix}" + slot_date.strftime(" %B")
        else:
            date_str = str(slot_date)
        time_str = _format_slot_time(slot_time)
        return f"{date_str} at {time_str}"
    return slot.strftime("%A, %d %B at %I:%M%p")


def format_alternatives_message(alternatives: list, _booking_fields: dict = None) -> str:
    """Format alternative time slots with enhanced formatting.

    Args:
        alternatives: List of datetime objects or dicts with date/time for alternative slots
        booking_fields: Optional booking fields for context

    Returns:
        Formatted alternatives message
    """
    if not alternatives:
        return ""
    return "\n".join(f"\u2022 {_format_one_alternative_slot(slot)}" for slot in alternatives)


def get_deposit_verified_message_outcall(client_name: str, deposit_amount: int, booking_fields: dict = None, total_cost: int = None, arrival_time_str: str = None) -> str:
    """Message after deposit is verified for outcall bookings with full booking details.

    Args:
        client_name: Client's name (use 'there' or empty if not known)
        deposit_amount: Actual deposit amount paid (e.g. 50 or 100)
        booking_fields: Full booking details for summary
        total_cost: Total booking cost
        arrival_time_str: Optional e.g. "8:55pm" for available-now outcall: "I'll aim to be there by {arrival_time_str}."

    Returns:
        Outcall deposit-verified message with full booking details
    """
    name = (client_name or "").strip() or "there"
    msg = f"Hi {name}, thanks for making payment of ${deposit_amount}. Your booking has now been confirmed.\n\n"
    
    # Add full booking summary if provided
    if booking_fields:
        msg += "**Booking Confirmed:**\n\n"
        summary = format_booking_summary(booking_fields)
        msg += summary + "\n\n"
        
        # Calculate and show amount outstanding
        if total_cost:
            amount_outstanding = max(0, total_cost - deposit_amount)
            msg += f"\U0001F4B0 Deposit Paid: ${deposit_amount}\n"
            msg += f"\U0001F4B0 Amount Outstanding: ${amount_outstanding}\n\n"
    
    msg += "I'll be in touch approx 1 hour prior to booking. "
    if arrival_time_str:
        msg += f"I'll aim to be there by {arrival_time_str}. "
    msg += "Looking forward to seeing you soon!"
    return msg


def _format_booking_summary_with_time_override(booking_fields: dict, time_display: str) -> str:
    """Format booking summary lines with a custom time display (e.g. arrival time for available-now outcall)."""
    lines: list[str] = []
    if booking_fields.get("date"):
        date_val = booking_fields["date"]
        date_str = _format_date_with_ordinal(date_val)
        lines.append(f"\U0001F4C5 Date: {date_str}")
    lines.append(f"\u23F0 Time: {time_display}")
    if booking_fields.get("duration"):
        lines.append(f"\u23F1\uFE0F Duration: {_duration_minutes_to_str(booking_fields['duration'])}")
    exp_line = _experience_line_from_fields(booking_fields)
    if exp_line:
        lines.append(exp_line)
    if (booking_fields.get("incall_outcall") or "").lower() == "outcall" and booking_fields.get("outcall_address"):
        lines.append(f"\U0001F4CD Location: {booking_fields['outcall_address']}")
    return "\n".join(lines)


def get_deposit_verified_message_outcall_available_now(
    client_name: str,
    deposit_amount: int,
    booking_fields: dict,
    total_cost: int,
    arrival_time_str: str | None = None,
) -> str:
    """Message after deposit is verified for available-now outcall: full summary + amount outstanding.

    Args:
        client_name: Client's name
        deposit_amount: Deposit amount paid
        booking_fields: Booking details for the summary block
        total_cost: Total booking cost (from Rates)
        arrival_time_str: Estimated arrival time e.g. "8:55pm"

    Returns:
        Full confirmation message with summary and amount outstanding
    """
    name = (client_name or "").strip() or "there"
    amount_outstanding = max(0, (total_cost or 0) - deposit_amount)
    time_display = arrival_time_str if arrival_time_str else "ASAP"
    summary_block = _format_booking_summary_with_time_override(booking_fields, time_display)
    msg = (
        f"Hi {name}, thanks for making payment of ${deposit_amount}. "
        "Your booking has now been confirmed. The estimated time of my arrival should now be showing below:\n\n"
    )
    msg += summary_block + "\n\n"
    msg += f"\U0001F4B0 Total: ${amount_outstanding} amount outstanding.\n\n"
    msg += "I'll see you soon babe looking forward to seeing you x"
    return msg


def get_deposit_verified_message_incall(client_name: str, deposit_amount: int, booking_fields: dict = None, total_cost: int = None) -> str:
    """Message after deposit is verified for incall bookings with full booking details.

    Args:
        client_name: Client's name
        deposit_amount: Actual deposit amount paid (e.g. 50 or 100)
        booking_fields: Full booking details for summary
        total_cost: Total booking cost

    Returns:
        Incall deposit-verified message with booking confirmation
    """
    name = (client_name or "").strip() or "there"
    msg = "\u2705 **Booking Confirmed!**\n\n"
    msg += f"Thanks {name} for paying ${deposit_amount} and sending confirmation of payment.\n"
    msg += "I have now marked your booking as 100% definite.\n\n"
    
    # Add full booking summary if provided
    if booking_fields:
        summary = format_booking_summary(booking_fields)
        msg += summary + "\n\n"
        
        # Calculate and show amount outstanding
        if total_cost:
            amount_outstanding = max(0, total_cost - deposit_amount)
            msg += f"\U0001F4B0 Deposit Paid: ${deposit_amount}\n"
            msg += f"\U0001F4B0 Amount Outstanding: ${amount_outstanding}\n\n"
    
    msg += "I'll send you access details (intercom & room info) approx 1 hour prior to your booking.\n"
    msg += "Looking forward to seeing you soon \u2764\uFE0F"
    return msg


def get_conflict_alternatives_message(alternatives: list, booking_fields: dict = None,
                                      webform_url: str = "") -> str:
    """Get enhanced message when requested time conflicts with calendar.

    Args:
        alternatives: List of alternative datetime slots or dicts with date/time
        booking_fields: Optional booking fields to provide context
        webform_url: Optional secure booking webform URL

    Returns:
        Enhanced conflict message with alternatives
    """
    if not alternatives:
        return "Sorry, that time is already booked and I couldn't find any nearby alternatives. Could you suggest another time?"

    # Enhanced formatting with better organization
    alt_text = format_alternatives_message(alternatives, booking_fields)
    
    # Check if alternatives are same-day
    same_day_count = 0
    if booking_fields and booking_fields.get('date'):
        requested_date = booking_fields['date']
        for alt in alternatives:
            if isinstance(alt, dict):
                alt_date = alt.get('date')
            else:
                alt_date = alt.date() if hasattr(alt, 'date') else None
            
            if alt_date == requested_date:
                same_day_count += 1
    
    message = "\u274C That time is already booked.\n\n"
    
    if same_day_count > 0:
        from utils.timezone import get_today_or_tonight
        time_ref = get_today_or_tonight()
        message += f"\u2705 I'm available {same_day_count} other time{'s' if same_day_count > 1 else ''} {time_ref}:\n\n"
    else:
        message += "\u2705 Here are some alternative times:\n\n"
    
    message += f"{alt_text}\n\n"
    message += "Let me know if any of these work for you. Or to make a booking please advise of DATE, TIME, Duration and Experience type (e.g. 8pm 1 hr PSE) so I can check availability for you."
    if webform_url:
        message += f"\n\nOr you can fill out this webform: {webform_url}"

    return message


def get_outcall_unavailable_message(
    city: str,
    hotel_name: str = "",
    webform_url: str = "",
    client_name: str = "",
    distance_km: float = 0.0,
) -> str:
    """Get message when outcall location is too far from the escort's current location.

    Args:
        city: City name
        hotel_name: Escort's current hotel/location name (e.g. "Sofitel")
        webform_url: Booking webform URL for the client
        client_name: Client name for greeting
        distance_km: Actual distance from escort's location in km

    Returns:
        Outcall unavailable message
    """
    name_part = f"Sorry {client_name} " if (client_name and str(client_name).strip()) else "Sorry "
    if distance_km > 0:
        distance_part = f"your address is {distance_km:.1f}km away which is outside the allowed 15km radius for an outcall."
    else:
        distance_part = "your address is outside of the allowed 15km radius for an outcall."
    base_hint = ""
    _h = (hotel_name or "").strip()
    _c = (city or "").strip()
    if _h and _c:
        base_hint = f" I'm currently incall near {_h}, {_c}."
    elif _c:
        base_hint = f" I'm currently based in {_c}."
    elif _h:
        base_hint = f" I'm currently incall near {_h}."
    msg = f"{name_part}{distance_part}{base_hint}\n\n"
    msg += "If you would like to visit me instead please fill in my booking webform: "
    if webform_url:
        msg += webform_url
    else:
        msg += "(link in my profile)"
    return msg


def get_touring_info_message(start_date: str = "", end_date: str = "", city: str = "", hotel_name: str = "", address: str = "") -> str:
    """Format touring information for client display.
    
    Creates a message showing when the escort is touring and where they're staying.
    Called from handlers to inform clients about future tour plans.
    
    Args:
        start_date: Tour start date (YYYY-MM-DD format)
        end_date: Tour end date (YYYY-MM-DD format)
        city: City name for touring
        hotel_name: Hotel/accommodation name
        address: Full address of accommodation
        
    Returns:
        Formatted touring information message, or empty string if no touring data
    """
    # If no touring data provided, try to get from config
    if not any([start_date, end_date, city, hotel_name, address]):
        try:
            from config import get_touring_australia
            touring = get_touring_australia()
            start_date = touring.get('tour_start_date', '')
            end_date = touring.get('tour_end_date', '')
            city = touring.get('tour_city', '')
            hotel_name = touring.get('tour_hotel_name', '')
            address = touring.get('tour_address', '')
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            return ""
    
    # Return empty string if still no data
    if not any([start_date, end_date, city, hotel_name]):
        return ""
    
    lines = []
    lines.append("\u2708\uFE0F **I'm Touring!**")
    
    # Format dates
    if start_date and end_date:
        try:
            # Parse and format dates for client display
            from datetime import datetime
            start_obj = datetime.strptime(start_date, "%Y-%m-%d")
            end_obj = datetime.strptime(end_date, "%Y-%m-%d")
            date_str = f"{start_obj.strftime('%b %d')} - {end_obj.strftime('%b %d')}"
            lines.append(f"\U0001F4C5 **Dates:** {date_str}")
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            if start_date or end_date:
                lines.append(f"\U0001F4C5 **Dates:** {start_date} to {end_date}")
    elif start_date:
        lines.append(f"\U0001F4C5 **From:** {start_date}")
    elif end_date:
        lines.append(f"\U0001F4C5 **Until:** {end_date}")
    
    # Format location
    if city:
        lines.append(f"\U0001F4CD **Location:** {city}")
    
    # Format accommodation details
    if hotel_name or address:
        lines.append("\U0001F3E8 **Staying at:**")
        if hotel_name:
            lines.append(f"  {hotel_name}")
        if address:
            lines.append(f"  {address}")
    
    lines.append("\nDuring this time I'll be available for bookings in this location!")
    
    return "\n".join(lines)


def is_currently_touring() -> bool:
    """Check if escort is currently touring based on today's date.
    
    Returns:
        True if current date falls within tour dates, False otherwise
    """
    try:
        from datetime import datetime

        from config import get_touring_australia
        from utils.timezone import get_current_datetime
        
        touring = get_touring_australia()
        start_date_str = touring.get('tour_start_date', '').strip()
        end_date_str = touring.get('tour_end_date', '').strip()
        
        if not (start_date_str and end_date_str):
            return False
        
        try:
            today = get_current_datetime().date()
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
            return start_date <= today <= end_date
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            return False
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        return False

def get_deposit_verified_booking_confirmation(client_name: str, booking_fields: dict, total_cost: int, 
                                              actual_deposit_amount: int, is_mandatory_deposit: bool = True) -> str:
    """
    Generate a comprehensive booking confirmation screen after deposit is verified.
    
    This screen is displayed after Google Vision confirms the deposit payment screenshot.
    Shows all booking details, amount paid, and amount outstanding.
    
    Works for:
    - Mandatory deposits (outcall, overnight, doubles_mff)
    - Non-mandatory deposits (incall)
    - Both incall and outcall bookings
    
    Args:
        client_name: Client's name
        booking_fields: Dict with booking details (date, time, duration, location, experience_type, incall_outcall)
        total_cost: Total booking cost from Rates page
        actual_deposit_amount: Actual amount detected/paid from Vision verification (e.g. 50 or 100)
        is_mandatory_deposit: True if mandatory, False if optional
        
    Returns:
        Formatted booking confirmation screen with all details
    """
    # Calculate amount outstanding
    amount_outstanding = max(0, total_cost - actual_deposit_amount)
    
    # Format date
    booking_date = booking_fields.get('date')
    if booking_date is not None and hasattr(booking_date, 'strftime'):
        date_str = _format_date_with_ordinal(booking_date)
    else:
        date_str = str(booking_date)
    
    # Format time
    time_tuple = booking_fields.get('time')
    if isinstance(time_tuple, (list, tuple)) and len(time_tuple) >= 2:
        time_str = f"{time_tuple[0]:02d}:{time_tuple[1]:02d}"
    else:
        time_str = str(time_tuple)
    
    # Format duration
    duration_min = booking_fields.get('duration', 60)
    if duration_min >= 60:
        hours = duration_min // 60
        mins = duration_min % 60
        if mins > 0:
            duration_str = f"{hours}h {mins}min"
        else:
            duration_str = f"{hours}h"
    else:
        duration_str = f"{duration_min}min"
    
    # Format experience
    exp_raw = (booking_fields.get('experience_type') or '').strip()
    if booking_fields.get('booking_type') == 'overnight' or (exp_raw and 'overnight' in exp_raw.lower()):
        exp_str = "Overnight"
    else:
        exp_str = _display_experience_label(exp_raw) if exp_raw else ""
    
    # Format location
    incall_outcall = (booking_fields.get('incall_outcall') or 'incall').lower()
    if incall_outcall == 'outcall':
        location_str = booking_fields.get('outcall_address', 'Location TBA')
    else:
        # For incall, try to get location from config
        try:
            from config import get_current_incall_location
            loc = get_current_incall_location() or {}
            city = loc.get('city') or 'Location'
            hotel = loc.get('hotel_name') or ''
            if city and hotel:
                location_str = f"{city} - {hotel}"
            elif city:
                location_str = city
            else:
                location_str = "Incall Location"
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            location_str = "Incall Location"
    
    # Build confirmation screen
    lines = []
    lines.append("\u2550" * 50)
    lines.append("\u2705 BOOKING CONFIRMED!")
    lines.append("\u2550" * 50)
    lines.append("")
    _cn = (client_name or "").strip()
    if _cn:
        lines.append(f"Hi {_cn},")
        lines.append("")
    
    # Booking details section
    lines.append("\U0001F4CB **BOOKING DETAILS**")
    lines.append("")
    lines.append(f"\U0001F4C5 Date:       {date_str}")
    lines.append(f"\U0001F550 Time:       {time_str}")
    lines.append(f"\u23F1\uFE0F  Duration:    {duration_str}")
    if exp_str:
        lines.append(f"\U0001F3AD Experience: {exp_str}")
    lines.append(f"\U0001F4CD Location:   {location_str}")
    lines.append("")
    
    # Payment summary section
    lines.append("\U0001F4B0 **PAYMENT SUMMARY**")
    lines.append("")
    lines.append(f"Total Cost:        ${total_cost}")
    lines.append(f"Deposit Paid:      -${actual_deposit_amount}")
    lines.append("\u2500" * 40)
    lines.append(f"Amount Outstanding: ${amount_outstanding}")
    lines.append("")
    
    # Additional info based on booking type
    if incall_outcall == 'outcall':
        lines.append("\U0001F4CC **OUTCALL INFORMATION**")
        lines.append("")
        lines.append("I'll be in touch approx 1 hour before your booking")
        lines.append("to confirm my arrival time and final details.")
        lines.append("")
    else:
        lines.append("\U0001F4CC **INCALL INFORMATION**")
        lines.append("")
        lines.append("I'll send you access details (intercom code,")
        lines.append("room number) approx 1 hour before your booking.")
        lines.append("")
    
    # Deposit type info
    if is_mandatory_deposit:
        lines.append("\u2713 Deposit verified and booking is now 100% confirmed.")
    else:
        lines.append("\u2713 Optional deposit paid - thank you!")
        lines.append("  Your booking is now 100% confirmed.")
    
    lines.append("")
    lines.append("Looking forward to seeing you! \U0001F495")
    lines.append("\u2550" * 50)
    
    return "\n".join(lines)
