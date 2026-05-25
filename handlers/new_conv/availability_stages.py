# ruff: noqa: F401,F403,F405
from handlers.new_conv._shared import *  # noqa: F401,F403
from typing import Any

import logging
import re

from templates.booking_collection_messages import pick_outcall_venue_display_name
from utils.log_sanitize import LOG_SUPPRESSED_FMT

logger = logging.getLogger("adella_chatbot.availability")

DEFAULT_AVAILABLE_DAYS = "7 days a week"
PREFERRED_TONIGHT_START_HOUR = 20

# ---------------------------------------------------------------------------
# String constants for repeated outcall-intent phrases (Sonar S1192)
# ---------------------------------------------------------------------------
_PHRASE_OUT_CALL = "out call"
_PHRASE_COME_SEE_ME = "come see me"
_PHRASE_COME_AND_SEE_ME = "come and see me"
_PHRASE_SEE_ME = "see me"
_PHRASE_TO_MY = "to my"
_PHRASE_MY_PLACE = "my place"
_PHRASE_MY_HOTEL = "my hotel"

# Shared outcall-intent phrase lists (Sonar S1192)
_OUTCALL_CTX_WORDS = (
    "outcall",
    _PHRASE_OUT_CALL,
    "come to",
    "come over",
    _PHRASE_COME_SEE_ME,
    _PHRASE_COME_AND_SEE_ME,
    _PHRASE_SEE_ME,
    _PHRASE_TO_MY,
    "to mine",
    _PHRASE_MY_PLACE,
    _PHRASE_MY_HOTEL,
    "my address",
)
_AVAILABLE_NOW_OUTSIDE_HOURS_KW = (
    "outcall",
    _PHRASE_OUT_CALL,
    "outcall to",
    _PHRASE_MY_PLACE,
    _PHRASE_MY_HOTEL,
    "my room",
    "my address",
    "come to me",
    "come to my",
    "come over",
    _PHRASE_COME_SEE_ME,
    _PHRASE_COME_AND_SEE_ME,
    _PHRASE_SEE_ME,
    "visit me",
    "can you come",
    "can you travel",
    "do you travel",
    "do you outcall",
    _PHRASE_TO_MY,
    "to me",
    "at my",
    "home visit",
    "hotel visit",
)
_EXPLICIT_OUTCALL_KEYWORDS = (
    "outcall",
    _PHRASE_OUT_CALL,
    "out-call",
    "outcall to",
    _PHRASE_TO_MY,
    "to me",
    "at my",
    "come to me",
    "come to my",
    "come to my place",
    _PHRASE_MY_PLACE,
    _PHRASE_MY_HOTEL,
    "my room",
    "my location",
    "my apartment",
    "my apt",
    _PHRASE_COME_SEE_ME,
    _PHRASE_COME_AND_SEE_ME,
    _PHRASE_SEE_ME,
    "visit me",
    "can you come",
    "can you travel",
    "do you travel",
    "do you outcall",
    "hotel visit",
    "home visit",
)


# ---------------------------------------------------------------------------
# Start-time question detection
# ---------------------------------------------------------------------------
_START_TIME_QUESTION_RE = re.compile(
    r'(?:'
    r'what\s+time\b.{0,40}\b(?:start(?:ing)?|begin(?:ning)?|open(?:ing)?|work(?:ing)?)\b'
    r'|when\b.{0,30}\b(?:start(?:ing)?|begin(?:ning)?|open(?:ing)?)\b'
    r'|(?:start(?:ing)?|begin(?:ning)?)\s+(?:work|today|tonight|now)\b'
    r')',
    re.IGNORECASE,
)


def _is_start_time_question(message: str) -> bool:
    """Return True when the client is asking what time the escort starts work today."""
    return bool(_START_TIME_QUESTION_RE.search((message or '').strip()))


def _format_hour_12(hour_24: int) -> str:
    """Format a 24-hour integer as a 12-hour label, e.g. 13 → '1pm'."""
    if hour_24 == 0:
        return "midnight"
    if hour_24 == 12:
        return "noon"
    if hour_24 < 12:
        return f"{hour_24}am"
    return f"{hour_24 - 12}pm"


def _build_start_time_response(context: dict[str, Any]) -> dict[str, Any] | None:
    """
    Build a reply to "What time are you starting work today?" type questions.

    Returns a response dict (messages/new_state/actions) on success, or None if
    the business hours are not configured (callers should fall through to the
    normal flow).
    """
    try:
        from datetime import timedelta

        from config import get_current_incall_location, get_profile_url
        from core.webform_security import get_webform_url
        from utils.availability_slots import (
            get_business_hours,
            get_next_available_time_slots,
            normalize_business_hours_pair,
        )
        from utils.timezone import get_current_datetime

        bh = normalize_business_hours_pair(get_business_hours())
        if bh is None:
            return None

        start_hour = bh[0]
        now = get_current_datetime()
        phone_number = context.get('phone_number', '')
        message = context.get('message', '')
        state = context.get('state') or {}
        state_manager = context.get('state_manager')

        client_name = greetings.extract_client_name(message)
        location = get_current_incall_location()
        webform_url = get_webform_url(phone_number)
        profile_url = get_profile_url()

        # Work out when today's shift starts
        start_today = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        already_started = now >= start_today

        if already_started:
            # Show slots from now+30 min
            slot_start = now + timedelta(minutes=30)
            slot_start = slot_start.replace(second=0, microsecond=0)
            rem = slot_start.minute % 15
            if rem != 0:
                slot_start += timedelta(minutes=15 - rem)
        else:
            # Show slots starting from the configured start hour
            slot_start = start_today

        time_slots = get_next_available_time_slots(
            now, num_slots=3, check_calendar=True,
            start_from=slot_start,
            persist_slots_for_phone=phone_number,
            persist_slots_state_manager=state_manager,
            **slot_kwargs_from_booking_state(state),
        )

        start_label = _format_hour_12(start_hour)
        hi = greetings.hi_name_spaced_lead(client_name)
        if already_started:
            opener = f"{hi}I started at {start_label} today"
        else:
            opener = f"{hi}I start at {start_label} today"

        avail_msg = greetings.get_available_now_message(
            city=location.get('city', ''),
            hotel_name=location.get('hotel_name', ''),
            client_name='',  # opener already contains the greeting
            is_outcall=False,
            address=location.get('address', ''),
            has_duration=False,
            webform_url=webform_url,
            profile_url=profile_url,
            time_slots=time_slots,
            fully_booked_tonight=False,
        )

        # Strip the duplicate "Hey" / "Hi" greeting from the slot message
        stripped = avail_msg
        for prefix in ("Hey! ", "Hey\n", "Hi! ", "Hi\n"):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):].lstrip()
                break

        full_msg = f"{opener}! {stripped}"

        if state_manager and phone_number:
            try:
                updates: dict[str, Any] = {'first_contact_sent': True}
                if client_name:
                    updates['client_name'] = client_name
                state_manager.update_fields(phone_number, updates)
            except Exception as _e:
                logger.warning(LOG_SUPPRESSED_FMT, _e, exc_info=False)

        return {"messages": [full_msg], "new_state": "COLLECTING", "actions": []}
    except Exception as exc:
        logger.warning("_build_start_time_response failed: %s", exc)
        return None


def _booking_fields_for_outcall_deposit_copy(state: dict, duration_minutes: int) -> dict:
    """Merge state + duration for ``greetings`` outcall deposit line (dinner, doubles, etc.)."""
    bt = (state.get("booking_type") or "").strip().lower()
    if is_dinner_date_booking(state) and not bt:
        bt = "dinner_date"
    return {
        "incall_outcall": "outcall",
        "booking_type": bt,
        "experience_type": state.get("experience_type") or "",
        "duration": int(duration_minutes),
    }


def _stage_avail_all_fields_present(
    context: dict,
    message: str,
    state: dict,
    state_manager,
    phone_number: str,
    current_fields: dict,
) -> dict:
    """Handle ask-availability when all mandatory fields are already extracted.
    Always returns a response dict (never None)."""
    from config import get_available_hours, get_current_incall_location, get_profile_url
    from core.webform_security import get_webform_url
    from handlers.booking_collection import (
        _format_perfect_timing_line,
        check_and_format_outside_hours,
    )

    location = get_current_incall_location()
    client_name = greetings.extract_client_name(message)
    webform_url = get_webform_url(phone_number)
    _vn_loc = (location.get("hotel_name") or location.get("display_name") or "").strip()
    _st_loc = (location.get("address") or "").strip()
    is_within_hours, outside_hours_msg, _, _ = check_and_format_outside_hours(
        current_fields,
        webform_url=webform_url,
        profile_url=get_profile_url() or '',
        city=location.get('city', ''),
        address=_st_loc,
        venue_name=_vn_loc,
        phone_number=phone_number,
        state_manager=state_manager,
    )

    inferred_booking_type = (
        current_fields.get('incall_outcall') or ('outcall' if _has_outcall_intent(message) else 'incall')
    ).lower()
    if (
        not state.get('first_contact_sent')
        and inferred_booking_type != "outcall"
    ):
        first_contact = greetings.get_first_contact_message(
            city=location.get('city', ''),
            hotel_name=location.get('hotel_name', ''),
            location_description=location.get('display_name', location.get('hotel_name', '')),
            available_hours=get_available_hours(),
            profile_url=get_profile_url(),
            booking_type='incall',
            webform_url=webform_url,
            client_name=client_name,
            address=location.get('address', ''),
            persist_slots_for_phone=phone_number,
            persist_slots_state_manager=state_manager,
        )
        updates = {'first_contact_sent': True, 'incall_outcall': 'incall'}
        if client_name:
            updates['client_name'] = client_name
        state_manager.update_fields(phone_number, updates)
        return {"messages": [first_contact], "new_state": "COLLECTING", "actions": []}

    if not is_within_hours:
        updates = {'first_contact_sent': True, 'date': None, 'time': None}
        if client_name:
            updates['client_name'] = client_name
        state_manager.update_fields(phone_number, updates)
        return {"messages": [outside_hours_msg], "new_state": "COLLECTING", "actions": []}

    perfect_timing = _format_perfect_timing_line(
        current_fields,
        client_name=client_name or "",
        phone_number=phone_number,
        webform_url=webform_url,
    )
    updates = {'first_contact_sent': True}
    if client_name:
        updates['client_name'] = client_name
    state_manager.update_fields(phone_number, updates)
    return {"messages": [perfect_timing], "new_state": "CHECKING_AVAILABILITY", "actions": ["check_calendar"]}


def _build_grace_start(now, timedelta_cls) -> Any:
    grace_start = now + timedelta_cls(minutes=30)
    grace_start = grace_start.replace(second=0, microsecond=0)
    grace_remainder = grace_start.minute % 15
    if grace_remainder != 0:
        grace_start = grace_start + timedelta_cls(minutes=15 - grace_remainder)
    return grace_start


def _message_has_outcall_context(message: str) -> bool:
    return any(word in (message or '').lower() for word in _OUTCALL_CTX_WORDS)


def _build_first_contact_updates(client_name: str | None, **extra: Any) -> dict[str, Any]:
    updates: dict[str, Any] = {'first_contact_sent': True, **extra}
    if client_name:
        updates['client_name'] = client_name
    return updates


def _handle_tonight_grace_availability(
    message: str,
    state: dict,
    state_manager,
    phone_number: str,
    location: dict,
    client_name: str | None,
    messages: list,
    now,
    end_hour: int | None,
    webform_url: str,
) -> dict[str, Any]:
    from config import get_profile_url
    from utils.availability_slots import get_next_available_time_slots

    grace_start = _build_grace_start(now, __import__('datetime').timedelta)
    end_by = now.replace(hour=end_hour or 4, minute=0, second=0, microsecond=0)
    time_slots = get_next_available_time_slots(
        now, num_slots=3, check_calendar=True,
        start_from=grace_start, end_by=end_by,
        persist_slots_for_phone=phone_number,
        persist_slots_state_manager=state_manager,
        **slot_kwargs_from_booking_state(state),
    )
    fully_booked_tonight = False
    if not time_slots:
        fully_booked_tonight = True
        time_slots = get_next_available_time_slots(
            now, num_slots=3, check_calendar=True,
            persist_slots_for_phone=phone_number,
            persist_slots_state_manager=state_manager,
            **slot_kwargs_from_booking_state(state),
        )

    availability_msg = greetings.get_available_now_message(
        city=location.get('city', ''), hotel_name=location.get('hotel_name', ''),
        client_name=client_name, is_outcall=_message_has_outcall_context(message),
        address=location.get('address', ''), has_duration=False,
        webform_url=webform_url, profile_url=get_profile_url(), time_slots=time_slots,
        fully_booked_tonight=fully_booked_tonight,
    )
    messages.append(availability_msg)
    state_manager.update_fields(phone_number, _build_first_contact_updates(client_name))
    return {"messages": messages, "new_state": "COLLECTING", "actions": []}


def _handle_tomorrow_early_availability(
    message: str,
    state: dict,
    state_manager,
    phone_number: str,
    location: dict,
    client_name: str | None,
    messages: list,
    now,
    webform_url: str,
) -> dict[str, Any]:
    from datetime import timedelta

    from config import get_profile_url
    from utils.availability_slots import (
        get_business_hours,
        get_next_available_time_slots,
        normalize_business_hours_pair,
    )

    business_hours = normalize_business_hours_pair(get_business_hours()) or (11, 4)
    service_start = now.replace(hour=int(business_hours[0]), minute=0, second=0, microsecond=0)
    grace_start = _build_grace_start(now, timedelta)
    start_from = service_start if service_start > grace_start else grace_start
    end_by = now.replace(hour=23, minute=59, second=0, microsecond=0)
    if start_from > end_by:
        start_from = grace_start
        end_by = None

    time_slots = get_next_available_time_slots(
        now, num_slots=3, check_calendar=True,
        start_from=start_from, end_by=end_by,
        persist_slots_for_phone=phone_number,
        persist_slots_state_manager=state_manager,
        **slot_kwargs_from_booking_state(state),
    )
    availability_msg = greetings.get_available_now_message(
        city=location.get('city', ''), hotel_name=location.get('hotel_name', ''),
        client_name=client_name, is_outcall=_message_has_outcall_context(message),
        address=location.get('address', ''), has_duration=False,
        webform_url=webform_url, profile_url=get_profile_url(), time_slots=time_slots,
    )
    messages.append(availability_msg)
    state_manager.update_fields(phone_number, _build_first_contact_updates(client_name))
    return {"messages": messages, "new_state": "COLLECTING", "actions": []}


def _check_outside_hours_and_respond(
    message: str,
    state_manager,
    phone_number: str,
    location: dict,
    client_name: str | None,
    inferred_date,
    inferred_hour: int,
    inferred_minute: int,
    webform_url: str,
) -> dict[str, Any] | None:
    from config import get_profile_url
    from handlers.booking_collection import check_and_format_outside_hours

    venue_name = (location.get('hotel_name') or location.get('display_name') or '').strip()
    address = (location.get('address') or '').strip()
    specific_time_fields = {
        'client_name': client_name or '',
        'incall_outcall': 'outcall' if _message_has_outcall_context(message) else 'incall',
        'date': inferred_date,
        'time': (inferred_hour, inferred_minute),
    }
    is_within_hours, outside_msg, _, _ = check_and_format_outside_hours(
        specific_time_fields,
        webform_url=webform_url,
        profile_url=get_profile_url() or '',
        city=location.get('city', ''),
        address=address,
        venue_name=venue_name,
        phone_number=phone_number,
        state_manager=state_manager,
        hours_setting_default='',
        days_setting_default=DEFAULT_AVAILABLE_DAYS,
    )
    if is_within_hours:
        return None

    state_manager.update_fields(
        phone_number,
        _build_first_contact_updates(client_name, date=None, time=None),
    )
    return {"messages": [outside_msg], "new_state": "COLLECTING", "actions": []}


def _extract_specific_time_outcall_address(
    context: dict,
    message: str,
    is_outcall: bool,
) -> str | None:
    if not is_outcall:
        return None

    try:
        import config as _cfg
        from booking.field_collector import FieldCollector

        field_collector = FieldCollector(_cfg, ai_service=context.get('ai_service'))
        extracted = field_collector.extract_fields(message) or {}
        return (extracted.get('outcall_address') or '').strip() or None
    except Exception as exc:
        logger.warning("Could not extract outcall address: %s", exc, exc_info=False)
        return None


def _build_specific_time_outcall_response(
    state_manager,
    phone_number: str,
    location: dict,
    client_name: str | None,
    messages: list,
    inferred_date,
    inferred_hour: int,
    inferred_minute: int,
    extracted_outcall_address: str | None,
    webform_url: str,
) -> dict[str, Any]:
    from booking.field_validator import FieldValidator

    validator = FieldValidator()
    is_valid, error_msg = validator.validate_outcall_address(
        extracted_outcall_address, 'outcall', city=location.get('city', '')
    )
    if is_valid:
        venue_info = getattr(validator, '_last_verified_hotel_info', None) or {}
        verified_address = (venue_info.get('verified_address') or '').strip()
        booking_city = (venue_info.get('city') or location.get('city') or '').strip()
        display_venue = pick_outcall_venue_display_name(
            venue_info, extracted_outcall_address or '', booking_city=booking_city
        )
        msg = build_verified_address_prompt(
            verified_address=verified_address,
            city=booking_city,
            venue_name=display_venue,
        )
    else:
        msg = f"❌ {error_msg}\n\nPlease provide a different address or use the booking webform: {webform_url}"
        extracted_outcall_address = None

    messages.append(msg)
    updates = _build_first_contact_updates(
        client_name,
        incall_outcall='outcall',
        date=inferred_date.strftime('%Y-%m-%d'),
        time=(inferred_hour, inferred_minute),
    )
    if extracted_outcall_address:
        updates['outcall_address'] = extracted_outcall_address
    state_manager.update_fields(phone_number, updates)
    return {"messages": messages, "new_state": "COLLECTING", "actions": []}


def _handle_available_specific_time(
    context: dict,
    message: str,
    state_manager,
    phone_number: str,
    location: dict,
    client_name: str | None,
    messages: list,
    extracted_time,
    inferred_date,
    inferred_hour: int,
    inferred_minute: int,
    is_outcall: bool,
    webform_url: str,
) -> dict[str, Any]:
    from config import get_escort_name, get_profile_url

    logger.info("Specific time %s is AVAILABLE", extracted_time)
    extracted_outcall_address = _extract_specific_time_outcall_address(context, message, is_outcall)
    if is_outcall and extracted_outcall_address:
        return _build_specific_time_outcall_response(
            state_manager,
            phone_number,
            location,
            client_name,
            messages,
            inferred_date,
            inferred_hour,
            inferred_minute,
            extracted_outcall_address,
            webform_url,
        )

    msg = greetings.get_time_requested_available_message(
        requested_datetime=extracted_time,
        city=location.get('city', ''), hotel_name=location.get('hotel_name', ''),
        client_name=client_name, is_outcall=is_outcall,
        address=location.get('address', ''), escort_name=get_escort_name(),
        webform_url=webform_url, profile_url=get_profile_url(),
    )
    messages.append(msg)
    state_manager.update_fields(
        phone_number,
        _build_first_contact_updates(
            client_name,
            incall_outcall='outcall' if is_outcall else 'incall',
            date=inferred_date.strftime('%Y-%m-%d'),
            time=(inferred_hour, inferred_minute),
        ),
    )
    return {"messages": messages, "new_state": "COLLECTING", "actions": []}


def _format_requested_time_label(extracted_time) -> str:
    requested_hour = int(extracted_time.hour)
    requested_minute = int(extracted_time.minute)
    requested_period = 'pm' if requested_hour >= 12 else 'am'
    requested_hour_12 = requested_hour % 12 or 12
    return (
        f"{requested_hour_12}:{requested_minute:02d}{requested_period}"
        if requested_minute
        else f"{requested_hour_12}{requested_period}"
    )


def _handle_unavailable_specific_time(
    state_manager,
    phone_number: str,
    location: dict,
    client_name: str | None,
    messages: list,
    extracted_time,
    booking_details: dict,
    is_outcall: bool,
    webform_url: str,
) -> dict[str, Any]:
    from config import get_escort_name, get_profile_url
    from templates.greetings import build_booking_time_unavailable_message

    logger.info("Specific time %s is NOT available", extracted_time)
    availability_msg, _ = build_booking_time_unavailable_message(
        booking_details,
        _format_requested_time_label(extracted_time),
        city=location.get('city', ''),
        hotel_name=location.get('hotel_name', ''),
        address=location.get('address', ''),
        client_name=client_name or '',
        is_outcall=is_outcall,
        escort_name=get_escort_name(),
        webform_url=webform_url,
        profile_url=get_profile_url(),
    )
    messages.append(availability_msg)
    state_manager.update_fields(phone_number, _build_first_contact_updates(client_name))
    return {"messages": messages, "new_state": "COLLECTING", "actions": []}



def _is_tomorrow_early_exception(message: str) -> bool:
    import re as _re

    return bool(
        _re.search(r'\b(tomorrow|tmrw|tmr)\b', message, _re.IGNORECASE)
        and _re.search(r'\b(midnight|12\s*am|1\s*am|2\s*am|3\s*am)\b', message, _re.IGNORECASE)
    )



def _is_specific_time_tonight_grace_case(
    message: str,
    now,
    end_hour: int | None,
    explicit_clock: bool,
) -> bool:
    from utils.time_parser import is_tonight_request

    return bool(
        end_hour is not None
        and 0 <= now.hour < end_hour
        and is_tonight_request(message)
        and not explicit_clock
    )



def _get_specific_time_booking_resolution(
    message: str,
    state: dict,
    inferred_date,
    inferred_hour: int,
    inferred_minute: int,
    check_conflict,
) -> tuple[dict[str, Any], bool, bool]:
    ask_duration = DINNER_DURATION_MINUTES if is_dinner_date_booking(state) else 60
    ask_incall_outcall = 'outcall' if is_dinner_date_booking(state) else 'incall'
    booking_details = {
        'date': inferred_date.strftime('%Y-%m-%d'),
        'time': (inferred_hour, inferred_minute),
        'duration': ask_duration,
        'incall_outcall': ask_incall_outcall,
    }
    conflict_type, _ = check_conflict(booking_details)
    is_available = conflict_type == 'none'
    is_outcall = is_dinner_date_booking(state) or _message_has_outcall_context(message)
    return booking_details, is_available, is_outcall



def _stage_avail_specific_time(
    context: dict,
    message: str,
    state: dict,
    state_manager,
    phone_number: str,
    location: dict,
    client_name: str | None,
    messages: list,
) -> dict | None:
    """Handle ask-availability when message contains a specific clock time.
    Appends to messages or returns early dict.  Returns None if no specific time."""
    from utils.time_parser import parse_time_from_message, infer_requested_datetime_for_booking, message_has_explicit_clock
    from utils.timezone import get_current_datetime

    specific_hour = parse_time_from_message(message)
    if specific_hour is None:
        return None

    logger.info("Specific time hour detected: %s from message: %s", specific_hour, message)
    try:
        from services.calendar_service import check_conflict
        from utils.availability_slots import get_business_hours, normalize_business_hours_pair

        now = get_current_datetime()
        explicit_clock = message_has_explicit_clock(message)
        business_hours = normalize_business_hours_pair(get_business_hours()) or (11, 4)
        end_hour = int(business_hours[1]) if business_hours and len(business_hours) > 1 else 4

        from core.webform_security import get_webform_url

        webform_url = get_webform_url(phone_number)
        if _is_specific_time_tonight_grace_case(message, now, end_hour, explicit_clock):
            return _handle_tonight_grace_availability(
                message,
                state,
                state_manager,
                phone_number,
                location,
                client_name,
                messages,
                now,
                end_hour,
                webform_url,
            )

        extracted_time = infer_requested_datetime_for_booking(message, now=now)
        if extracted_time is None:
            return None

        inferred_date = extracted_time.date()
        inferred_hour = extracted_time.hour
        inferred_minute = extracted_time.minute
        logger.info("Inferred specific time: %s", extracted_time)

        if _is_tomorrow_early_exception(message):
            return _handle_tomorrow_early_availability(
                message,
                state,
                state_manager,
                phone_number,
                location,
                client_name,
                messages,
                now,
                webform_url,
            )

        outside_hours_response = _check_outside_hours_and_respond(
            message,
            state_manager,
            phone_number,
            location,
            client_name,
            inferred_date,
            inferred_hour,
            inferred_minute,
            webform_url,
        )
        if outside_hours_response is not None:
            return outside_hours_response

        booking_details, is_available, is_outcall = _get_specific_time_booking_resolution(
            message,
            state,
            inferred_date,
            inferred_hour,
            inferred_minute,
            check_conflict,
        )
        if is_available:
            return _handle_available_specific_time(
                context,
                message,
                state_manager,
                phone_number,
                location,
                client_name,
                messages,
                extracted_time,
                inferred_date,
                inferred_hour,
                inferred_minute,
                is_outcall,
                webform_url,
            )
        return _handle_unavailable_specific_time(
            state_manager,
            phone_number,
            location,
            client_name,
            messages,
            extracted_time,
            booking_details,
            is_outcall,
            webform_url,
        )
    except Exception as e:
        logger.warning("Specific time handling failed: %s", e, exc_info=True)
        return None  # Fall through to generic 3-slot template


def _get_generic_slot_window(message: str, now, grace_start):
    from utils.time_parser import get_requested_day_start, get_tonight_slot_window, is_tonight_request

    requested_day_start, requested_day_label, requested_day_end = get_requested_day_start(now, message)
    if is_tonight_request(message):
        tonight_start, tonight_end = get_tonight_slot_window(now)
    elif requested_day_start is not None:
        tonight_start, tonight_end = requested_day_start, requested_day_end
    else:
        tonight_start, tonight_end = grace_start, None
    return tonight_start, requested_day_label, tonight_end



def _merge_slot_candidates(earlier_slots, primary_slots):
    deduped: dict[str, tuple[Any, Any]] = {}
    for dt, label in sorted([*earlier_slots, *primary_slots], key=lambda row: row[0]):
        deduped[getattr(dt, 'isoformat', lambda: str(dt))()] = (dt, label)
    return list(deduped.values())[:3]



def _get_preferred_tonight_slots(
    now,
    state: dict,
    state_manager,
    phone_number: str,
    tonight_start,
    tonight_end,
):
    from utils.availability_slots import get_next_available_time_slots

    preferred_start = tonight_start.replace(
        hour=PREFERRED_TONIGHT_START_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )
    primary_slots = get_next_available_time_slots(
        now, num_slots=3, check_calendar=True,
        start_from=preferred_start, end_by=tonight_end,
        persist_slots_for_phone=phone_number,
        persist_slots_state_manager=state_manager,
        **slot_kwargs_from_booking_state(state),
    )
    if len(primary_slots) >= 3 or not (tonight_start < preferred_start):
        return primary_slots

    earlier_slots = get_next_available_time_slots(
        now, num_slots=3, check_calendar=True,
        start_from=tonight_start, end_by=preferred_start,
        persist_slots_for_phone=phone_number,
        persist_slots_state_manager=state_manager,
        **slot_kwargs_from_booking_state(state),
    )
    return _merge_slot_candidates(earlier_slots, primary_slots)



def _get_generic_time_slots(
    message: str,
    now,
    state: dict,
    state_manager,
    phone_number: str,
    tonight_start,
    tonight_end,
):
    from utils.availability_slots import get_next_available_time_slots
    from utils.time_parser import is_tonight_request

    is_tonight = is_tonight_request(message)
    if is_tonight and tonight_start is not None and tonight_end is not None and now.hour < PREFERRED_TONIGHT_START_HOUR:
        time_slots = _get_preferred_tonight_slots(
            now, state, state_manager, phone_number, tonight_start, tonight_end
        )
    else:
        time_slots = get_next_available_time_slots(
            now, num_slots=3, check_calendar=True,
            start_from=tonight_start, end_by=tonight_end,
            persist_slots_for_phone=phone_number,
            persist_slots_state_manager=state_manager,
            **slot_kwargs_from_booking_state(state),
        )

    fully_booked_tonight = False
    if is_tonight and not time_slots:
        fully_booked_tonight = True
        time_slots = get_next_available_time_slots(
            now, num_slots=3, check_calendar=True,
            persist_slots_for_phone=phone_number,
            persist_slots_state_manager=state_manager,
            **slot_kwargs_from_booking_state(state),
        )
    return time_slots, fully_booked_tonight



def _build_generic_availability_message(
    location: dict,
    client_name: str | None,
    webform_url: str,
    time_slots,
    fully_booked_tonight: bool,
    requested_day_label: str | None,
) -> str:
    from config import get_profile_url

    availability_msg = greetings.get_available_now_message(
        city=location.get('city', ''), hotel_name=location.get('hotel_name', ''),
        client_name=client_name, is_outcall=False,
        address=location.get('address', ''), has_duration=False,
        webform_url=webform_url, profile_url=get_profile_url(), time_slots=time_slots,
        fully_booked_tonight=fully_booked_tonight,
    )
    if requested_day_label:
        availability_msg = availability_msg.replace('available tonight', f'available {requested_day_label}')
        availability_msg = availability_msg.replace('available today', f'available {requested_day_label}')
    return availability_msg



def _build_generic_slot_updates(now, time_slots, client_name: str | None) -> dict[str, Any]:
    return _build_first_contact_updates(
        client_name,
        offered_slot_hours=[dt.hour for dt, _ in time_slots] if time_slots else [],
        offered_slot_minutes=[dt.minute for dt, _ in time_slots] if time_slots else [],
        offered_slot_dates=[dt.strftime('%Y-%m-%d') for dt, _ in time_slots] if time_slots else [],
        offered_slot_date=time_slots[0][0].strftime('%Y-%m-%d') if time_slots else now.strftime('%Y-%m-%d'),
    )



def _stage_avail_generic_slots(
    message: str,
    state: dict,
    state_manager,
    phone_number: str,
    location: dict,
    client_name: str | None,
    messages: list,
) -> None:
    """Show generic 3-slot availability when no specific time was detected.
    Appends to messages in-place; no return value."""
    from datetime import timedelta

    from core.webform_security import get_webform_url
    from utils.timezone import get_current_datetime

    webform_url = get_webform_url(phone_number)
    now = get_current_datetime()
    grace_start = _build_grace_start(now, timedelta)
    tonight_start, requested_day_label, tonight_end = _get_generic_slot_window(message, now, grace_start)
    time_slots, fully_booked_tonight = _get_generic_time_slots(
        message,
        now,
        state,
        state_manager,
        phone_number,
        tonight_start,
        tonight_end,
    )
    messages.append(
        _build_generic_availability_message(
            location,
            client_name,
            webform_url,
            time_slots,
            fully_booked_tonight,
            requested_day_label,
        )
    )
    state_manager.update_fields(
        phone_number,
        _build_generic_slot_updates(now, time_slots, client_name),
    )


def _stage_avail_already_sent(
    context: dict,
    message: str,
    state: dict,
    state_manager,
    phone_number: str,
) -> dict:
    """Handle ask-availability when first contact was already sent (follow-up messages).
    Always returns a response dict."""
    from datetime import timedelta, datetime as _dt
    from config import get_current_incall_location, get_profile_url
    from core.webform_security import get_webform_url
    from utils.availability_slots import get_next_available_time_slots
    from utils.time_parser import get_requested_day_start, parse_time_from_message, infer_time_from_hour
    from utils.timezone import get_current_datetime
    from services.calendar_service import check_conflict

    specific_hour = parse_time_from_message(message)
    if specific_hour is not None:
        logger.info("Specific time detected in follow-up: %s from message: %s", specific_hour, message)
        try:
            now = get_current_datetime()
            inferred_date, inferred_hour = infer_time_from_hour(specific_hour, now)

            import re as _re
            _min_match = _re.search(r'\b\d{1,2}:(\d{2})', message)
            inferred_minute = int(_min_match.group(1)) if _min_match and int(_min_match.group(1)) < 60 else 0

            try:
                from utils.timezone import get_local_timezone

                _escort_tz = get_local_timezone()
                extracted_time = _escort_tz.localize(
                    _dt.combine(inferred_date, _dt.min.time().replace(hour=inferred_hour, minute=inferred_minute))
                )
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                extracted_time = _dt.combine(inferred_date, _dt.min.time().replace(hour=inferred_hour, minute=inferred_minute))

            try:
                conflict_type, conflict_details = check_conflict({
                    'date': inferred_date,
                    'time': (inferred_hour, inferred_minute),
                    'duration': 60,
                    'incall_outcall': state.get('incall_outcall', 'incall'),
                    'outcall_address': None,
                })

                location = get_current_incall_location() or {}
                webform_url = get_webform_url(phone_number)
                client_name = state.get('client_name', '')

                from templates.greetings import get_time_requested_available_message

                if conflict_type == "none":
                    availability_msg = get_time_requested_available_message(
                        requested_datetime=extracted_time,
                        city=location.get('city', ''),
                        hotel_name=location.get('hotel_name', ''),
                        client_name=client_name,
                        is_outcall=state.get('incall_outcall', '').lower() == 'outcall',
                        address=location.get('address', ''),
                        escort_name=None,
                        webform_url=webform_url,
                        profile_url=get_profile_url(),
                    )
                    return {"messages": [availability_msg], "new_state": "COLLECTING", "actions": []}
                else:
                    now = get_current_datetime()
                    _slots = get_next_available_time_slots(
                        now, num_slots=3, check_calendar=True,
                        persist_slots_for_phone=phone_number,
                        persist_slots_state_manager=state_manager,
                        **slot_kwargs_from_booking_state(state),
                    )
                    from templates.booking_collection_messages import format_slot_list_for_sms
                    slots_list = format_slot_list_for_sms(_slots)

                    unavailable_msg = f"""Hi {client_name} ❌ Unfortunately {inferred_hour:02d}:{inferred_minute:02d} isn't available.

Here are my closest available times:{slots_list}

Which time works for you?

Or fill in my booking webform {webform_url}"""

                    return {"messages": [unavailable_msg], "new_state": "COLLECTING", "actions": []}
            except Exception as e:
                logger.warning("Failed to check specific time availability: %s", e, exc_info=False)
        except Exception as e:
            logger.warning("Failed to process specific time request: %s", e, exc_info=False)

    _requested_day_start, _requested_day_label, _requested_day_end = get_requested_day_start(
        get_current_datetime(), message
    )
    if _requested_day_start is not None:
        _now = get_current_datetime()
        _slots = get_next_available_time_slots(
            _now, num_slots=3, check_calendar=True,
            start_from=_requested_day_start, end_by=_requested_day_end,
            persist_slots_for_phone=phone_number,
            persist_slots_state_manager=state_manager,
            **slot_kwargs_from_booking_state(state),
        )
        _webform_url = get_webform_url(phone_number)
        _loc = get_current_incall_location() or {}
        _msg = greetings.get_available_now_message(
            city=_loc.get('city', ''), hotel_name=_loc.get('hotel_name', ''),
            client_name=(state.get('client_name') or ''), is_outcall=False,
            address=_loc.get('address', ''), has_duration=False,
            webform_url=_webform_url, profile_url=get_profile_url(), time_slots=_slots,
        ).replace("available tonight", f"available {_requested_day_label}").replace(
            "available today", f"available {_requested_day_label}"
        )
        return {"messages": [_msg], "new_state": None, "actions": []}

    if (state.get('incall_outcall') == 'outcall') or _has_outcall_intent(message):
        return {"messages": [_outcall_fallback_msg()], "new_state": None, "actions": []}

    _now = get_current_datetime()
    _grace_start = _now + timedelta(minutes=30)
    _grace_start = _grace_start.replace(second=0, microsecond=0)
    _gr = _grace_start.minute % 15
    if _gr != 0:
        _grace_start = _grace_start + timedelta(minutes=15 - _gr)
    _slots = get_next_available_time_slots(
        _now, num_slots=3, check_calendar=True,
        start_from=_grace_start, end_by=None,
        persist_slots_for_phone=phone_number,
        persist_slots_state_manager=state_manager,
        **slot_kwargs_from_booking_state(state),
    )
    _webform_url = get_webform_url(phone_number)
    _loc = get_current_incall_location() or {}
    _msg = greetings.get_available_now_message(
        city=_loc.get('city', ''), hotel_name=_loc.get('hotel_name', ''),
        client_name=(state.get('client_name') or ''), is_outcall=False,
        address=_loc.get('address', ''), has_duration=False,
        webform_url=_webform_url, profile_url=get_profile_url(), time_slots=_slots,
    )
    return {"messages": [_msg], "new_state": None, "actions": []}


def handle_ask_availability(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle ask_availability intent in NEW state.
    Extracts date/time/duration from the client's message so we don't ask again
    if they already said e.g. "Friday at 9pm for 1 hour".
    """
    try:
        return _handle_ask_availability_impl(context)
    except Exception as e:
        logger.exception("handle_ask_availability failed: %s", e)
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
        try:
            from config import get_available_hours
            hours = get_available_hours()
            fallback = (
                f"Hi! I'd love to help. I'm generally available {hours}. "
                f"When were you thinking? You can say e.g. tomorrow at 8pm for 1 hour."
            )
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            fallback = "I'd love to help! When were you thinking? Try e.g. tomorrow at 8pm for 1 hour."
        return {"messages": [fallback], "new_state": "COLLECTING", "actions": []}


def _handle_ask_availability_impl(context: dict[str, Any]) -> dict[str, Any]:
    """Implementation of ask_availability handler."""
    state = context.get('state') or {}
    state_manager = context['state_manager']
    phone_number = context['phone_number']
    message = (context.get('message') or '').strip()

    try:
        from core.classifier import Classifier

        if Classifier(ai_service=context.get("ai_service")).classify(
            message, media_urls=[], context=context or {}
        ) == "dinner_date_enquiry":
            from handlers.new_conv.enquiries import handle_dinner_date_enquiry

            return handle_dinner_date_enquiry(context)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)

    import config as cfg
    from booking.field_collector import FieldCollector
    ai_service = context.get('ai_service')
    field_collector = FieldCollector(cfg, ai_service=ai_service)
    current_fields = state_manager.get_booking_fields(phone_number) or {}
    extracted = field_collector.extract_fields(message, current_fields)
    if extracted:
        ok = state_manager.update_fields(phone_number, extracted)
        if not ok:
            logger.warning(
                "ask_availability: update_fields failed for %s (extracted: %s)",
                phone_number, list(extracted.keys()),
            )
        current_fields = state_manager.get_booking_fields(phone_number) or {}
    missing_fields = field_collector.get_missing_fields(current_fields)
    state = state_manager.get_state(phone_number) or state

    if extracted and not missing_fields:
        return _stage_avail_all_fields_present(
            context, message, state, state_manager, phone_number, current_fields
        )

    messages: list = []
    if not state.get('first_contact_sent'):
        from config import get_current_incall_location
        location = get_current_incall_location()
        client_name = greetings.extract_client_name(message)

        result = _stage_avail_specific_time(
            context, message, state, state_manager, phone_number,
            location, client_name, messages,
        )
        if result is not None:
            return result

        # Try AI with full context before falling back to the generic template.
        # The AI can naturally answer any schedule question ("what time do you
        # start?", "are you free tonight?") using real working hours + slots.
        ai_result = _generate_ai_availability_reply(message, context, phone_number)
        if ai_result is not None:
            return ai_result

        _stage_avail_generic_slots(
            message, state, state_manager, phone_number,
            location, client_name, messages,
        )
    else:
        return _stage_avail_already_sent(context, message, state, state_manager, phone_number)

    return {"messages": messages, "new_state": "COLLECTING", "actions": []}


def _create_availability_ai_service():
    try:
        from services.ai_service import AIService

        return AIService()
    except Exception as exc:
        logger.warning("availability: AIService() failed: %s", exc)
        return None



def _get_availability_ai_name() -> str:
    try:
        from config import get_escort_name

        return get_escort_name() or 'Adella'
    except Exception:
        return 'Adella'



def _append_availability_hours_context(context_lines: list[str]) -> None:
    try:
        from config import get_available_hours

        hours = (get_available_hours() or '').strip()
        if hours:
            context_lines.append(f"Working hours: {hours}")
    except Exception as exc:
        logger.warning("availability AI: get_available_hours failed: %s", exc)



def _append_availability_location_context(context_lines: list[str]) -> None:
    try:
        from config import get_current_incall_location

        loc = get_current_incall_location() or {}
        hotel = (loc.get('hotel_name') or '').strip()
        city = (loc.get('city') or '').strip()
        location_str = f"{hotel}, {city}".strip(', ')
        if location_str:
            context_lines.append(f"Location: {location_str}")
    except Exception as exc:
        logger.warning("availability AI: location fetch failed: %s", exc)



def _append_availability_slots_context(context_lines: list[str], phone_number: str) -> None:
    from datetime import timedelta

    try:
        from utils.availability_slots import get_next_available_time_slots
        from utils.timezone import get_current_datetime

        now = get_current_datetime()
        slots = get_next_available_time_slots(
            now, num_slots=3, check_calendar=True,
            start_from=_build_grace_start(now, timedelta),
            persist_slots_for_phone=phone_number or None,
        )
        if slots:
            context_lines.append(f"Next available times: {', '.join(slot[1] for slot in slots)}")
        else:
            context_lines.append('Next available times: none immediately available')
    except Exception as exc:
        logger.warning("availability AI: slot fetch failed: %s", exc)



def _append_availability_url_context(context_lines: list[str], phone_number: str) -> None:
    try:
        from config import get_profile_url
        from core.webform_security import get_webform_url

        webform_url = get_webform_url(phone_number) if phone_number else ''
        profile_url = (get_profile_url() or '').strip()
        if webform_url:
            context_lines.append(f"Booking form: {webform_url}")
        if profile_url:
            context_lines.append(f"Profile: {profile_url}")
    except Exception as exc:
        logger.warning("availability AI: URL fetch failed: %s", exc)



def _build_availability_ai_system_prompt(name: str, context_lines: list[str]) -> str:
    from core.prompt_registry import append_prompt_metadata, get_runtime_persona_prompt

    persona = get_runtime_persona_prompt()
    context_block = '\n'.join(context_lines)
    base = (
        f"You are {name}, an escort replying to a new client's first SMS.\n\n"
        "REAL DATA — use this only, do not invent times, locations, or prices:\n"
        f"{context_block}\n\n"
        "Guidelines:\n"
        "- Answer whatever they asked using the real data above.\n"
        "- If they ask about start time or schedule, use Working hours.\n"
        "- If they ask about what's available, give them the Next available times.\n"
        "- Never quote dollar rates.\n"
        "- SMS format — keep it concise, 2–4 short lines.\n"
        "- End with a warm invitation to book or a soft question."
    )
    system_body = f"{base}\n{persona}".strip() if persona else base
    return append_prompt_metadata(system_body, key='v2_availability')



def _chat_availability_reply(ai_service, message: str, system_prompt: str) -> str | None:
    try:
        reply = ai_service.chat(message, system_prompt=system_prompt)
        if reply and isinstance(reply, str) and reply.strip():
            return reply.strip()
    except Exception as exc:
        logger.warning("availability AI: chat call failed: %s", exc)
    return None



def _generate_ai_availability_reply(
    message: str, context: dict[str, Any], phone_number: str
) -> dict[str, Any] | None:
    """
    Ask the AI to answer the client's availability question using REAL data.

    Returns a response dict on success, None if AI is unavailable or fails
    (caller will fall through to the template path).
    """
    _ = context
    ai_service = _create_availability_ai_service()
    if ai_service is None:
        return None

    context_lines: list[str] = []
    _append_availability_hours_context(context_lines)
    _append_availability_location_context(context_lines)
    _append_availability_slots_context(context_lines, phone_number)
    _append_availability_url_context(context_lines, phone_number)
    if not context_lines:
        return None

    reply = _chat_availability_reply(
        ai_service,
        message,
        _build_availability_ai_system_prompt(_get_availability_ai_name(), context_lines),
    )
    if reply is None:
        return None

    return {
        "messages": [reply],
        "new_state": "COLLECTING",
        "actions": [],
        "updates": {"first_contact_sent": True},
    }
