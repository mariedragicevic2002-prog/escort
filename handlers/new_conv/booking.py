# ruff: noqa: F401,F403,F405
from handlers.new_conv._shared import *  # noqa: F401,F403
from typing import Any, Optional

import logging

from utils.log_sanitize import LOG_SUPPRESSED_FMT
logger = logging.getLogger("adella_chatbot.booking")

def handle_book_appointment(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle book_appointment intent in NEW state.
    Extracts date/time/duration from the client's message so we don't ask again
    if they already said e.g. "Friday at 9pm for 1 hour".
    """
    try:
        return _handle_book_appointment_impl(context)
    except Exception as e:
        logger.exception("handle_book_appointment failed: %s", e)
        if _has_outcall_intent(context.get('message', '')):
            return {"messages": [_outcall_fallback_msg()], "new_state": "COLLECTING", "actions": []}
        incall_msg = _get_incall_first_contact_for_fallback(context)
        if incall_msg:
            try:
                _sm = context.get('state_manager')
                if _sm is not None:
                    _sm.update_fields(context['phone_number'], {'first_contact_sent': True})
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            return {"messages": [incall_msg], "new_state": "COLLECTING", "actions": []}
        fallback = field_prompts.get_ask_date_time_duration_prompt(
            is_outcall=_has_outcall_intent(context.get('message', ''))
        )
        return {"messages": [fallback], "new_state": "COLLECTING", "actions": []}


def _handle_book_appointment_impl(context: dict[str, Any]) -> dict[str, Any]:
    """Implementation of book_appointment handler."""
    state = context['state']
    state_manager = context['state_manager']
    phone_number = context['phone_number']
    message = context.get('message', '')

    import config as cfg
    from booking.field_collector import FieldCollector
    ai_service = context.get('ai_service')
    field_collector = FieldCollector(cfg, ai_service=ai_service)
    current_fields = state_manager.get_booking_fields(phone_number)
    extracted = field_collector.extract_fields(message, current_fields)
    if extracted:
        state_manager.update_fields(phone_number, extracted)
        current_fields = state_manager.get_booking_fields(phone_number)
    missing_fields = field_collector.get_missing_fields(current_fields)
    state = state_manager.get_state(phone_number) or state

    # Detect booking type using shared helper so detection stays in sync with _shared.py
    booking_type = "outcall" if _has_outcall_intent(message) or extracted.get('outcall_address') else "incall"

    if extracted and not missing_fields:
        from config import get_current_incall_location, get_profile_url
        from core.webform_security import get_webform_url
        from handlers.booking_collection import (
            _format_perfect_timing_line,
            check_and_format_outside_hours,
        )

        location = get_current_incall_location()
        client_name = greetings.extract_client_name(message)
        webform_url = get_webform_url(phone_number)
        _venue_h = (location.get("hotel_name") or location.get("display_name") or "").strip()
        _street_h = (location.get("address") or "").strip()
        is_within_hours, outside_hours_msg, _, _ = check_and_format_outside_hours(
            current_fields,
            webform_url=webform_url,
            profile_url=get_profile_url() or '',
            city=location.get('city', ''),
            address=_street_h,
            venue_name=_venue_h,
            phone_number=phone_number,
            state_manager=state_manager,
        )

        # Same golden-rule first contact as greeting / missing-fields book_appointment path.
        if (
            not state.get('first_contact_sent')
            and booking_type != "outcall"
        ):
            return _new_booking_first_contact(context)

        if not is_within_hours:
            updates = {'first_contact_sent': True, 'date': None, 'time': None}
            if client_name:
                updates['client_name'] = client_name
            # Persist a default incall_outcall so downstream COLLECTING messages have a booking-type hint
            # even when name extraction/outcall intent parsing didn't set it.
            if not current_fields.get('incall_outcall') and not state.get('incall_outcall'):
                updates['incall_outcall'] = 'incall'
            state_manager.update_fields(phone_number, updates)
            try:
                from utils.availability_slots import get_next_available_time_slots
                from utils.timezone import get_current_datetime
                _now_oh = get_current_datetime()
                _slots_oh = get_next_available_time_slots(
                    _now_oh,
                    num_slots=3,
                    check_calendar=True,
                    persist_slots_for_phone=phone_number,
                    persist_slots_state_manager=state_manager,
                    **slot_kwargs_from_booking_state(state),
                )
                if _slots_oh:
                    _slot_lines = "\n".join(f"\u2022 {s[1]}" for s in _slots_oh)
                    outside_hours_msg = f"{outside_hours_msg}\n\nMy next available times:\n{_slot_lines}"
            except Exception as _slot_err:
                logger.warning(LOG_SUPPRESSED_FMT, _slot_err, exc_info=False)
            return {"messages": [outside_hours_msg], "new_state": "COLLECTING", "actions": []}

        # All 3 mandatory present and within hours — proceed to calendar check.
        # Experience type is collected in the booking summary confirmation (never as a standalone question).
        perfect_timing = _format_perfect_timing_line(
            current_fields,
            client_name=client_name or "",
            phone_number=phone_number,
            webform_url=webform_url,
        )
        updates: dict[str, Any] = {'first_contact_sent': True}
        if client_name:
            updates['client_name'] = client_name
        if not current_fields.get('incall_outcall') and not state.get('incall_outcall'):
            updates['incall_outcall'] = 'incall'
        state_manager.update_fields(phone_number, updates)
        return {"messages": [perfect_timing], "new_state": "CHECKING_AVAILABILITY", "actions": ["check_calendar"]}

    # Missing some fields (0, 1 or 2 mandatory) - send first contact with webform if not already sent
    messages = []
    if not state.get('first_contact_sent'):
        from config import get_available_hours, get_current_incall_location, get_escort_name, get_profile_url
        from core.webform_security import get_webform_url

        location = get_current_incall_location()
        available_hours = get_available_hours()
        client_name = greetings.extract_client_name(message)

        webform_url = get_webform_url(phone_number)

        # If a specific time was mentioned, check working hours AND calendar availability
        try:
            from datetime import datetime as _dt2

            from config import get_escort_name
            from handlers.booking_collection import check_and_format_outside_hours
            from services.calendar_service import check_conflict
            from utils.availability_slots import (
                get_business_hours,
                get_next_available_time_slots,
                normalize_business_hours_pair,
            )
            from utils.time_parser import (
                has_invalid_numeric_date_literal,
                infer_requested_datetime_for_booking,
                is_tonight_request,
                message_has_explicit_clock,
                parse_time_from_message,
            )
            from utils.timezone import get_current_datetime

            _now_bk = get_current_datetime()
            if has_invalid_numeric_date_literal(message, now=_now_bk):
                from templates.errors import get_error_message

                updates = {'first_contact_sent': True}
                if client_name:
                    updates['client_name'] = client_name
                state_manager.update_fields(phone_number, updates)
                return {
                    "messages": [f"❌ {get_error_message('invalid_date')}"],
                    "new_state": "COLLECTING",
                    "actions": [],
                }
            _specific_dt = infer_requested_datetime_for_booking(message, now=_now_bk)
            _bh = normalize_business_hours_pair(get_business_hours()) or (11, 4)
            _end_hour = int(_bh[1]) if _bh and len(_bh) > 1 else None
            _in_late_night = 0 <= _now_bk.hour < _end_hour

            if _specific_dt is not None:
                _inf_date = _specific_dt.date()
                _inf_hour = _specific_dt.hour
                _inf_min = _specific_dt.minute
                _bk_venue = (location.get("hotel_name") or location.get("display_name") or "").strip()
                _bk_street = (location.get("address") or "").strip()
                _bk_fields = {
                    'client_name': client_name or '',
                    'incall_outcall': booking_type,
                    'date': _inf_date,
                    'time': (_inf_hour, _inf_min),
                }
                _bk_within, outside_msg, _, _ = check_and_format_outside_hours(
                    _bk_fields,
                    webform_url=webform_url,
                    profile_url=get_profile_url() or '',
                    city=location.get('city', ''),
                    address=_bk_street,
                    venue_name=_bk_venue,
                    phone_number=phone_number,
                    hours_setting_default='',
                    days_setting_default='7 days a week',
                )
                if not _bk_within:
                    updates = {'first_contact_sent': True}
                    if client_name:
                        updates['client_name'] = client_name
                    state_manager.update_fields(phone_number, updates)
                    return {"messages": [outside_msg], "new_state": "COLLECTING", "actions": []}

                _bk_dur = DINNER_DURATION_MINUTES if is_dinner_date_booking(state) else 60
                _bk_io = "outcall" if is_dinner_date_booking(state) else booking_type
                _bk_details = {
                    'date': _inf_date.strftime('%Y-%m-%d'),
                    'time': (_inf_hour, _inf_min),
                    'duration': _bk_dur,
                    'incall_outcall': _bk_io,
                }
                _bk_conflict, _ = check_conflict(_bk_details)
                if _bk_conflict == "none":
                    from utils.timezone import get_local_timezone

                    _escort_tz = get_local_timezone()
                    _extracted_dt = _escort_tz.localize(
                        _dt2.combine(_inf_date, _dt2.min.time().replace(hour=_inf_hour, minute=_inf_min))
                    )
                    _is_outcall_bk = booking_type == 'outcall'
                    msg = greetings.get_time_requested_available_message(
                        requested_datetime=_extracted_dt,
                        city=location.get('city', ''),
                        hotel_name=location.get('hotel_name', ''),
                        client_name=client_name,
                        is_outcall=_is_outcall_bk,
                        address=location.get('address', ''),
                        escort_name=get_escort_name(),
                        webform_url=webform_url,
                        profile_url=get_profile_url(),
                    )
                    updates = {
                        'first_contact_sent': True,
                        'incall_outcall': booking_type,
                        'date': _inf_date.strftime('%Y-%m-%d'),
                        'time': (_inf_hour, _inf_min),
                    }
                    if client_name:
                        updates['client_name'] = client_name
                    state_manager.update_fields(phone_number, updates)
                    return {"messages": [msg], "new_state": "COLLECTING", "actions": []}
                else:
                    _rh12_bk = _inf_hour % 12 or 12
                    _rampm_bk = "am" if _inf_hour < 12 else "pm"
                    _req_ts_bk = (
                        f"{_rh12_bk}:{_inf_min:02d}{_rampm_bk}"
                        if _inf_min
                        else f"{_rh12_bk}{_rampm_bk}"
                    )
                    msg, _ = greetings.build_booking_time_unavailable_message(
                        _bk_details,
                        _req_ts_bk,
                        city=location.get('city', ''),
                        hotel_name=location.get('hotel_name', ''),
                        address=location.get('address', ''),
                        client_name=client_name,
                        is_outcall=(booking_type == 'outcall'),
                        escort_name=get_escort_name(),
                        webform_url=webform_url,
                        profile_url=get_profile_url(),
                        find_alternative_slots_kwargs={
                            "same_day_only": False,
                            "max_hours_from_requested": 2.0,
                        },
                    )
                    updates = {'first_contact_sent': True, 'incall_outcall': booking_type}
                    if client_name:
                        updates['client_name'] = client_name
                    state_manager.update_fields(phone_number, updates)
                    return {"messages": [msg], "new_state": "COLLECTING", "actions": []}
            elif (
                parse_time_from_message(message) is not None
                and is_tonight_request(message)
                and _in_late_night
                and not message_has_explicit_clock(message)
            ):
                _grace_start = _now_bk + timedelta(minutes=30)
                _grace_start = _grace_start.replace(second=0, microsecond=0)
                _gr = _grace_start.minute % 15
                if _gr != 0:
                    _grace_start = _grace_start + timedelta(minutes=15 - _gr)
                _end_by = _now_bk.replace(hour=_end_hour, minute=0, second=0, microsecond=0)
                _slots = get_next_available_time_slots(
                    _now_bk,
                    num_slots=3,
                    check_calendar=True,
                    start_from=_grace_start,
                    end_by=_end_by,
                    persist_slots_for_phone=phone_number,
                    persist_slots_state_manager=state_manager,
                    **slot_kwargs_from_booking_state(state),
                )
                _fully_booked_tonight = False
                if not _slots:
                    _fully_booked_tonight = True
                    _slots = get_next_available_time_slots(
                        _now_bk,
                        num_slots=3,
                        check_calendar=True,
                        persist_slots_for_phone=phone_number,
                        persist_slots_state_manager=state_manager,
                        **slot_kwargs_from_booking_state(state),
                    )
                _is_outcall_bk = booking_type == 'outcall'
                _msg = greetings.get_available_now_message(
                    city=location.get('city', ''),
                    hotel_name=location.get('hotel_name', ''),
                    available_hours=available_hours,
                    client_name=client_name,
                    is_outcall=_is_outcall_bk,
                    address=location.get('address', ''),
                    has_duration=False,
                    webform_url=webform_url,
                    profile_url=get_profile_url(),
                    time_slots=_slots,
                    fully_booked_tonight=_fully_booked_tonight,
                )
                updates = {'first_contact_sent': True, 'incall_outcall': booking_type}
                if client_name:
                    updates['client_name'] = client_name
                state_manager.update_fields(phone_number, updates)
                return {"messages": [_msg], "new_state": "COLLECTING", "actions": []}
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            return _new_booking_first_contact(context)

        # Same golden-rule first contact as handle_greeting (slots + webform + STRONGLY recommend).
        return _new_booking_first_contact(context)
    else:
        _prompt_outcall = str((current_fields.get('incall_outcall') or booking_type or '')).lower() == 'outcall'
        messages.append(field_prompts.get_ask_date_time_duration_prompt(is_outcall=_prompt_outcall))

    return {"messages": messages, "new_state": "COLLECTING", "actions": []}


def handle_modify_booking_new(context: dict[str, Any]) -> dict[str, Any]:
    """
    NEW state: reschedule/modify intent when there is no persisted booking yet.

    Avoids treating ghost reschedule messages like an active booking change.
    """
    phone_number = context["phone_number"]
    state_manager = context["state_manager"]
    bf = state_manager.get_booking_fields(phone_number) or {}
    has_core = bool(bf.get("date") and bf.get("time"))
    if has_core:
        return handle_book_appointment(context)
    return {
        "messages": [
            "I don't see an existing booking for this number yet — happy to set one up. "
            "Send the day, time, how long you'd like, and GFE/PSE if you know what you're after."
        ],
        "new_state": "COLLECTING",
        "actions": [],
    }


def handle_cancel_booking_new(context: dict[str, Any]) -> dict[str, Any]:
    """
    NEW state: acknowledge cancellations without dropping into booking prompts.
    """
    from templates.errors import get_error_message

    phone_number = context["phone_number"]
    state_manager = context["state_manager"]
    fields = state_manager.get_booking_fields(phone_number) or {}
    has_active_payload = bool(fields.get("date") or fields.get("time") or fields.get("duration"))
    if has_active_payload:
        state_manager.clear_booking(phone_number)
        return {
            "messages": ["No worries! Let me know if you'd like to book another time."],
            "new_state": "NEW",
            "actions": [],
        }
    return {
        "messages": [get_error_message("booking_not_found")],
        "new_state": None,
        "actions": [],
    }
