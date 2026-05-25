# ruff: noqa: F401,F403,F405
from handlers.new_conv._shared import *  # noqa: F401,F403
from typing import Any

import logging

from utils.log_sanitize import LOG_SUPPRESSED_FMT

logger = logging.getLogger("adella_chatbot.enquiries")


def _safe_format_dinner_date_rates_text() -> str:
    """Delegate to core (never raises)."""
    from core.rates_from_config import safe_format_dinner_date_rates_text
    return safe_format_dinner_date_rates_text()


def handle_dinner_date_enquiry(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle dinner date booking enquiry.

    Sends ONE SMS on this turn. State is persisted for COLLECTING; the client's next reply
    continues through ``handle_provide_field``.
    """
    state = context.get("state") or {}
    cs = (state.get("current_state") or "").strip().upper()
    if cs == "COLLECTING" and state.get("first_contact_sent") and (
        (state.get("booking_type") or "").strip().lower() == "dinner_date"
        or (state.get("experience_type") or "").strip().lower() in ("dinner date", "dinner_date")
    ):
        from handlers import booking_collection
        return booking_collection.handle_provide_field(context)

    if cs == "COLLECTING" and state.get("first_contact_sent"):
        bt0 = (state.get("booking_type") or "").strip().lower()
        exp0 = (state.get("experience_type") or "").strip().lower()
        if bt0 in ("couples_booking", "doubles_mff") or exp0 in ("couples_mff", "doubles_mff", "Doubles MMF"):
            from handlers.new_conv.booking_pivot import clear_incompatible_flow_for_special_booking_pivot
            clear_incompatible_flow_for_special_booking_pivot(
                context["state_manager"], context["phone_number"]
            )
            context = dict(context)
            context["state"] = context["state_manager"].get_state(context["phone_number"]) or state

    try:
        return _handle_dinner_date_enquiry_impl(context)
    except Exception as e:
        logger.exception("handle_dinner_date_enquiry failed: %s", e)
        return _dinner_date_enquiry_safe_fallback(context)


def _dinner_enquiry_try_unavailable_for_inferred_time(
    *,
    raw_message: str,
    client_name: str,
    city: str,
    profile_url: str,
    webform_url: str,
    phone_number: str = "",
    state_manager: Any = None,
) -> tuple[str, dict[str, Any]] | None:
    """
    When a clock time can be inferred, check the calendar. If that slot is busy,
    build the ❌ + alternatives dinner SMS.
    """
    from core.rates_from_config import get_deposit_outcall
    from services.calendar_service import check_conflict, find_alternative_slots
    from templates.special_bookings import build_dinner_date_requested_time_unavailable_full_message
    from utils.availability_slots import format_slot_display_short, get_next_available_time_slots, weekday_abbrev_3
    from utils.dinner_date import DINNER_DURATION_MINUTES, dinner_slot_fits_window
    from utils.time_parser import infer_requested_datetime_for_booking
    from utils.timezone import get_current_datetime

    now = get_current_datetime()
    extracted_dt = infer_requested_datetime_for_booking(raw_message, now=now)
    if extracted_dt is None:
        return None

    booking_details = {
        "date": extracted_dt.date().strftime("%Y-%m-%d"),
        "time": (extracted_dt.hour, extracted_dt.minute),
        "duration": DINNER_DURATION_MINUTES,
        "incall_outcall": "outcall",
    }
    try:
        conflict_type, _ = check_conflict(booking_details)
        is_busy = conflict_type != "none"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        is_busy = True

    if not is_busy:
        return None

    time_slots: list = []
    try:
        time_slots = get_next_available_time_slots(
            now,
            num_slots=3,
            check_calendar=True,
            booking_type="dinner_date",
            persist_slots_for_phone=phone_number or None,
            persist_slots_state_manager=state_manager,
        )
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)

    slot_dts_for_offer: list = []
    try:
        slot_dts_for_offer = list(
            find_alternative_slots(
                booking_details, max_results=6, same_day_only=False, max_hours_from_requested=72.0,
            )
        )
        slot_dts_for_offer = [
            dt for dt in slot_dts_for_offer
            if dinner_slot_fits_window(dt, DINNER_DURATION_MINUTES)
        ][:3]
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        slot_dts_for_offer = []
    if not slot_dts_for_offer and time_slots:
        try:
            slot_dts_for_offer = [dt for dt, _ in time_slots[:3]]
        except (TypeError, ValueError):
            slot_dts_for_offer = []

    lines_fmt: list[str] = []
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

    rates_text = _safe_format_dinner_date_rates_text()
    try:
        deposit = int(get_deposit_outcall())
    except (TypeError, ValueError):
        deposit = 100

    msg = build_dinner_date_requested_time_unavailable_full_message(
        client_name=client_name or "",
        slot_display_lines=lines_fmt,
        rates_text=rates_text,
        profile_url=profile_url,
        webform_url=webform_url,
        city=city,
        requested_time=extracted_dt,
        deposit=deposit,
    )

    _offer_from = slot_dts_for_offer[:3]
    if not _offer_from:
        try:
            _offer_from = [dt for dt, _ in time_slots[:3]]
        except (TypeError, ValueError):
            _offer_from = []

    try:
        offered_slot_hours = [dt.hour for dt in _offer_from[:3]]
        offered_slot_minutes = [dt.minute for dt in _offer_from[:3]]
        offered_slot_date = _offer_from[0].strftime("%Y-%m-%d") if _offer_from else now.strftime("%Y-%m-%d")
    except (AttributeError, TypeError, IndexError) as e:
        logger.warning("dinner safe unavailable: could not derive offered_slot_* from %s: %s", _offer_from, e)
        offered_slot_hours = []
        offered_slot_minutes = []
        offered_slot_date = now.strftime("%Y-%m-%d")

    updates: dict[str, Any] = {
        "first_contact_sent": True,
        "booking_type": "dinner_date",
        "experience_type": "Dinner Date",
        "duration": 120,
        "incall_outcall": "outcall",
        "offered_slot_hours": offered_slot_hours,
        "offered_slot_minutes": offered_slot_minutes,
        "offered_slot_date": offered_slot_date,
    }
    if client_name:
        updates["client_name"] = client_name

    return msg, updates


def _dinner_date_enquiry_safe_fallback(context: dict[str, Any]) -> dict[str, Any]:
    """
    If the full dinner handler fails, still send a valid dinner-date intro and continue
    COLLECTING — never the generic ENQUIRY template.
    """
    phone_number = context.get("phone_number") or ""
    raw_message = (context.get("message") or "").strip()
    state = context.get("state") or {}

    from config import get_cbd_label_for_messages, get_effective_booking_city, get_profile_url
    from core.rates_from_config import get_deposit_outcall
    from core.webform_security import get_webform_url

    city = get_effective_booking_city()
    cbd = get_cbd_label_for_messages(city)
    profile_url = (get_profile_url() or "").strip()
    client_name = state.get("client_name") or greetings.extract_client_name(raw_message)
    webform_url = get_webform_url(phone_number)
    deposit = get_deposit_outcall()

    try:
        busy_pair = _dinner_enquiry_try_unavailable_for_inferred_time(
            raw_message=raw_message,
            client_name=client_name or "",
            city=city,
            profile_url=profile_url,
            webform_url=webform_url,
            phone_number=phone_number,
            state_manager=context.get("state_manager"),
        )
        if busy_pair is not None:
            msg, updates = busy_pair
            return _dinner_enquiry_apply_state_and_return_single_message(context, msg, updates)
    except Exception as e:
        logger.exception("dinner safe fallback: unavailable-time branch failed: %s", e)

    name_part = f" {client_name}" if client_name else ""
    rates_text = _safe_format_dinner_date_rates_text()
    opener = f"Hi{name_part} I love dinner dates here is what you need to know:\n\n"
    parts = [
        opener + f"{rates_text}\n\nDinner/social time counts toward the booking. You cover the cost of food and drinks separately"
    ]
    if profile_url:
        parts.append(profile_url)
    parts.append(f"I only eat at restaurants within 15km of {cbd}. There is a mandatory ${deposit} deposit also required.")
    parts.append(f"I STRONGLY recommend booking through my webform: {webform_url}")
    parts.append("Which time works for you, and where do you want to go to eat?")
    msg = "\n\n".join(parts)
    updates: dict[str, Any] = {
        "first_contact_sent": True,
        "booking_type": "dinner_date",
        "experience_type": "Dinner Date",
        "duration": 120,
        "incall_outcall": "outcall",
    }
    if client_name:
        updates["client_name"] = client_name
    return _dinner_enquiry_apply_state_and_return_single_message(context, msg, updates)


def _handle_dinner_date_enquiry_impl(context: dict[str, Any]) -> dict[str, Any]:
    state = context.get("state") or {}
    phone_number = context['phone_number']
    state_manager = context['state_manager']
    raw_message = (context.get('message') or '').strip()

    from config import get_cbd_label_for_messages, get_effective_booking_city, get_profile_url
    from core.rates_from_config import get_deposit_outcall
    from core.webform_security import get_webform_url
    from services.calendar_service import check_conflict
    from templates.booking_collection_messages import format_yes_im_free_at_line
    from utils.availability_slots import get_next_available_time_slots
    from utils.dinner_date import DINNER_DURATION_MINUTES, DINNER_WINDOW_START, bump_to_next_dinner_candidate, dinner_slot_fits_window
    from utils.time_parser import infer_requested_datetime_for_booking
    from utils.timezone import get_current_datetime

    city = get_effective_booking_city()
    cbd = get_cbd_label_for_messages(city)
    profile_url = (get_profile_url() or '').strip()
    client_name = state.get('client_name') or greetings.extract_client_name(raw_message)
    name_part = f" {client_name}" if client_name else ""
    webform_url = get_webform_url(phone_number)

    now = get_current_datetime()
    extracted_dt = infer_requested_datetime_for_booking(raw_message, now=now)

    free_line = ""
    dinner_requested_time_busy = False
    booking_details: dict | None = None
    date_str = None
    time_tuple = None

    if extracted_dt is not None:
        inferred_date = extracted_dt.date()
        inferred_hour = extracted_dt.hour
        inferred_minute = extracted_dt.minute
        booking_details = {
            'date': inferred_date.strftime('%Y-%m-%d'),
            'time': (inferred_hour, inferred_minute),
            'duration': DINNER_DURATION_MINUTES,
            'incall_outcall': 'outcall',
        }
        try:
            conflict_type, _ = check_conflict(booking_details)
            is_available = conflict_type == 'none'
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            is_available = False

        if is_available:
            free_line = " " + format_yes_im_free_at_line(inferred_date, (inferred_hour, inferred_minute))
            date_str = booking_details['date']
            time_tuple = (inferred_hour, inferred_minute)
        else:
            dinner_requested_time_busy = True

    time_slots = []
    if not free_line:
        preferred_date = _infer_preferred_date(raw_message, now)
        if preferred_date is not None:
            try:
                from datetime import datetime as _dt
                tz = now.tzinfo
                _at_dinner = _dt.combine(preferred_date, DINNER_WINDOW_START)
                if tz:
                    try:
                        _at_dinner = tz.localize(_at_dinner.replace(tzinfo=None))
                    except Exception as e:
                        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                        _at_dinner = _at_dinner.replace(tzinfo=tz)
                start_from = _at_dinner
                if start_from < now:
                    start_from = bump_to_next_dinner_candidate(
                        now + timedelta(minutes=30), DINNER_DURATION_MINUTES
                    )
                time_slots = get_next_available_time_slots(
                    now, num_slots=3, check_calendar=True,
                    booking_type="dinner_date", start_from=start_from, end_by=None,
                    persist_slots_for_phone=phone_number,
                    persist_slots_state_manager=state_manager,
                )
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                time_slots = []
        if not time_slots:
            try:
                time_slots = get_next_available_time_slots(
                    now,
                    num_slots=3,
                    check_calendar=True,
                    booking_type="dinner_date",
                    persist_slots_for_phone=phone_number,
                    persist_slots_state_manager=state_manager,
                )
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                time_slots = []

    slot_dts_for_offer: list = []
    lines_fmt: list[str] = []

    if dinner_requested_time_busy and booking_details:
        from services.calendar_service import find_alternative_slots
        from utils.availability_slots import format_slot_display_short, weekday_abbrev_3

        try:
            slot_dts_for_offer = list(
                find_alternative_slots(
                    booking_details, max_results=6, same_day_only=False, max_hours_from_requested=72.0,
                )
            )
            slot_dts_for_offer = [
                dt for dt in slot_dts_for_offer
                if dinner_slot_fits_window(dt, DINNER_DURATION_MINUTES)
            ][:3]
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            slot_dts_for_offer = []
        if not slot_dts_for_offer and time_slots:
            try:
                slot_dts_for_offer = [dt for dt, _ in time_slots[:3]]
            except (TypeError, ValueError):
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

    slots_section = ""
    if not free_line and not dinner_requested_time_busy and time_slots:
        slots_text = "\n".join(f"\u2022 {slot_str}" for _, slot_str in time_slots)
        slots_section = f"Here are my closest available times:\n\n{slots_text}"

    rates_text = _safe_format_dinner_date_rates_text()
    deposit = get_deposit_outcall()

    if dinner_requested_time_busy and booking_details:
        from templates.special_bookings import build_dinner_date_requested_time_unavailable_full_message
        message = build_dinner_date_requested_time_unavailable_full_message(
            client_name=client_name or "",
            slot_display_lines=lines_fmt,
            rates_text=rates_text,
            profile_url=profile_url,
            webform_url=webform_url,
            city=city,
            requested_time=(extracted_dt if extracted_dt is not None else (booking_details or {}).get("time")),
            deposit=deposit,
        )
    else:
        opener = f"Hi{name_part} I love dinner dates{free_line} here is what you need to know:\n\n"
        parts = [
            opener + f"{rates_text}\n\nDinner/social time counts toward the booking. You cover the cost of food and drinks separately"
        ]
        if slots_section:
            parts.append(slots_section)
        if profile_url:
            parts.append(profile_url)
        parts.append(f"I only eat at restaurants within 15km of {cbd}. There is a mandatory ${deposit} deposit also required.")
        parts.append(f"I STRONGLY recommend booking through my webform: {webform_url}")
        parts.append("Where would you like to go to eat?" if free_line else "Which time works for you, and where do you want to go to eat?")
        message = "\n\n".join(parts)

    if dinner_requested_time_busy and slot_dts_for_offer:
        _offer_from = slot_dts_for_offer
    else:
        try:
            _offer_from = [dt for dt, _ in time_slots]
        except (TypeError, ValueError):
            _offer_from = []
    try:
        offered_slot_hours = [dt.hour for dt in _offer_from[:3]]
        offered_slot_minutes = [dt.minute for dt in _offer_from[:3]]
        if _offer_from:
            offered_slot_date = _offer_from[0].strftime("%Y-%m-%d")
        elif date_str:
            offered_slot_date = date_str
        else:
            offered_slot_date = now.strftime("%Y-%m-%d")
    except (AttributeError, TypeError, IndexError) as e:
        logger.warning("dinner enquiry: could not derive offered_slot_* from %s: %s", _offer_from, e)
        offered_slot_hours = []
        offered_slot_minutes = []
        offered_slot_date = date_str or now.strftime("%Y-%m-%d")
    if date_str:
        offered_slot_date = date_str
    updates = {
        'first_contact_sent': True,
        'booking_type': 'dinner_date',
        'experience_type': 'Dinner Date',
        'duration': 120,
        'incall_outcall': 'outcall',
        'offered_slot_hours': offered_slot_hours,
        'offered_slot_minutes': offered_slot_minutes,
        'offered_slot_date': offered_slot_date,
    }
    if date_str:
        updates['date'] = date_str
    if time_tuple is not None:
        updates['time'] = time_tuple
    if client_name:
        updates['client_name'] = client_name

    return _dinner_enquiry_apply_state_and_return_single_message(context, message, updates)


def _dinner_enquiry_apply_state_and_return_single_message(
    context: dict[str, Any],
    message: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Persist dinner-enquiry fields and return exactly one outbound SMS."""
    state_manager = context['state_manager']
    phone_number = context['phone_number']
    if not state_manager.update_fields(phone_number, updates):
        logger.error(
            "dinner enquiry: update_fields failed for %s — check migrations / allowed fields. keys=%s",
            phone_number, list(updates.keys()),
        )
        fallback_keys = {
            "first_contact_sent", "booking_type", "experience_type",
            "duration", "incall_outcall", "client_name", "date", "time",
        }
        fallback_updates = {k: v for k, v in updates.items() if k in fallback_keys}
        if fallback_updates and fallback_updates != updates:
            if state_manager.update_fields(phone_number, fallback_updates):
                logger.warning(
                    "dinner enquiry: applied fallback state updates for %s. keys=%s",
                    phone_number, list(fallback_updates.keys()),
                )
            else:
                logger.error(
                    "dinner enquiry: fallback state update also failed for %s. keys=%s",
                    phone_number, list(fallback_updates.keys()),
                )
    return {"messages": [message], "new_state": "COLLECTING", "actions": []}
