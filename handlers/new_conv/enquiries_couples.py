# ruff: noqa: F401,F403,F405
from handlers.new_conv._shared import *  # noqa: F401,F403
from typing import Any

import logging

from utils.log_sanitize import LOG_SUPPRESSED_FMT

logger = logging.getLogger("adella_chatbot.enquiries")


def handle_msog_enquiry(context: dict[str, Any]) -> dict[str, Any]:
    """MSOG enquiry — quick-close webform redirect, no booking collection."""
    from config import get_base_url
    from templates.greetings import extract_client_name

    phone_number = context.get("phone_number", "")
    state = context.get("state") or {}
    client_name = state.get("client_name") or extract_client_name(context.get("message", ""))
    name_part = f" {client_name}" if client_name else ""

    base_url = get_base_url()
    try:
        from core.webform_security import get_webform_url
        webform_url = get_webform_url(phone_number)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        webform_url = f"{base_url}/booking"

    message = (
        f"Hi{name_part}! MSOG (multiple shots on goal) is available as an add-on for most bookings.\n\n"
        f"You can find full details on my experience page:\n{base_url}/experience\n\n"
        f"To book, use my webform and mention MSOG in the notes:\n{webform_url}"
    )
    return {"messages": [message], "new_state": None, "actions": []}


def _couples_incall_staying_at_string(location: dict) -> str:
    """Full incall location phrase: 'I'm located at [hotel], [address] [city]'."""
    city = (location.get("city") or "").strip()
    hotel_name = (location.get("hotel_name") or location.get("display_name") or "").strip()
    address = (location.get("address") or "").strip()
    hotel_addr = " ".join(p for p in [hotel_name, address] if p)
    city_already_in_addr = city and city.lower() in hotel_addr.lower()
    if hotel_addr and city and not city_already_in_addr:
        return f"I'm located at {hotel_addr} {city}"
    if hotel_addr:
        return f"I'm located at {hotel_addr}"
    if city:
        return f"I'm currently in {city}"
    return ""


def handle_couples_enquiry(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle couples booking enquiry (client + their partner).

    Doubles/threesome (``doubles_enquiry``) uses a separate handler and templates — do not
    merge behaviour here.
    """
    state = context["state"]
    state_manager = context["state_manager"]
    phone_number = context["phone_number"]
    message_text = context.get("message", "")

    if state.get("first_contact_sent"):
        from handlers.new_conv.booking_pivot import clear_incompatible_flow_for_special_booking_pivot
        from utils.time_parser import is_immediate_request

        bt = (state.get("booking_type") or "").strip().lower()
        if bt != "couples_booking":
            clear_incompatible_flow_for_special_booking_pivot(state_manager, phone_number)
            context = dict(context)
            context["state"] = state_manager.get_state(phone_number) or state
            state = context["state"]
        else:
            if is_immediate_request(message_text):
                state_manager.update_fields(
                    phone_number,
                    {
                        "available_now_requested": True,
                        "incall_outcall": "outcall" if _has_outcall_intent(message_text) else "incall",
                    },
                )
                context = dict(context)
                context["state"] = state_manager.get_state(phone_number) or state

            from handlers import booking_collection

            return booking_collection.handle_provide_field(context) or {
                "messages": [],
                "new_state": "COLLECTING",
                "actions": [],
            }

    from config import get_cbd_label_for_messages, get_current_incall_location, get_effective_booking_city, get_profile_url
    from core.rates_from_config import get_deposit_mff_pair, get_surcharge
    from core.webform_security import get_webform_url
    from services.calendar_service import check_conflict, find_alternative_slots
    from templates.booking_collection_messages import format_yes_im_free_at_line
    from templates.special_bookings import build_couples_available_now_message
    from utils.availability_slots import format_slot_display_short, get_next_available_time_slots, weekday_abbrev_3
    from utils.time_parser import infer_requested_datetime_for_booking, is_immediate_request
    from utils.timezone import get_current_datetime

    city = get_effective_booking_city()
    cbd = get_cbd_label_for_messages(city)
    profile_url = (get_profile_url() or "").strip()
    location = get_current_incall_location() or {}
    webform_url = get_webform_url(phone_number)
    client_name = state.get("client_name") or greetings.extract_client_name(message_text)
    name_part = f" {client_name}" if client_name else ""

    _msg_lower = message_text.lower()
    _outcall_kws = (
        "outcall", "out call", "my place", "my hotel", "my address", "my location",
        "my apartment", "my room", "my airbnb", "my unit", "my suite",
        "come to me", "come to my", "come over", "come see me", "come and see me",
        "visit me", "staying at", "i'm at", "im at", "i am at",
        "located at", "can you come", "you come to",
    )
    is_outcall = any(kw in _msg_lower for kw in _outcall_kws)

    now = get_current_datetime()
    deposit = get_deposit_mff_pair()
    try:
        surcharge = get_surcharge()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        surcharge = 100

    extracted_dt = None
    try:
        extracted_dt = infer_requested_datetime_for_booking(message_text, now=now)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
    immediate_request = is_immediate_request(message_text)

    opener = f"Hi{name_part} that sounds amazing, couples bookings are one of my favourites!"
    webform_line = (
        "I STRONGLY recommend booking through my webform — just select 'Couples MFF' as the experience type:\n"
        f"{webform_url}"
    )
    closing_incall = "What time works for you, and how long would you like to book for?"
    closing_outcall = "What time works for you, and what's your address?"

    booking_details: dict | None = None
    specific_free = False
    specific_busy = False
    inferred_date = None
    inferred_hour = None
    inferred_minute = None
    lines_fmt: list[str] = []
    slot_dts_for_offer: list = []

    if extracted_dt is not None:
        inferred_date = extracted_dt.date()
        inferred_hour = extracted_dt.hour
        inferred_minute = extracted_dt.minute
        booking_details = {
            "date": inferred_date.strftime("%Y-%m-%d"),
            "time": (inferred_hour, inferred_minute),
            "duration": 60,
            "incall_outcall": "outcall" if is_outcall else "incall",
            "experience_type": "couples_mff",
            "booking_type": "couples_booking",
        }
        try:
            conflict_type, _ = check_conflict(booking_details)
            is_available = conflict_type == "none"
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            is_available = False

        if is_available:
            specific_free = True
        else:
            specific_busy = True
            try:
                slot_dts_for_offer = list(find_alternative_slots(booking_details, max_results=3))
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                slot_dts_for_offer = []
            if not slot_dts_for_offer:
                try:
                    _fallback_slots = get_next_available_time_slots(
                        now,
                        num_slots=3,
                        check_calendar=True,
                        persist_slots_for_phone=phone_number,
                        persist_slots_state_manager=state_manager,
                    )
                    slot_dts_for_offer = [dt for dt, _ in _fallback_slots[:3]]
                except Exception as e:
                    logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                    slot_dts_for_offer = []
            for dt in slot_dts_for_offer[:3]:
                try:
                    lines_fmt.append(format_slot_display_short(dt))
                except Exception as e:
                    logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                    try:
                        if hasattr(dt, "strftime"):
                            lines_fmt.append(
                                f"{weekday_abbrev_3(dt)} {dt.strftime('%d %b %I:%M%p').replace(' 0', ' ')}"
                            )
                    except Exception as e:
                        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                        lines_fmt.append(str(dt))

    time_slots: list = []
    if extracted_dt is None:
        try:
            time_slots = get_next_available_time_slots(
                now,
                num_slots=3,
                check_calendar=True,
                persist_slots_for_phone=phone_number,
                persist_slots_state_manager=state_manager,
            )
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            time_slots = []

    if extracted_dt is None and immediate_request:
        message = build_couples_available_now_message(
            client_name=client_name or "",
            time_slots=time_slots,
            profile_url=profile_url,
            webform_url=webform_url,
            city=city,
            hotel_name=location.get("hotel_name", ""),
            address=location.get("address", ""),
            is_outcall=is_outcall,
            surcharge=surcharge,
            deposit=deposit,
        )
        updates: dict[str, Any] = {
            "first_contact_sent": True,
            "booking_type": "couples_booking",
            "experience_type": "couples_mff",
            "incall_outcall": "outcall" if is_outcall else "incall",
            "available_now_requested": True,
        }
        if client_name:
            updates["client_name"] = client_name
        state_manager.update_fields(phone_number, updates)
        return {"messages": [message], "new_state": "COLLECTING", "actions": []}

    slots_section = ""
    if not specific_free and not specific_busy and time_slots:
        label = get_availability_window_label(time_slots, now=now)
        slots_text = "\n".join(f"\u2022 {slot_str}" for _, slot_str in time_slots)
        slots_section = f"Here are the times I have available {label}:\n\n{slots_text}"

    parts: list[str] = [opener]
    if specific_free and inferred_date is not None and inferred_hour is not None:
        parts.append(format_yes_im_free_at_line(inferred_date, (inferred_hour, inferred_minute)))
    elif specific_busy:
        if lines_fmt:
            parts.append("❌ Unfortunately that time isn't available. I have these time slots open:")
            parts.append("\n".join(f"\u2022 {line}" for line in lines_fmt))
        else:
            parts.append("❌ Unfortunately that time isn't available. Please contact me for my closest available times.")
    elif slots_section:
        parts.append(slots_section)

    if profile_url:
        parts.append(profile_url)

    if is_outcall:
        parts.append(
            f"I only do outcalls to hotels or apartments within 15km of {cbd}. "
            f"There is a ${surcharge} surcharge + ${deposit} deposit required for all couples bookings."
        )
    else:
        location_phrase = _couples_incall_staying_at_string(location)
        if location_phrase:
            parts.append(
                f"{location_phrase}. A ${deposit} mandatory deposit "
                "is required for all couples bookings."
            )
        else:
            parts.append(f"A ${deposit} mandatory deposit is required for all couples bookings.")

    parts.append(webform_line)
    if specific_free:
        closing = "How long would you like to book for, and what's your address?" if is_outcall else "How long would you like to book for?"
    else:
        closing = closing_outcall if is_outcall else closing_incall
    parts.append(closing)

    message = "\n\n".join(parts)

    updates: dict[str, Any] = {
        "first_contact_sent": True,
        "booking_type": "couples_booking",
        "experience_type": "couples_mff",
        "incall_outcall": "outcall" if is_outcall else "incall",
    }
    if client_name:
        updates["client_name"] = client_name
    if specific_free and inferred_date is not None and inferred_hour is not None:
        updates["date"] = inferred_date.strftime("%Y-%m-%d")
        updates["time"] = (inferred_hour, inferred_minute)
    if specific_busy and slot_dts_for_offer:
        _offer_from = slot_dts_for_offer
        try:
            updates["offered_slot_hours"] = [dt.hour for dt in _offer_from[:3]]
            updates["offered_slot_minutes"] = [dt.minute for dt in _offer_from[:3]]
            updates["offered_slot_date"] = (
                _offer_from[0].strftime("%Y-%m-%d") if _offer_from else now.strftime("%Y-%m-%d")
            )
        except (AttributeError, TypeError, IndexError) as e:
            logger.warning("couples enquiry: could not derive offered_slot_*: %s", e)

    state_manager.update_fields(phone_number, updates)
    return {"messages": [message], "new_state": "COLLECTING", "actions": []}
