# ruff: noqa: F401,F403,F405
from handlers.new_conv._shared import *  # noqa: F401,F403
from handlers.new_conv.availability_stages import (
    DEFAULT_AVAILABLE_DAYS,
    _AVAILABLE_NOW_OUTSIDE_HOURS_KW,
    _EXPLICIT_OUTCALL_KEYWORDS,
    _booking_fields_for_outcall_deposit_copy,
)
from typing import Any

import logging

from utils.log_sanitize import LOG_SUPPRESSED_FMT

logger = logging.getLogger("adella_chatbot.availability")


def handle_available_now(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle immediate availability request (no deposit required).

    Flow:
    1. Check calendar for next 2 hours
    2. If available: Create peacock event, send location
    3. If not: Offer alternative times
    """
    from datetime import timedelta

    from config import get_current_incall_location
    from utils.timezone import get_current_datetime

    state = context['state']
    state_manager = context['state_manager']
    phone_number = context['phone_number']
    _persisted_state = state_manager.get_state(phone_number) or {}
    state = {**_persisted_state, **(state or {})}

    _cs_now = (state.get("current_state") or "").strip().upper()
    if _cs_now == "CONFIRMED":
        from handlers.confirmed_booking import handle_provide_field as _confirmed_pf

        _ctx = dict(context)
        _ctx["state"] = state
        return _confirmed_pf(_ctx)

    messages = []

    client_name = greetings.extract_client_name(context.get('message', ''))

    import config as cfg
    from booking.field_collector import FieldCollector

    ai_service = context.get('ai_service')
    field_collector = FieldCollector(cfg, ai_service=ai_service)
    try:
        extracted_fields_initial = field_collector.extract_fields(context.get('message', '') or '')
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        extracted_fields_initial = {}
    has_duration_in_first_message = bool((extracted_fields_initial or {}).get('duration'))

    location = get_current_incall_location()

    from datetime import timedelta as _td_han

    from config import get_profile_url
    from core.webform_security import get_webform_url
    from handlers.booking_collection import check_and_format_outside_hours
    from utils.timezone import get_current_datetime as _gcd_han

    _han_now = _gcd_han()
    _han_check_time = _han_now + _td_han(minutes=30)
    _han_is_outcall = any(
        kw in context.get("message", "").lower() for kw in _AVAILABLE_NOW_OUTSIDE_HOURS_KW
    )
    _han_wf = get_webform_url(phone_number)
    _han_within, _han_msg, _, _ = check_and_format_outside_hours(
        {
            'client_name': client_name or '',
            'incall_outcall': 'outcall' if _han_is_outcall else 'incall',
            'date': _han_check_time.date(),
            'time': (_han_check_time.hour, _han_check_time.minute),
        },
        webform_url=_han_wf,
        profile_url=get_profile_url() or '',
        city=location.get('city', ''),
        address=(location.get('address') or '').strip(),
        venue_name=(location.get('hotel_name') or location.get('display_name') or '').strip(),
        phone_number=phone_number,
        state_manager=state_manager,
        hours_setting_default='',
        suppress_time_specific_opener=True,
    )
    if not _han_within:
        return {"messages": [_han_msg], "new_state": "COLLECTING", "actions": []}

    client_message = context.get('message', '').lower()

    explicit_outcall_keywords = _EXPLICIT_OUTCALL_KEYWORDS

    location_hint_patterns = [
        r"\bi'?m\s+at\s+",
        r"\blocated\s+at\s+",
        r"\bstaying\s+at\s+",
    ]
    has_location_hint = any(re.search(pattern, client_message, re.IGNORECASE) for pattern in location_hint_patterns)
    extracted_outcall_address = (extracted_fields_initial or {}).get('outcall_address')
    extracted_location_hint = bool(extracted_outcall_address)

    is_outcall_request = (
        any(keyword in client_message for keyword in explicit_outcall_keywords)
        or extracted_location_hint
        or has_location_hint
    )

    if is_outcall_request:
        _addr_follow = (extracted_fields_initial or {}).get('outcall_address')
        if (
            state.get('available_now_requested')
            and state.get('incall_outcall') == 'outcall'
            and state.get('first_contact_sent')
            and _addr_follow
            and not state.get('earliest_slot_auto_selected')
        ):
            from utils.time_parser import parse_time_from_message

            _msg_in = (context.get('message') or '').strip()
            _has_explicit_clock = bool(
                re.search(r'\b\d{1,2}(?::\d{2})?\s*(am|pm)\b', _msg_in, re.IGNORECASE)
            )
            if parse_time_from_message(_msg_in) is None and not _has_explicit_clock:
                try:
                    from handlers.booking_collection import handle_provide_field
                    from utils.availability_slots import get_next_available_time_slots

                    _now_f = get_current_datetime()
                    _start_f = _now_f + timedelta(minutes=30)
                    _start_f = _start_f.replace(second=0, microsecond=0)
                    _gr = _start_f.minute % 15
                    if _gr != 0:
                        _start_f = _start_f + timedelta(minutes=15 - _gr)
                    _slots_f = get_next_available_time_slots(
                        _now_f,
                        num_slots=3,
                        check_calendar=True,
                        start_from=_start_f,
                        persist_slots_for_phone=phone_number,
                        persist_slots_state_manager=state_manager,
                        **slot_kwargs_from_booking_state(state),
                    )
                    if _slots_f:
                        _earliest = _slots_f[0][0]
                        _dur = state.get('duration') or 60
                        try:
                            _dur = int(_dur)
                        except (TypeError, ValueError):
                            _dur = 60
                        state_manager.update_fields(
                            phone_number,
                            {
                                'outcall_address': _addr_follow.strip(),
                                'date': _earliest.date(),
                                'time': (_earliest.hour, _earliest.minute),
                                'earliest_slot_auto_selected': True,
                                'duration': _dur,
                            },
                        )
                        context['state'] = state_manager.get_state(phone_number) or {}
                        logger.info(
                            "[available_now outcall] Address follow-up — auto-selecting earliest slot %s for %s",
                            _earliest.isoformat(),
                            phone_number,
                        )
                        return handle_provide_field(context)
                except Exception as _e_follow:
                    logger.warning("earliest-slot auto-select failed: %s", _e_follow, exc_info=True)

        try:
            from config import get_available_hours
            from core.webform_security import generate_secure_token, get_webform_url

            now = get_current_datetime()
            client_name = greetings.extract_client_name(context.get('message', ''))
            location = get_current_incall_location() or {}

            from handlers.booking_collection import check_within_available_hours_and_days, check_and_format_outside_hours

            _nc_check_time = now + timedelta(minutes=30)
            _nc_within, _nc_msg, _nc_avail_hours, _nc_avail_days = check_and_format_outside_hours(
                {
                    'client_name': client_name or '',
                    'incall_outcall': 'outcall',
                    'date': _nc_check_time.date(),
                    'time': (_nc_check_time.hour, _nc_check_time.minute),
                },
                phone_number=phone_number,
                state_manager=state_manager,
                hours_setting_default='',
                days_setting_default=DEFAULT_AVAILABLE_DAYS,
                suppress_time_specific_opener=True,
            )
            if not _nc_within:
                return {"messages": [_nc_msg], "new_state": "COLLECTING", "actions": []}

            webform_url = get_webform_url(phone_number)

            from utils.booking_window_interpreter import get_mandatory_booking_window, get_window_description
            _oc_window_start, _oc_window_end = get_mandatory_booking_window(now, context.get('message', ''))
            logger.info(f"MANDATORY BOOKING WINDOW APPLIED (outcall available-now): {get_window_description(now)}")

            import re as _re_av

            from utils.availability_slots import get_next_available_time_slots

            _arrival_mins_av = None
            _av_patterns = [
                (r'in\s+(?:about\s+|around\s+)?(\d+)\s*(?:mins?|minutes?)', 'min'),
                (r'(\d+)\s*(?:mins?|minutes?)\s*(?:from\s+now|away)', 'min'),
                (r'in\s+(?:about\s+|around\s+)?(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)', 'hour'),
                (r'in\s+an?\s+(?:hours?|hrs?)', 'hour_1'),
            ]
            for _av_pat, _av_unit in _av_patterns:
                _av_m = _re_av.search(_av_pat, client_message)
                if _av_m:
                    if _av_unit == 'hour_1':
                        _arrival_mins_av = 60
                    elif _av_unit == 'hour':
                        _arrival_mins_av = int(float(_av_m.group(1)) * 60)
                    else:
                        _arrival_mins_av = int(_av_m.group(1))
                    if not (5 <= _arrival_mins_av <= 180):
                        _arrival_mins_av = None
                    break

            if _arrival_mins_av is not None:
                requested_dt = now + timedelta(minutes=_arrival_mins_av)
                requested_dt = requested_dt.replace(second=0, microsecond=0)
                _rem = requested_dt.minute % 15
                if _rem != 0:
                    requested_dt = requested_dt + timedelta(minutes=15 - _rem)
                _rh, _rm2 = requested_dt.hour, requested_dt.minute
                _rh12 = _rh % 12 or 12
                _ampm = "am" if _rh < 12 else "pm"
                _req_time_str = f"{_rh12}:{_rm2:02d}{_ampm}" if _rm2 else f"{_rh12}{_ampm}"

                _req_within, _ = check_within_available_hours_and_days(
                    requested_dt.date(), (requested_dt.hour, requested_dt.minute),
                    _nc_avail_hours, _nc_avail_days
                )

                _an_dur = DINNER_DURATION_MINUTES if is_dinner_date_booking(state) else 60
                if requested_dt > _oc_window_end:
                    _req_within = False
                _slot_available = False
                if _req_within:
                    from services.calendar_service import check_conflict as _cc_av
                    _bd_req = {
                        'date': requested_dt.strftime('%Y-%m-%d'),
                        'time': (requested_dt.hour, requested_dt.minute),
                        'duration': _an_dur,
                        'incall_outcall': 'outcall',
                    }
                    _ct_req, _ = _cc_av(_bd_req)
                    _slot_available = _ct_req == 'none'

                if _slot_available:
                    from config import get_profile_url as _gpurl
                    _bf_dep = _booking_fields_for_outcall_deposit_copy(state, _an_dur)
                    available_now_msg = greetings.get_time_requested_available_message(
                        requested_datetime=requested_dt,
                        city=location.get('city', ''),
                        hotel_name=location.get('hotel_name', ''),
                        client_name=client_name or '',
                        is_outcall=True,
                        address=location.get('address', ''),
                        webform_url=webform_url,
                        profile_url=_gpurl() or '',
                        booking_fields=_bf_dep,
                        phone_number=phone_number,
                        state_manager=state_manager,
                    )
                else:
                    from services.calendar_service import find_alternative_slots as _fa_oc
                    from utils.availability_slots import _format_slot_display as _fsd_oc
                    _bd_oc = {
                        'date': requested_dt.strftime('%Y-%m-%d'),
                        'time': (requested_dt.hour, requested_dt.minute),
                        'duration': _an_dur,
                        'incall_outcall': 'outcall',
                    }
                    _alt_oc = _fa_oc(_bd_oc, max_results=3, same_day_only=False, max_hours_from_requested=2.0)
                    _outcall_time_slots = [(dt, _fsd_oc(dt)) for dt in _alt_oc]
                    from config import get_profile_url as _gpurl2
                    _bf_dep_na = _booking_fields_for_outcall_deposit_copy(state, _an_dur)
                    available_now_msg = greetings.get_requested_time_not_available_message(
                        requested_time_str=_req_time_str,
                        time_slots=_outcall_time_slots,
                        city=location.get('city', ''),
                        hotel_name=location.get('hotel_name', ''),
                        client_name=client_name or '',
                        is_outcall=True,
                        address=location.get('address', ''),
                        webform_url=webform_url,
                        profile_url=_gpurl2() or '',
                        booking_fields=_bf_dep_na,
                        phone_number=phone_number,
                        state_manager=state_manager,
                    )
            else:
                _outcall_start_from = now + timedelta(minutes=30)
                if _outcall_start_from > _oc_window_end:
                    _outcall_start_from = _oc_window_end
                _outcall_time_slots = get_next_available_time_slots(
                    now,
                    num_slots=3,
                    check_calendar=True,
                    start_from=_outcall_start_from,
                    end_by=_oc_window_end,
                    persist_slots_for_phone=phone_number,
                    persist_slots_state_manager=state_manager,
                    **slot_kwargs_from_booking_state(state),
                )
                _outcall_time_slots = [(dt, label) for dt, label in _outcall_time_slots if dt <= _oc_window_end]
                _an_dur_slots = DINNER_DURATION_MINUTES if is_dinner_date_booking(state) else 60
                _bf_dep_slots = _booking_fields_for_outcall_deposit_copy(state, _an_dur_slots)
                available_now_msg = greetings.get_available_now_message(
                    city=location.get('city', ''),
                    hotel_name=location.get('hotel_name', ''),
                    available_hours=get_available_hours(),
                    client_name=client_name or '',
                    is_outcall=True,
                    address=location.get('address', ''),
                    has_duration=has_duration_in_first_message,
                    webform_url=webform_url,
                    time_slots=_outcall_time_slots,
                    booking_fields=_bf_dep_slots,
                    phone_number=phone_number,
                    state_manager=state_manager,
                )
            messages.append(available_now_msg)

            if not state.get('first_contact_sent'):
                state_manager.update_fields(phone_number, {'first_contact_sent': True})

            updates = {
                'incall_outcall': 'outcall',
                'available_now_requested': True,
                'date': now.date(),
                'time': (now.hour, now.minute),
            }
            if client_name:
                updates['client_name'] = client_name
            if (extracted_fields_initial or {}).get('duration'):
                updates['duration'] = extracted_fields_initial['duration']
            if (extracted_fields_initial or {}).get('outcall_address'):
                updates['outcall_address'] = extracted_fields_initial['outcall_address']
            state_manager.update_fields(phone_number, updates)

            return {
                "messages": messages,
                "new_state": "COLLECTING",
                "actions": []
            }
        except Exception as e:
            logger.exception("Available-now outcall handler failed: %s", e)
            _name = greetings.extract_client_name(context.get('message', ''))
            try:
                from config import get_available_hours
                hours = get_available_hours()
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                hours = "5pm-6am, Thursday-Sunday"
            fallback = f"Hi{(' ' + _name) if _name else ''} Yes I am available now for outcalls! My hours are {hours}. Please reply with how long you'd like to book (min 1 hour for outcall) and your location, or use the webform: {get_base_url()}/booking"
            return {
                "messages": [fallback],
                "new_state": "COLLECTING",
                "actions": []
            }
    else:
        _specific_time = (extracted_fields_initial or {}).get('time')
        if _specific_time and not isinstance(_specific_time, bool):
            from datetime import time as _time_cls_sp

            from utils.timezone import get_current_datetime as _gcd_sp
            _sp_now = _gcd_sp()
            _sp_date = (extracted_fields_initial or {}).get('date') or _sp_now.date()
            _sp_duration = int((extracted_fields_initial or {}).get('duration') or 60)
            try:
                _sh, _sm = int(_specific_time[0]), int(_specific_time[1])
                _requested_dt = datetime.combine(_sp_date, _time_cls_sp(hour=_sh, minute=_sm))
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                _requested_dt = None

            if _requested_dt:
                from services.calendar_service import check_conflict as _cc_sp
                _sp_fields_check = {'date': _sp_date, 'time': (_sh, _sm), 'duration': _sp_duration, 'incall_outcall': 'incall'}
                try:
                    _sp_conflict, _ = _cc_sp(_sp_fields_check)
                except Exception as e:
                    logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                    _sp_conflict = 'unknown'

                _sp_save = {k: v for k, v in (extracted_fields_initial or {}).items() if v is not None}
                _sp_save['first_contact_sent'] = True
                _sp_save.setdefault('incall_outcall', 'incall')
                if client_name:
                    _sp_save['client_name'] = client_name
                state_manager.update_fields(phone_number, _sp_save)

                if _sp_conflict in ('none', 'graphite'):
                    from handlers.new_conv.availability_stages import _handle_ask_availability_impl
                    context['state'] = state_manager.get_state(phone_number) or {}
                    return _handle_ask_availability_impl(context)
                else:
                    from config import get_profile_url as _gpurl_sp
                    from core.webform_security import get_webform_url as _gwu_sp
                    from services.calendar_service import find_alternative_slots as _fa_sp
                    from utils.availability_slots import _format_slot_display as _fsd_sp
                    _sp_wf = _gwu_sp(phone_number)
                    _sp_alt_details = {
                        'date': _sp_date,
                        'time': (_sh, _sm),
                        'duration': _sp_duration,
                        'incall_outcall': 'incall',
                    }
                    _alt_sp = _fa_sp(
                        _sp_alt_details,
                        max_results=3,
                        same_day_only=False,
                        max_hours_from_requested=2.0,
                    )
                    _sp_alt_slots = [(dt, _fsd_sp(dt)) for dt in _alt_sp]
                    _sh12 = _sh % 12 or 12
                    _sp_ampm = "am" if _sh < 12 else "pm"
                    _sp_time_str = f"{_sh12}:{_sm:02d}{_sp_ampm}" if _sm else f"{_sh12}{_sp_ampm}"
                    _sp_not_avail_msg = greetings.get_requested_time_not_available_message(
                        requested_time_str=_sp_time_str,
                        time_slots=_sp_alt_slots,
                        city=location.get('city', ''),
                        hotel_name=location.get('hotel_name', ''),
                        client_name=client_name or '',
                        is_outcall=False,
                        address=location.get('address', ''),
                        webform_url=_sp_wf,
                        profile_url=_gpurl_sp() or '',
                    )
                    return {
                        "messages": [_sp_not_avail_msg],
                        "new_state": "COLLECTING",
                        "actions": [],
                    }

        from utils.booking_window_interpreter import get_mandatory_booking_window, get_window_description

        now = get_current_datetime()
        window_start, window_end = get_mandatory_booking_window(now, context.get('message', ''))

        logger.info(f"MANDATORY BOOKING WINDOW APPLIED: {get_window_description(now)}")
        logger.info(f"Window: {window_start} to {window_end}")

        start_time = now + timedelta(minutes=30)
        start_time = start_time.replace(second=0, microsecond=0)

        _rem = start_time.minute % 15
        if _rem != 0:
            start_time = start_time + timedelta(minutes=15 - _rem)

        if start_time > window_end:
            start_time = window_end

        from utils.availability_slots import get_next_available_time_slots
        from utils.time_parser import get_tonight_slot_window, is_tonight_request
        _msg = context.get('message', '')
        _tonight_query = is_tonight_request(_msg)
        if _tonight_query:
            _tonight_start, _tonight_end = get_tonight_slot_window(now)
            _slot_start_from = _tonight_start
            _slot_end_by = _tonight_end
        else:
            _slot_start_from = start_time
            _slot_end_by = None
        time_slots = get_next_available_time_slots(
            now,
            num_slots=3,
            check_calendar=True,
            start_from=_slot_start_from,
            end_by=_slot_end_by,
            persist_slots_for_phone=phone_number,
            persist_slots_state_manager=state_manager,
            **slot_kwargs_from_booking_state(state),
        )

        time_slots = [(dt, label) for dt, label in time_slots if dt <= window_end]

        explicit_incall_keywords = [
            'incall', 'in call', 'in-call', 'come to you', 'come to your',
            'your place', 'your location', 'visit you', 'your hotel', 'your room',
        ]
        is_explicit_incall = any(kw in client_message for kw in explicit_incall_keywords)
        is_explicit_outcall = _has_outcall_intent(client_message)

        if time_slots:
            try:
                from config import get_current_incall_location
                from core.webform_security import get_webform_url
                location = get_current_incall_location() or {}
                webform_url = get_webform_url(phone_number)
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                webform_url = f"{get_base_url()}/booking"
                location = {}

            if is_explicit_incall:
                message = greetings.get_available_now_3slot_message(
                    time_slots=time_slots,
                    city=location.get('city', ''),
                    hotel_name=location.get('hotel_name', ''),
                    client_name=client_name or '',
                    is_outcall=False,
                    address=location.get('address', ''),
                    webform_url=webform_url,
                )
                updates = {'incall_outcall': 'incall', 'available_now_requested': True, 'first_contact_sent': True}
            elif is_explicit_outcall:
                message = greetings.get_available_now_3slot_message(
                    time_slots=time_slots,
                    city=location.get('city', ''),
                    hotel_name=location.get('hotel_name', ''),
                    client_name=client_name or '',
                    is_outcall=True,
                    address=location.get('address', ''),
                    webform_url=webform_url,
                )
                updates = {'incall_outcall': 'outcall', 'available_now_requested': True, 'first_contact_sent': True}
            else:
                message = greetings.get_available_now_3slot_message(
                    time_slots=time_slots,
                    city=location.get('city', ''),
                    hotel_name=location.get('hotel_name', ''),
                    client_name=client_name or '',
                    is_outcall=False,
                    address=location.get('address', ''),
                    webform_url=webform_url,
                )
                updates = {'incall_outcall': 'incall', 'available_now_requested': True, 'first_contact_sent': True}

            messages.append(message)
            if client_name:
                updates['client_name'] = client_name
            state_manager.update_fields(phone_number, updates)

            return {
                "messages": messages,
                "new_state": "COLLECTING",
                "actions": []
            }
        else:
            fallback_slots = get_next_available_time_slots(
                now,
                num_slots=3,
                check_calendar=True,
                persist_slots_for_phone=phone_number,
                persist_slots_state_manager=state_manager,
                **slot_kwargs_from_booking_state(state),
            )
            if fallback_slots:
                try:
                    from config import get_current_incall_location
                    from core.webform_security import get_webform_url
                    location = get_current_incall_location() or {}
                    webform_url = get_webform_url(phone_number)
                except Exception as e:
                    logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                    webform_url = f"{get_base_url()}/booking"
                    location = {}
                if is_explicit_incall:
                    message = greetings.get_available_now_3slot_message(
                        time_slots=fallback_slots,
                        city=location.get('city', ''),
                        hotel_name=location.get('hotel_name', ''),
                        client_name=client_name or '',
                        is_outcall=False,
                        address=location.get('address', ''),
                        webform_url=webform_url,
                        fully_booked_tonight=_tonight_query,
                    )
                elif is_explicit_outcall:
                    message = greetings.get_available_now_3slot_message(
                        time_slots=fallback_slots,
                        city=location.get('city', ''),
                        hotel_name=location.get('hotel_name', ''),
                        client_name=client_name or '',
                        is_outcall=True,
                        address=location.get('address', ''),
                        webform_url=webform_url,
                        fully_booked_tonight=_tonight_query,
                    )
                else:
                    message = greetings.get_available_now_3slot_message(
                        time_slots=fallback_slots,
                        city=location.get('city', ''),
                        hotel_name=location.get('hotel_name', ''),
                        client_name=client_name or '',
                        is_outcall=False,
                        address=location.get('address', ''),
                        webform_url=webform_url,
                        fully_booked_tonight=_tonight_query,
                    )
            else:
                message = "Sorry, I'm fully booked for the next few hours. Please try again later or check back tomorrow."
            messages.append(message)
            return {
                "messages": messages,
                "new_state": "NEW",
                "actions": []
            }
