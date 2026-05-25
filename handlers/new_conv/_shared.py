# ruff: noqa: E402

from utils.log_sanitize import LOG_SUPPRESSED_FMT
"""
NEW state handler - First contact enforcement.
"""

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from config import get_base_url
from templates import field_prompts, greetings
from templates.booking_collection_messages import (
    build_outcall_policy_line,
    build_outcall_slots_message,
    build_verified_address_prompt,
    get_availability_window_label,
    format_slot_list_for_sms,
)
from utils.experiments import first_contact_variant

logger = logging.getLogger("adella_chatbot.handlers.new")

from utils.dinner_date import DINNER_DURATION_MINUTES, is_dinner_date_booking, slot_kwargs_from_booking_state

# Explicitly include _-prefixed helpers so `from _shared import *` works in sub-modules.
__all__ = [
    "logging", "re", "datetime", "timedelta", "Any",
    "get_base_url",
    "field_prompts", "greetings",
    "build_outcall_policy_line", "build_outcall_slots_message",
    "build_verified_address_prompt", "get_availability_window_label", "format_slot_list_for_sms",
    "DINNER_DURATION_MINUTES", "is_dinner_date_booking", "slot_kwargs_from_booking_state",
    "logger",
    "_get_outcall_pricing_defaults",
    "_has_outcall_intent",
    "_build_outside_hours_response",
    "_greeting_fallback_response",
    "_get_incall_first_contact_for_fallback",
    "_outcall_fallback_msg",
    "_new_booking_first_contact",
    "_infer_preferred_date",
]


def _get_outcall_pricing_defaults() -> tuple[int, int]:
    """Return outcall surcharge/deposit with centralized fallback values."""
    try:
        from core.rates_from_config import get_deposit_outcall, get_surcharge

        return int(get_surcharge()), int(get_deposit_outcall())
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        try:
            from core.rates_from_config import get_default_pricing

            defaults = get_default_pricing()
            return int(defaults.get("surcharge", 100)), int(defaults.get("deposit_outcall", 100))
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            return 100, 100


def _has_outcall_intent(message: str) -> bool:
    """Return True when a message clearly indicates outcall intent."""
    text = (message or "").lower()
    if not text:
        return False

    outcall_keywords = [
        'outcall', 'out call', 'out-call', 'outcall to',
        'my place', 'my hotel', 'my room', 'my address', 'my location',
        'my apartment', 'my apt',
        'the hotel', 'a hotel', 'my motel', 'the motel', 'a motel',
        'airbnb', 'air bnb', 'my airbnb', 'the airbnb',
        'serviced apartment',
        'come to me', 'come to my', 'come over', 'come here',
        'come see me', 'come and see me', 'see me',
        'visit me', 'you visit', 'you come', 'you travel',
        'can you come', 'can you travel', 'do you travel', 'do you outcall',
        'are you mobile', 'mobile service',
        'travel to me', 'to me', 'to my', 'at my',
        'where i am', 'home visit', 'hotel visit', 'do outcalls',
    ]
    if any(keyword in text for keyword in outcall_keywords):
        return True

    location_hint_patterns = [
        r"\bi'?m\s+at\s+",
        r"\blocated\s+at\s+",
        r"\bstaying\s+at\s+",
        r"\bat\s+the\s+\w+",
        r"\bchecked\s+(?:in|into)\b",
    ]
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in location_hint_patterns)


def _build_outside_hours_response(context: dict[str, Any]) -> dict[str, Any] | None:
    """
    Check if the requested time (or now+30min for available-now requests) is outside available
    hours. Returns an outside-hours response dict only when the time being checked is outside.
    Returns None if within hours, no hours configured, or parsing fails (fail open).
    """
    try:
        from datetime import timedelta as _td_ohr

        from config import get_current_incall_location as _gcil_ohr
        from config import get_profile_url as _gpr_ohr
        from core.webform_security import generate_secure_token as _gst_ohr
        from handlers.booking_collection import check_and_format_outside_hours
        from utils.timezone import get_current_datetime as _gcd_ohr

        _now = _gcd_ohr()
        message = context.get('message') or ''
        phone_number = context.get('phone_number', '')

        # Use the time the client actually requested, not the current time.
        # "Can I come at 3pm?" → check 3pm. Only fall back to now+30min for
        # "available now" messages where no specific time is mentioned.
        _check = None
        try:
            from utils.time_parser import infer_requested_datetime_for_booking
            _inferred = infer_requested_datetime_for_booking(message, _now)
            if _inferred is not None and (_inferred - _now).total_seconds() > 0:
                _check = _inferred
        except Exception:
            pass

        if _check is None:
            _check = _now + _td_ohr(minutes=30)

        _loc = _gcil_ohr() or {}
        _tok = _gst_ohr(phone_number, use_short_url=True)
        _wf = f"{get_base_url()}/b/{_tok['short_code']}" if (_tok and isinstance(_tok, dict) and _tok.get('short_code')) else f"{get_base_url()}/booking"
        _is_outcall = _has_outcall_intent(message)
        from templates.greetings import extract_client_name as _ecn_ohr
        _within, _msg, _, _ = check_and_format_outside_hours(
            {
                'client_name': _ecn_ohr(message) or '',
                'incall_outcall': 'outcall' if _is_outcall else 'incall',
                'date': _check.date(),
                'time': (_check.hour, _check.minute),
            },
            webform_url=_wf,
            profile_url=_gpr_ohr() or '',
            city=_loc.get('city', ''),
            address=(_loc.get('address') or '').strip(),
            venue_name=(_loc.get('hotel_name') or _loc.get('display_name') or '').strip(),
            phone_number=phone_number,
            state_manager=context.get('state_manager'),
            hours_setting_default='',
        )
        if _within:
            return None
        return {"messages": [_msg], "new_state": "COLLECTING", "actions": []}
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return None




def _greeting_fallback_response(context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Safe first-contact response when greeting handler fails. Avoids system error message."""
    try:
        if context and _has_outcall_intent(context.get('message', '')):
            return {
                "messages": [_outcall_fallback_msg()],
                "new_state": "COLLECTING",
                "actions": [],
            }
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
    return {
        "messages": [
            field_prompts.get_ask_date_time_duration_prompt(
                is_outcall=context and _has_outcall_intent(context.get('message', ''))
            )
        ],
        "new_state": "COLLECTING",
        "actions": [],
    }


def _get_incall_first_contact_for_fallback(context: dict[str, Any]) -> str | None:
    """
    Build incall first-contact message for use when book_appointment/ask_availability
    handlers fail. Returns None if template generation fails so caller can use a safe fallback.
    """
    try:
        from config import get_available_hours, get_current_incall_location, get_profile_url
        from core.webform_security import get_webform_url
        location = get_current_incall_location()
        city = location.get('city') or ''
        hotel_name = location.get('hotel_name') or ''
        display_name = location.get('display_name') or hotel_name
        available_hours = get_available_hours()
        profile_url = get_profile_url() or ''
        phone_number = context.get('phone_number', '')
        message = context.get('message') or ''
        client_name = greetings.extract_client_name(message)
        webform_url = get_webform_url(phone_number)
        return greetings.get_first_contact_message(
            city=city,
            hotel_name=hotel_name,
            location_description=display_name,
            available_hours=available_hours,
            profile_url=profile_url,
            booking_type="incall",
            webform_url=webform_url,
            client_name=client_name or '',
            address=location.get('address') or '',
            persist_slots_for_phone=phone_number or None,
            persist_slots_state_manager=context.get('state_manager'),
        )
    except Exception as e:
        logger.warning("_get_incall_first_contact_for_fallback failed: %s", e)
        return None


_DAY_NAME_TO_WEEKDAY: "dict[str, int]" = {
    'monday': 0, 'mon': 0,
    'tuesday': 1, 'tue': 1, 'tues': 1,
    'wednesday': 2, 'wed': 2,
    'thursday': 3, 'thu': 3, 'thur': 3, 'thurs': 3,
    'friday': 4, 'fri': 4,
    'saturday': 5, 'sat': 5,
    'sunday': 6, 'sun': 6,
}


def _infer_preferred_date(message: str, now: "datetime") -> "date | None":
    """
    Return the date of a day preference mentioned in the message (e.g. 'wednesday', 'tomorrow'),
    or None if no day reference is found.  Does NOT require a clock time.
    """
    msg_lower = (message or "").lower()
    if re.search(r'\btomorrow\b', msg_lower):
        return (now + timedelta(days=1)).date()
    for name, weekday in _DAY_NAME_TO_WEEKDAY.items():
        if re.search(rf'\b{re.escape(name)}\b', msg_lower):
            days_ahead = (weekday - now.weekday()) % 7
            return (now + timedelta(days=days_ahead)).date()
    # "this weekend" / "the weekend" → next Saturday
    if re.search(r'\b(this |the )?weekend\b', msg_lower):
        days_ahead = (5 - now.weekday()) % 7 or 7
        return (now + timedelta(days=days_ahead)).date()
    # "next week" → next Monday
    if re.search(r'\bnext\s+week\b', msg_lower):
        days_ahead = (7 - now.weekday()) % 7 or 7
        return (now + timedelta(days=days_ahead)).date()
    # "end of the week" / "end of week" → next Friday
    if re.search(r'\bend\s+of\s+(the\s+)?week\b', msg_lower):
        days_ahead = (4 - now.weekday()) % 7 or 7
        return (now + timedelta(days=days_ahead)).date()
    return None


def _new_booking_first_contact(
    context: "dict[str, Any]",
    *,
    booking_type: "str | None" = None,
    experience_type: "str | None" = None,
    default_duration: "int | None" = None,
    force_outcall: bool = False,
    dinner_window: bool = False,
    extra_opener: "str | None" = None,
    lead_with_location: bool = False,
) -> "dict[str, Any]":
    """
    Universal first-contact handler implementing the golden rules for all NEW-state experiences.

    Golden rules:
    - No specific time mentioned → show 3 next available slots
    - Specific time requested + available → ✅ confirm and continue collecting
    - Specific time requested + not available → ❌ + 3 alternatives near the requested time
    - Outcall keywords detected → show outcall policy (15km + surcharge + deposit), NOT incall location

    Args:
        booking_type: e.g. 'doubles_mff', 'msog', None for generic
        experience_type: DB value for experience_type field
        default_duration: override duration in minutes (e.g. 120 for dinner)
        force_outcall: always treat as outcall (dinner date)
        dinner_window: limit slots to 5–9pm window
        extra_opener: extra line after "Hi [name]" (e.g. "Doubles are so much fun!")
    """
    try:
        return _new_booking_first_contact_impl(
            context,
            booking_type=booking_type,
            experience_type=experience_type,
            default_duration=default_duration,
            force_outcall=force_outcall,
            dinner_window=dinner_window,
            extra_opener=extra_opener,
            lead_with_location=lead_with_location,
        )
    except Exception as exc:
        logger.exception("_new_booking_first_contact failed: %s", exc)
        return _greeting_fallback_response(context)


def _new_booking_first_contact_impl(
    context: "dict[str, Any]",
    *,
    booking_type: "str | None" = None,
    experience_type: "str | None" = None,
    default_duration: "int | None" = None,
    force_outcall: bool = False,
    dinner_window: bool = False,
    extra_opener: "str | None" = None,
    lead_with_location: bool = False,
) -> "dict[str, Any]":
    from config import get_current_incall_location, get_profile_url
    from services.calendar_service import check_conflict, find_alternative_slots
    from templates.booking_collection_messages import (
        build_outcall_policy_line,
        format_requested_time_unavailable_line,
        format_yes_time_available_short,
    )
    from utils.availability_slots import format_slot_display_short, get_next_available_time_slots, weekday_abbrev_3
    from utils.time_parser import infer_requested_datetime_for_booking
    from utils.timezone import get_current_datetime

    # Outside hours → return before doing anything else
    outside = _build_outside_hours_response(context)
    if outside:
        return outside

    phone_number = context['phone_number']
    state = context.get('state') or {}
    state_manager = context['state_manager']
    raw_message = (context.get('message') or '').strip()

    is_outcall = force_outcall or _has_outcall_intent(raw_message)

    client_name = state.get('client_name') or greetings.extract_client_name(raw_message)
    name_part = f" {client_name}" if client_name else ""

    location = get_current_incall_location() or {}
    city = (location.get('city') or '').strip()
    hotel_name = (location.get('hotel_name') or location.get('display_name') or '').strip()
    address = (location.get('address') or '').strip()
    profile_url = (get_profile_url() or '').strip()
    try:
        from core.rates_from_config import get_deposit_outcall, get_outcall_travel_surcharge_for_booking

        _policy_bf = {
            "incall_outcall": "outcall" if is_outcall else "incall",
            "booking_type": (booking_type or "") or ("dinner_date" if dinner_window else ""),
            "experience_type": experience_type or "",
        }
        surcharge = (
            int(get_outcall_travel_surcharge_for_booking(_policy_bf)) if is_outcall else 0
        )
        deposit = int(get_deposit_outcall())
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        surcharge, deposit = _get_outcall_pricing_defaults()

    from core.webform_security import get_webform_url
    webform_url = get_webform_url(phone_number)

    now = get_current_datetime()
    duration = default_duration or 60

    # --- Time extraction & calendar check ---
    extracted_dt = infer_requested_datetime_for_booking(raw_message, now=now)

    free_line = ""
    unavailable = False
    date_str: "str | None" = None
    time_tuple: "tuple | None" = None
    slot_lines: "list[str]" = []
    offer_dts: "list" = []

    _outside_hours_str = ""

    if extracted_dt is not None:
        bk: "dict[str, Any]" = {
            'date': extracted_dt.strftime('%Y-%m-%d'),
            'time': (extracted_dt.hour, extracted_dt.minute),
            'duration': duration,
            'incall_outcall': 'outcall' if is_outcall else 'incall',
        }
        if booking_type:
            bk['booking_type'] = booking_type

        # Check requested time against admin-configured available hours before hitting calendar.
        _req_within_hours = True
        is_available = False
        try:
            from handlers.booking_collection import check_and_format_outside_hours as _cafoh
            _req_bk_fields = {
                'client_name': client_name or '',
                'incall_outcall': 'outcall' if is_outcall else 'incall',
                'date': extracted_dt.date(),
                'time': (extracted_dt.hour, extracted_dt.minute),
            }
            _req_within_hours, _, _req_ah, _ = _cafoh(
                _req_bk_fields,
                webform_url=webform_url,
                profile_url=profile_url,
                city=city,
                address=address,
                phone_number=context['phone_number'],
                state_manager=context['state_manager'],
                hours_setting_default='',
            )
            if not _req_within_hours:
                _outside_hours_str = _req_ah or ""
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)

        if not _req_within_hours:
            unavailable = True
        else:
            try:
                conflict_type, _ = check_conflict(bk)
                is_available = conflict_type == 'none'
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                is_available = False

        if is_available:
            free_line = format_yes_time_available_short(extracted_dt.hour, extracted_dt.minute)
            date_str = bk['date']
            time_tuple = (extracted_dt.hour, extracted_dt.minute)
        else:
            unavailable = True
            try:
                offer_dts = list(find_alternative_slots(bk, max_results=3))
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                offer_dts = []

    # Fetch next available slots when: no specific time given, OR busy but find_alternative_slots returned nothing
    if not free_line and not offer_dts:
        _btype = 'dinner_date' if dinner_window else None
        preferred_date = _infer_preferred_date(raw_message, now)
        raw_slots: "list" = []
        if preferred_date is not None:
            try:
                from datetime import datetime as _dt, time as _time
                tz = now.tzinfo
                _day_start = _dt.combine(preferred_date, _time(0, 0))
                if tz:
                    try:
                        _day_start = tz.localize(_day_start)
                    except Exception as e:
                        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                        _day_start = _day_start.replace(tzinfo=tz)
                _day_end = _day_start + timedelta(days=1)
                raw_slots = get_next_available_time_slots(
                    now, num_slots=3, check_calendar=True, booking_type=_btype,
                    start_from=_day_start, end_by=_day_end,
                    persist_slots_for_phone=phone_number,
                    persist_slots_state_manager=state_manager,
                )
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                raw_slots = []
        if not raw_slots:
            try:
                raw_slots = get_next_available_time_slots(
                    now,
                    num_slots=3,
                    check_calendar=True,
                    booking_type=_btype,
                    persist_slots_for_phone=phone_number,
                    persist_slots_state_manager=state_manager,
                )
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                raw_slots = []
        offer_dts = [dt for dt, _ in raw_slots[:3]]
        if not unavailable:
            slot_lines = [s for _, s in raw_slots[:3]]

    # Format unavailable-path slot lines (busy time → alternatives)
    if unavailable and offer_dts and not slot_lines:
        for dt in offer_dts[:3]:
            try:
                slot_lines.append(format_slot_display_short(dt))
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                try:
                    slot_lines.append(
                        f"{weekday_abbrev_3(dt)} {dt.strftime('%d %b %I:%M%p').replace(' 0', ' ')}"
                    )
                except Exception as e:
                    logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)

    # --- Build message ---
    client_context = context.get("client_context") or {}
    try:
        is_returning_client = int(client_context.get("total_bookings") or 0) > 0
    except Exception:
        is_returning_client = False

    opener = f"Hi{name_part}"
    if is_returning_client:
        opener = f"{opener}, welcome back"
    if first_contact_variant(phone_number) == "warmer":
        opener = f"{opener}, thanks for messaging me"
    if extra_opener:
        opener = f"{opener} {extra_opener}"

    def _build_incall_location_line() -> str:
        location_parts = [p for p in [hotel_name, address] if p]
        location_detail = " ".join(location_parts) if location_parts else ""
        city_already_in_detail = city and city.lower() in location_detail.lower()
        if location_detail and city and not city_already_in_detail:
            return f"I'm located at {location_detail} {city}"
        if location_detail:
            return f"I'm located at {location_detail}"
        if city:
            return f"I'm currently in {city}"
        return ""

    parts: "list[str]" = []
    incall_location_line = _build_incall_location_line() if not is_outcall else ""

    if lead_with_location and not is_outcall:
        location_intro = opener
        if incall_location_line:
            location_intro = f"{location_intro} {incall_location_line}"
        if slot_lines:
            slot_block = "\n".join(f"\u2022 {line}" for line in slot_lines)
            label_slots = [(dt, slot_lines[idx]) for idx, dt in enumerate(offer_dts[:len(slot_lines)])]
            now_label = get_availability_window_label(label_slots, now=now)
            parts.append(
                f"{location_intro}\n\nIf you would like to make a booking I'm available at these times {now_label}:\n\n{slot_block}"
            )
        else:
            parts.append(location_intro)
        if profile_url:
            parts.append(profile_url)
        parts.append("Let me know what time suits you?")
        parts.append(f"Or alternatively you can make a booking using my webform\n{webform_url}")
        message = "\n\n".join(part for part in parts if part)
        updates: "dict[str, Any]" = {
            'first_contact_sent': True,
            'incall_outcall': 'incall',
        }
        if booking_type:
            updates['booking_type'] = booking_type
        if experience_type:
            updates['experience_type'] = experience_type
        if default_duration:
            updates['duration'] = default_duration
        if date_str:
            updates['date'] = date_str
        if time_tuple is not None:
            updates['time'] = time_tuple
        if client_name:
            updates['client_name'] = client_name
        if offer_dts:
            updates['offered_slot_hours'] = [dt.hour for dt in offer_dts[:3]]
            updates['offered_slot_minutes'] = [dt.minute for dt in offer_dts[:3]]
            updates['offered_slot_date'] = offer_dts[0].strftime('%Y-%m-%d')

        state_manager.update_fields(phone_number, updates)
        return {"messages": [message], "new_state": "COLLECTING", "actions": []}

    if free_line:
        parts.append(f"{opener}\n\n{free_line}")
    elif unavailable and slot_lines:
        slot_block = "\n".join(f"\u2022 {line}" for line in slot_lines)
        if extracted_dt is not None:
            _ul = format_requested_time_unavailable_line(extracted_dt.hour, extracted_dt.minute)
            if _outside_hours_str:
                parts.append(
                    f"{opener}\n\n{_ul} - my hours are {_outside_hours_str}."
                    f" Here are my next available times:\n\n{slot_block}"
                )
            else:
                parts.append(f"{opener}\n\n{_ul}, but I have these slots open:\n\n{slot_block}")
        else:
            parts.append(
                f"{opener}\n\n"
                f"❌ Unfortunately that time isn't available, but I have these slots open:\n\n{slot_block}"
            )
    elif slot_lines:
        slot_block = "\n".join(f"\u2022 {line}" for line in slot_lines)
        label_slots = [(dt, slot_lines[idx]) for idx, dt in enumerate(offer_dts[:len(slot_lines)])]
        now_label = get_availability_window_label(label_slots, now=now)
        parts.append(f"{opener}\n\nHere are the times I have available {now_label}:\n\n{slot_block}")
    else:
        parts.append(opener)

    if profile_url:
        parts.append(profile_url)

    if is_outcall:
        parts.append(build_outcall_policy_line(surcharge, deposit, city))
    else:
        if incall_location_line:
            parts.append(incall_location_line)

    parts.append(f"I STRONGLY recommend booking through my webform:\n{webform_url}")
    if free_line:
        if is_outcall:
            parts.append("How long would you like to book for, and what's your address?")
        else:
            parts.append("How long would you like to book for?")
    else:
        if is_outcall:
            parts.append("What time works for you, and what's your address?")
        else:
            parts.append("What time works for you, and how long would you like to book for?")

    message = "\n\n".join(parts)

    # --- State updates ---
    updates: "dict[str, Any]" = {
        'first_contact_sent': True,
        'incall_outcall': 'outcall' if is_outcall else 'incall',
    }
    if booking_type:
        updates['booking_type'] = booking_type
    if experience_type:
        updates['experience_type'] = experience_type
    if default_duration:
        updates['duration'] = default_duration
    if date_str:
        updates['date'] = date_str
    if time_tuple is not None:
        updates['time'] = time_tuple
    if client_name:
        updates['client_name'] = client_name
    if offer_dts:
        updates['offered_slot_hours'] = [dt.hour for dt in offer_dts[:3]]
        updates['offered_slot_minutes'] = [dt.minute for dt in offer_dts[:3]]
        updates['offered_slot_date'] = offer_dts[0].strftime('%Y-%m-%d')

    state_manager.update_fields(phone_number, updates)
    return {"messages": [message], "new_state": "COLLECTING", "actions": []}


def _outcall_fallback_msg() -> str:
    """Safe first-response outcall message when template generation fails. No external calls that can raise."""
    try:
        from config import get_available_hours
        hours = get_available_hours()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        hours = "most evenings"
    try:
        from core.rates_from_config import get_deposit_outcall, get_surcharge
        surcharge, deposit = get_surcharge(), get_deposit_outcall()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        surcharge, deposit = _get_outcall_pricing_defaults()
    return (
        f"Hi! I'd love to help with an outcall. I'm available {hours}. "
        f"Outcalls are within 15km, with a ${surcharge} surcharge and ${deposit} deposit. "
        "To book I need time, duration and your hotel/address. "
        f"Or use the webform: {get_base_url()}/booking"
    )

