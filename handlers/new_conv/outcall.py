# ruff: noqa: F401,F403,F405
from handlers.new_conv._shared import *  # noqa: F401,F403
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Stage helpers for _handle_request_outcall_impl
# ---------------------------------------------------------------------------

import logging

from utils.log_sanitize import LOG_SUPPRESSED_FMT
logger = logging.getLogger("adella_chatbot.outcall")


def _build_outcall_policy_cta_message(
    *,
    client_name: str | None,
    city: str,
    surcharge: int,
    deposit_outcall: int,
    webform_url: str,
) -> str:
    name_str = f" {client_name}" if (client_name and isinstance(client_name, str)) else ""
    policy_line = build_outcall_policy_line(
        surcharge=surcharge,
        deposit_outcall=deposit_outcall,
        city=city,
    )
    return (
        f"Hi{name_str} {policy_line}\n\n"
        f"Send me a text if you want to make a booking or alternatively use my booking webform {webform_url}"
    )

def _stage_outcall_no_intent(
    context: dict,
    state: dict,
    state_manager,
    phone_number: str,
    message: str,
) -> dict | None:
    """Return incall 3-slot response when no outcall intent is found.
    Returns None to continue with the outcall flow."""
    client_message = message.lower()
    outcall_keywords = [
        'outcall', 'out call', 'out-call', 'outcall to',
        'my place', 'my hotel', 'my room', 'my address', 'my location',
        'my apartment', 'my apt',
        'come to me', 'come to my', 'come over', 'come here',
        'come see me', 'come and see me', 'see me',
        'visit me', 'you visit', 'you come', 'you travel',
        'can you come', 'can you travel', 'do you travel', 'do you outcall',
        'are you mobile', 'mobile service',
        'travel to me', 'to me', 'to my', 'at my',
        'where i am', 'home visit', 'hotel visit',
        'do outcalls',
    ]
    location_hint_patterns = [
        r"\bi'?m\s+at\s+",
        r"\blocated\s+at\s+",
        r"\bstaying\s+at\s+",
    ]
    has_location_hint = any(re.search(p, client_message, re.IGNORECASE) for p in location_hint_patterns)
    has_outcall_intent = any(kw in client_message for kw in outcall_keywords) or has_location_hint
    if has_outcall_intent:
        return None

    from config import get_available_hours, get_current_incall_location, get_profile_url
    from core.webform_security import get_webform_url
    from utils.availability_slots import get_next_available_time_slots
    from utils.timezone import get_current_datetime

    location = get_current_incall_location()
    client_name = greetings.extract_client_name(message)
    webform_url = get_webform_url(phone_number)
    now = get_current_datetime()
    _grace_start = now + timedelta(minutes=30)
    _grace_start = _grace_start.replace(second=0, microsecond=0)
    _gr = _grace_start.minute % 15
    if _gr != 0:
        _grace_start = _grace_start + timedelta(minutes=15 - _gr)
    from utils.time_parser import get_tonight_slot_window, is_tonight_request
    if is_tonight_request(message):
        _tonight_start, _tonight_end = get_tonight_slot_window(now)
    else:
        _tonight_start, _tonight_end = _grace_start, None
    time_slots = get_next_available_time_slots(
        now,
        num_slots=3,
        check_calendar=True,
        start_from=_tonight_start,
        end_by=_tonight_end,
        persist_slots_for_phone=phone_number,
        persist_slots_state_manager=state_manager,
        **slot_kwargs_from_booking_state(state),
    )
    fully_booked_tonight = False
    if is_tonight_request(message) and not time_slots:
        fully_booked_tonight = True
        time_slots = get_next_available_time_slots(
            now,
            num_slots=3,
            check_calendar=True,
            persist_slots_for_phone=phone_number,
            persist_slots_state_manager=state_manager,
            **slot_kwargs_from_booking_state(state),
        )
    availability_msg = greetings.get_available_now_message(
        city=location.get('city', ''),
        hotel_name=location.get('hotel_name', ''),
        available_hours=get_available_hours(),
        client_name=client_name,
        is_outcall=False,
        address=location.get('address', ''),
        has_duration=False,
        webform_url=webform_url,
        profile_url=get_profile_url(),
        time_slots=time_slots,
        fully_booked_tonight=fully_booked_tonight,
    )
    updates = {
        'first_contact_sent': True,
        'incall_outcall': 'incall',
        'offered_slot_hours': [dt.hour for dt, _ in time_slots] if time_slots else [],
        'offered_slot_minutes': [dt.minute for dt, _ in time_slots] if time_slots else [],
        'offered_slot_dates': [dt.strftime('%Y-%m-%d') for dt, _ in time_slots] if time_slots else [],
        'offered_slot_date': time_slots[0][0].strftime('%Y-%m-%d') if time_slots else now.strftime('%Y-%m-%d'),
    }
    if client_name:
        updates['client_name'] = client_name
    state_manager.update_fields(phone_number, updates)
    return {"messages": [availability_msg], "new_state": "COLLECTING", "actions": []}


def _stage_outcall_specific_time(
    context: dict,
    state: dict,
    state_manager,
    phone_number: str,
    message: str,
    location: dict,
    client_name: str | None,
    webform_url: str,
    specific_hour,
) -> dict | None:
    """Handle a specific-time outcall request.
    Returns response dict if handled; None to fall through to standard flow."""
    if specific_hour is None:
        return None
    logger.info("[OUTCALL] Specific time hour detected: %s", specific_hour)
    try:
        import pytz
        import re as _re

        from datetime import datetime as _dt

        from services.calendar_service import check_conflict
        from utils.availability_slots import (
            get_business_hours,
            get_next_available_time_slots,
            normalize_business_hours_pair,
        )
        from utils.time_parser import (
            get_requested_day_start,
            infer_requested_datetime_for_booking,
            infer_time_from_hour,
            is_tonight_request,
            message_has_explicit_clock,
        )
        from utils.timezone import get_current_datetime

        from config import get_escort_name, get_profile_url

        now = get_current_datetime()
        bh = normalize_business_hours_pair(get_business_hours()) or (11, 4)
        end_hour = int(bh[1]) if bh and len(bh) > 1 else None
        in_late_night = 0 <= now.hour < end_hour
        tomorrow_early_exception = bool(
            _re.search(r'\b(tomorrow|tmrw|tmr)\b', message, _re.IGNORECASE)
            and _re.search(r'\b(midnight|12\s*am|1\s*am|2\s*am|3\s*am)\b', message, _re.IGNORECASE)
        )

        _explicit_clock = message_has_explicit_clock(message)

        if is_tonight_request(message) and in_late_night and not _explicit_clock:
            # After midnight — show remaining slots until knock-off
            _grace_start = now + timedelta(minutes=30)
            _grace_start = _grace_start.replace(second=0, microsecond=0)
            _gr = _grace_start.minute % 15
            if _gr != 0:
                _grace_start = _grace_start + timedelta(minutes=15 - _gr)
            _end_by = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
            time_slots = get_next_available_time_slots(
                now, num_slots=3, check_calendar=True,
                start_from=_grace_start, end_by=_end_by,
                persist_slots_for_phone=phone_number,
                persist_slots_state_manager=state_manager,
                **slot_kwargs_from_booking_state(state),
            )
            tonight_notice = ""
            if not time_slots:
                from templates.booking_collection_messages import FULLY_BOOKED_TONIGHT_NOTICE

                tonight_notice = FULLY_BOOKED_TONIGHT_NOTICE
                time_slots = get_next_available_time_slots(
                    now, num_slots=3, check_calendar=True,
                    persist_slots_for_phone=phone_number,
                    persist_slots_state_manager=state_manager,
                    **slot_kwargs_from_booking_state(state),
                )
            try:
                from core.rates_from_config import get_deposit_outcall, get_surcharge
                surcharge, deposit_outcall = get_surcharge(), get_deposit_outcall()
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                surcharge, deposit_outcall = _get_outcall_pricing_defaults()
            name_str = f" {client_name}" if (client_name and isinstance(client_name, str)) else ""
            _policy_line = build_outcall_policy_line(
                surcharge=surcharge, deposit_outcall=deposit_outcall,
                location_name=location.get('hotel_name', 'my location'),
            )
            outcall_slots_msg = build_outcall_slots_message(
                heading=f"Here are the times I have available {get_availability_window_label(time_slots, now=now)}",
                name_str=name_str,
                time_slots_formatted=format_slot_list_for_sms(time_slots),
                profile_url=get_profile_url(), policy_line=_policy_line, webform_url=webform_url,
                tonight_unavailable_notice=tonight_notice,
            )
            updates = {'first_contact_sent': True, 'incall_outcall': 'outcall'}
            if client_name:
                updates['client_name'] = client_name
            state_manager.update_fields(phone_number, updates)
            return {"messages": [outcall_slots_msg], "new_state": "COLLECTING", "actions": []}

        extracted_time = infer_requested_datetime_for_booking(message, now=now)
        if extracted_time is None:
            inferred_date, inferred_hour = infer_time_from_hour(specific_hour, now)
            _req_day_start, _, _ = get_requested_day_start(now, message)
            if _req_day_start is not None:
                inferred_date = _req_day_start.date()
            try:
                from utils.timezone import get_local_timezone

                _escort_tz = get_local_timezone()
                extracted_time = _escort_tz.localize(
                    _dt.combine(inferred_date, _dt.min.time().replace(hour=inferred_hour, minute=0))
                )
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                extracted_time = _dt.combine(
                    inferred_date, _dt.min.time().replace(hour=inferred_hour, minute=0)
                )

        try:
            now_tz = getattr(now, "tzinfo", None)
            ext_tz = getattr(extracted_time, "tzinfo", None)
            if now_tz and ext_tz is None:
                try:
                    extracted_time = now_tz.localize(extracted_time)
                except Exception as e:
                    logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                    extracted_time = extracted_time.replace(tzinfo=now_tz)
            elif ext_tz and not now_tz:
                extracted_time = extracted_time.replace(tzinfo=None)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        logger.info("[OUTCALL] Inferred specific time: %s", extracted_time)

        if tomorrow_early_exception:
            _bh = normalize_business_hours_pair(get_business_hours()) or (11, 4)
            _service_start = now.replace(hour=int(_bh[0]), minute=0, second=0, microsecond=0)
            _grace_start = now + timedelta(minutes=30)
            _grace_start = _grace_start.replace(second=0, microsecond=0)
            _gr = _grace_start.minute % 15
            if _gr != 0:
                _grace_start = _grace_start + timedelta(minutes=15 - _gr)
            _start_from = _service_start if _service_start > _grace_start else _grace_start
            _end_by = now.replace(hour=23, minute=59, second=0, microsecond=0)
            if _start_from > _end_by:
                _start_from = _grace_start
                _end_by = None
            time_slots = get_next_available_time_slots(
                now, num_slots=3, check_calendar=True,
                start_from=_start_from, end_by=_end_by,
                persist_slots_for_phone=phone_number,
                persist_slots_state_manager=state_manager,
                **slot_kwargs_from_booking_state(state),
            )
            try:
                from core.rates_from_config import get_deposit_outcall, get_surcharge
                surcharge, deposit_outcall = get_surcharge(), get_deposit_outcall()
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                surcharge, deposit_outcall = _get_outcall_pricing_defaults()
            name_str = f" {client_name}" if (client_name and isinstance(client_name, str)) else ""
            _policy_line = build_outcall_policy_line(
                surcharge=surcharge, deposit_outcall=deposit_outcall,
                location_name=location.get('hotel_name', 'my location'),
            )
            _slots_heading = f"Here are the times I have available {get_availability_window_label(time_slots, now=now)}"
            outcall_slots_msg = build_outcall_slots_message(
                heading=_slots_heading,
                name_str=name_str,
                time_slots_formatted=format_slot_list_for_sms(time_slots),
                profile_url=get_profile_url(), policy_line=_policy_line, webform_url=webform_url,
            )
            updates = {'first_contact_sent': True, 'incall_outcall': 'outcall'}
            if client_name:
                updates['client_name'] = client_name
            state_manager.update_fields(phone_number, updates)
            return {"messages": [outcall_slots_msg], "new_state": "COLLECTING", "actions": []}

        _oc_dur = DINNER_DURATION_MINUTES if is_dinner_date_booking(state) else 60
        booking_details = {
            'date': extracted_time.strftime('%Y-%m-%d'),
            'time': (extracted_time.hour, extracted_time.minute),
            'duration': _oc_dur,
            'incall_outcall': 'outcall',
        }
        conflict_type, _ = check_conflict(booking_details)
        is_available = conflict_type == "none"

        if is_available:
            logger.info("[OUTCALL] Time %s is AVAILABLE", extracted_time)
            msg = greetings.get_time_requested_available_message(
                requested_datetime=extracted_time,
                city=location.get('city', ''),
                hotel_name=location.get('hotel_name', ''),
                client_name=client_name,
                is_outcall=True,
                address=location.get('address', ''),
                escort_name=get_escort_name(),
                webform_url=webform_url,
                profile_url=get_profile_url(),
            )
            updates = {
                'first_contact_sent': True, 'incall_outcall': 'outcall',
                'date': extracted_time.strftime('%Y-%m-%d'),
                'time': (extracted_time.hour, extracted_time.minute),
            }
            if client_name:
                updates['client_name'] = client_name
            logger.info("[OUTCALL_TIME_SPECIFIC] Persisting fields and transitioning to COLLECTING for %s", phone_number)
            state_manager.update_fields(phone_number, updates)
            return {"messages": [msg], "new_state": "COLLECTING", "actions": []}
        else:
            logger.info("[OUTCALL] Time %s NOT available — showing ±2h alternatives", extracted_time)
            from services.calendar_service import find_alternative_slots as _fa_oc2
            from utils.availability_slots import _format_slot_display as _fsd_oc2
            alt_dts = _fa_oc2(booking_details, max_results=3, same_day_only=False, max_hours_from_requested=2.0)
            time_slots = [(dt, _fsd_oc2(dt)) for dt in alt_dts]
            try:
                from core.rates_from_config import get_deposit_outcall, get_surcharge
                surcharge, deposit_outcall = get_surcharge(), get_deposit_outcall()
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                surcharge, deposit_outcall = _get_outcall_pricing_defaults()
            name_str = f" {client_name}" if (client_name and isinstance(client_name, str)) else ""
            _policy_line = build_outcall_policy_line(
                surcharge=surcharge, deposit_outcall=deposit_outcall,
                location_name=location.get('hotel_name', 'my location'),
            )
            outcall_slots_msg = build_outcall_slots_message(
                heading="Unfortunately that time isn't available. Here are times I have",
                name_str=name_str,
                time_slots_formatted=format_slot_list_for_sms(time_slots),
                profile_url=get_profile_url(), policy_line=_policy_line, webform_url=webform_url,
            )
            updates = {'first_contact_sent': True, 'incall_outcall': 'outcall'}
            if client_name:
                updates['client_name'] = client_name
            state_manager.update_fields(phone_number, updates)
            return {"messages": [outcall_slots_msg], "new_state": "COLLECTING", "actions": []}
    except Exception as e:
        logger.warning("[OUTCALL] Specific time handling failed: %s", e, exc_info=True)
        return None  # Fall through to standard handling


def _stage_outcall_first_contact(
    state: dict,
    state_manager,
    phone_number: str,
    message: str,
    location: dict,
    client_name: str | None,
    webform_url: str,
    current_fields: dict,
    extracted: dict | None,
    messages: list,
) -> None:
    """Build outcall first-contact message. Appends to messages in-place."""
    from config import get_available_hours, get_profile_url

    has_date = current_fields.get('date') or (extracted or {}).get('date')
    has_time = current_fields.get('time') or (extracted or {}).get('time')

    if not has_date and not has_time:
        try:
            try:
                from core.rates_from_config import get_deposit_outcall, get_surcharge
                surcharge, deposit_outcall = get_surcharge(), get_deposit_outcall()
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                surcharge, deposit_outcall = _get_outcall_pricing_defaults()
            outcall_slots_msg = _build_outcall_policy_cta_message(
                client_name=client_name,
                city=location.get('city') or 'the city',
                surcharge=surcharge,
                deposit_outcall=deposit_outcall,
                webform_url=webform_url,
            )
            messages.append(outcall_slots_msg)
        except Exception as e:
            logger.warning("Could not generate outcall slots: %s, falling back", e)
            first_contact = greetings.get_first_contact_message(
                city=location.get('city', ''),
                hotel_name=location.get('hotel_name', ''),
                location_description=location.get('display_name', location.get('hotel_name', '')),
                available_hours=get_available_hours(),
                profile_url=get_profile_url(),
                booking_type="outcall",
                webform_url=webform_url,
                client_name=client_name,
                address=location.get('address', ''),
                persist_slots_for_phone=phone_number,
                persist_slots_state_manager=state_manager,
            )
            messages.append(first_contact)
    else:
        has_address = current_fields.get('outcall_address') or (extracted or {}).get('outcall_address')
        has_duration = current_fields.get('duration') or (extracted or {}).get('duration')
        _exp_set = bool(current_fields.get('experience_type') or (extracted or {}).get('experience_type'))
        if has_time and has_address and not has_duration:
            messages.append(field_prompts.get_duration_only_prompt(experience_already_set=_exp_set, is_outcall=True))
        elif has_time and not has_duration:
            messages.append(field_prompts.get_duration_only_prompt(experience_already_set=_exp_set, is_outcall=True))
        else:
            messages.append(field_prompts.get_ask_date_time_duration_prompt(is_outcall=True))

    updates = {'first_contact_sent': True, 'incall_outcall': 'outcall'}
    if client_name:
        updates['client_name'] = client_name
    state_manager.update_fields(phone_number, updates)


def _stage_outcall_already_sent(
    *,
    phone_number: str,
    client_name: str | None,
    location: dict,
    messages: list,
) -> None:
    """Append outcall info when first contact was already sent. Modifies messages in-place."""
    try:
        from core.rates_from_config import get_deposit_outcall, get_surcharge
        outcall_surcharge, outcall_deposit = get_surcharge(), get_deposit_outcall()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        outcall_surcharge, outcall_deposit = _get_outcall_pricing_defaults()
    try:
        from core.webform_security import get_webform_url

        webform_url = get_webform_url(phone_number)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        webform_url = ""
    outcall_message = _build_outcall_policy_cta_message(
        client_name=client_name,
        city=location.get('city') or 'the city',
        surcharge=outcall_surcharge,
        deposit_outcall=outcall_deposit,
        webform_url=webform_url,
    )
    messages.append(outcall_message)


# ---------------------------------------------------------------------------
# Public wrapper
# ---------------------------------------------------------------------------

def handle_request_outcall(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle outcall request.

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    try:
        return _handle_request_outcall_impl(context)
    except Exception as e:
        logger.exception("handle_request_outcall failed: %s", e)
        return {
            "messages": [_outcall_fallback_msg()],
            "new_state": "COLLECTING",
            "actions": []
        }


def _handle_request_outcall_impl(context: dict[str, Any]) -> dict[str, Any]:
    """Implementation of outcall handler (called from handle_request_outcall)."""
    state = context['state']
    state_manager = context['state_manager']
    phone_number = context['phone_number']
    message = (context.get('message') or '').strip()
    state = state_manager.get_state(phone_number) or state

    messages: list = []

    # Stage 1: Redirect to incall flow if no outcall intent detected
    result = _stage_outcall_no_intent(context, state, state_manager, phone_number, message)
    if result is not None:
        return result

    # Outcall confirmed — set up common variables
    from config import get_current_incall_location, get_escort_name, get_profile_url  # noqa: F401
    from core.webform_security import get_webform_url
    from utils.time_parser import parse_time_from_message

    location = get_current_incall_location()
    client_name = greetings.extract_client_name(message)
    webform_url = get_webform_url(phone_number)

    # Stage 2: Handle specific-time request
    specific_hour = parse_time_from_message(message)
    logger.info("[OUTCALL DEBUG] parse_time_from_message returned: %s for message: %s", specific_hour, message)
    result = _stage_outcall_specific_time(
        context, state, state_manager, phone_number, message,
        location, client_name, webform_url, specific_hour,
    )
    if result is not None:
        return result

    # Stage 3: Standard field extraction
    import config as cfg
    from booking.field_collector import FieldCollector
    ai_service = context.get('ai_service')
    field_collector = FieldCollector(cfg, ai_service=ai_service)
    current_fields = state_manager.get_booking_fields(phone_number) or {}
    extracted = field_collector.extract_fields(message, current_fields)
    if extracted:
        if not state_manager.update_fields(phone_number, extracted):
            logger.warning("outcall: update_fields failed for %s", phone_number)
        current_fields = state_manager.get_booking_fields(phone_number) or {}
    state = state_manager.get_state(phone_number) or state

    # Stage 4: First contact or already-sent follow-up
    if not state.get('first_contact_sent'):
        _stage_outcall_first_contact(
            state, state_manager, phone_number, message,
            location, client_name, webform_url, current_fields, extracted, messages,
        )
    else:
        _stage_outcall_already_sent(
            phone_number=phone_number,
            client_name=client_name,
            location=location,
            messages=messages,
        )

    return {"messages": messages, "new_state": "COLLECTING", "actions": []}
