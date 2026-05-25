"""
POST_BOOKING state handler - Post-appointment interactions.
Limits interactions after appointment, sends enquiry prompt after 3+ messages.
"""

import logging
from datetime import date as date_type, datetime, time as time_type, timedelta
from typing import Any

from templates.post_booking_messages import (
    DETAILS_COMPLETE_CHECKING,
    GOODBYE_RESPONSE,
    REBOOK_DETAILS_COMPLETE_CHECKING,
    THANK_YOU_RESPONSE,
)

logger = logging.getLogger("escort_chatbot.handlers.post_booking")

# Maximum messages before sending enquiry prompt
MAX_POST_BOOKING_MESSAGES = 3


def _coerce_booking_end_dt(state: dict[str, Any]) -> datetime | None:
    """Best-effort booking end timestamp from state date/time/duration."""
    raw_date = state.get("date")
    raw_time = state.get("time")
    duration = int(state.get("duration") or 60)
    try:
        if isinstance(raw_date, datetime):
            d = raw_date.date()
        elif isinstance(raw_date, date_type):
            d = raw_date
        elif isinstance(raw_date, str):
            d = datetime.fromisoformat(raw_date[:10]).date()
        else:
            return None

        if isinstance(raw_time, datetime):
            t = raw_time.time()
        elif isinstance(raw_time, time_type):
            t = raw_time
        elif isinstance(raw_time, (tuple, list)) and len(raw_time) >= 2:
            t = time_type(int(raw_time[0]), int(raw_time[1]))
        elif isinstance(raw_time, str):
            s = raw_time.strip()
            if ":" in s:
                hh, mm = s.split(":", 1)
                t = time_type(int(hh), int(mm[:2]))
            else:
                return None
        else:
            return None

        start = datetime.combine(d, t)
        return start + timedelta(minutes=max(15, duration))
    except Exception:
        return None


def _build_post_booking_greeting(state: dict[str, Any]) -> str:
    """Personalized post-booking greeting using client name and time since booking."""
    name = (state.get("client_name") or "").strip()
    name_part = f" {name}" if name else ""

    try:
        from utils.timezone import get_current_datetime

        now = get_current_datetime()
        booking_end = _coerce_booking_end_dt(state)
        if booking_end is None:
            return f"Hi{name_part}! Great to hear from you.\n\nWould you like to book again?"
        if now.tzinfo and booking_end.tzinfo is None:
            booking_end = booking_end.replace(tzinfo=now.tzinfo)
        hours_since = (now - booking_end).total_seconds() / 3600
    except Exception:
        return f"Hi{name_part}! Great to hear from you.\n\nWould you like to book again?"

    if hours_since <= 12:
        opener = f"Hi{name_part}! Hope you had a great time. 💕"
    elif hours_since >= 48:
        opener = f"Hi{name_part}! Great to hear from you again."
    else:
        opener = f"Hi{name_part}! Hope you've been well."
    return f"{opener}\n\nWould you like to book again?"


def _apply_smart_defaults_after_reset(context: dict[str, Any], extracted: dict[str, Any]) -> None:
    """
    Apply smart defaults after clear_booking() to reduce recollection for returning clients.
    Only fills fields not already extracted from the current inbound message.
    """
    state_manager = context["state_manager"]
    phone_number = context["phone_number"]
    defaults = dict((context.get("smart_defaults") or {}))
    if not defaults:
        cc = context.get("conversation_context")
        if cc and hasattr(cc, "get_smart_defaults"):
            try:
                defaults = dict(cc.get_smart_defaults(phone_number) or {})
            except Exception:
                defaults = {}
    if not defaults:
        return

    allowed = {"duration", "experience_type", "incall_outcall"}
    updates: dict[str, Any] = {}
    for key in allowed:
        if key in extracted and extracted.get(key) not in (None, ""):
            continue
        val = defaults.get(key)
        if val not in (None, ""):
            updates[key] = val
    if updates:
        state_manager.update_fields(phone_number, updates)


def _increment_post_booking_messages(context: dict[str, Any]) -> int:
    """Increment and persist the post-booking message counter."""
    phone_number = context['phone_number']
    state = context['state']
    state_manager = context['state_manager']
    post_booking_messages = state.get('post_booking_messages', 0) + 1
    state_manager.update_fields(phone_number, {'post_booking_messages': post_booking_messages})
    return post_booking_messages


def handle_greeting(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle greeting in POST_BOOKING state.

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    post_booking_messages = _increment_post_booking_messages(context)

    # Check if we should send enquiry prompt
    if post_booking_messages >= MAX_POST_BOOKING_MESSAGES:
        return send_enquiry_prompt(context)

    message = _build_post_booking_greeting(context.get("state") or {})

    return {
        "messages": [message],
        "new_state": None,  # Stay in POST_BOOKING
        "actions": []
    }


def handle_book_appointment(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle new booking request after appointment.
    Extracts date/time/duration from the message so we never ask for them again.
    """
    phone_number = context['phone_number']
    state_manager = context['state_manager']
    message = context.get('message', '')

    import config as cfg
    from booking.field_collector import FieldCollector
    from templates.field_prompts import build_missing_fields_message

    # Extract from message before clearing so we don't lose what they said
    ai_service = context.get('ai_service')
    field_collector = FieldCollector(cfg, ai_service=ai_service)
    extracted = field_collector.extract_fields(message, {})

    # Clear old booking and apply any extracted fields
    state_manager.clear_booking(phone_number)
    if extracted:
        updates = {k: v for k, v in extracted.items() if v is not None and (v != '' or k not in ('outcall_address',))}
        if updates:
            state_manager.update_fields(phone_number, updates)
    _apply_smart_defaults_after_reset(context, extracted)

    current_fields = state_manager.get_booking_fields(phone_number)
    missing = field_collector.get_missing_fields(current_fields)

    if not missing:
        return {
            "messages": [DETAILS_COMPLETE_CHECKING],
            "new_state": "CHECKING_AVAILABILITY",
            "actions": ["check_calendar"]
        }
    from templates.field_prompts import get_prompt_for_missing_core_fields
    from utils.dinner_date import is_dinner_date_booking

    _exp_ok = bool((current_fields.get("experience_type") or "").strip()) or is_dinner_date_booking(current_fields)
    _is_oc = str((current_fields.get("incall_outcall") or "")).lower() == "outcall"
    missing_prompt = build_missing_fields_message(
        missing, experience_already_set=_exp_ok, is_outcall=_is_oc
    ) or get_prompt_for_missing_core_fields(missing, experience_already_set=_exp_ok, is_outcall=_is_oc)
    return {
        "messages": [missing_prompt],
        "new_state": "COLLECTING",
        "actions": []
    }


def handle_ask_rates(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle rate inquiry after booking.

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    _increment_post_booking_messages(context)

    from core.rates_from_config import format_extended_rates_message, format_rates_message
    rates_message = format_rates_message(include_extended=False) + "\n\n" + format_extended_rates_message() + "\n\nWould you like to book again?"

    return {
        "messages": [rates_message],
        "new_state": None,
        "actions": []
    }


def handle_ask_availability(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle availability check after booking.
    Extracts date/time/duration from the message so we never ask for them again.
    """
    phone_number = context['phone_number']
    state_manager = context['state_manager']
    message = context.get('message', '')

    import config as cfg
    from booking.field_collector import FieldCollector
    from templates.field_prompts import build_missing_fields_message

    # Extract from message first, then clear previous booking and apply extracted
    ai_service = context.get('ai_service')
    field_collector = FieldCollector(cfg, ai_service=ai_service)
    extracted = field_collector.extract_fields(message, {})
    state_manager.clear_booking(phone_number)
    if extracted:
        updates = {k: v for k, v in extracted.items() if v is not None and (v != '' or k not in ('outcall_address',))}
        if updates:
            state_manager.update_fields(phone_number, updates)
    _apply_smart_defaults_after_reset(context, extracted)
    current_fields = state_manager.get_booking_fields(phone_number)
    missing = field_collector.get_missing_fields(current_fields)

    if not missing:
        return {
            "messages": [REBOOK_DETAILS_COMPLETE_CHECKING],
            "new_state": "CHECKING_AVAILABILITY",
            "actions": ["check_calendar"]
        }
    from templates.field_prompts import get_prompt_for_missing_core_fields
    from utils.dinner_date import is_dinner_date_booking

    _exp_ok = bool((current_fields.get("experience_type") or "").strip()) or is_dinner_date_booking(current_fields)
    _is_oc = str((current_fields.get("incall_outcall") or "")).lower() == "outcall"
    missing_prompt = build_missing_fields_message(
        missing, experience_already_set=_exp_ok, is_outcall=_is_oc
    ) or get_prompt_for_missing_core_fields(missing, experience_already_set=_exp_ok, is_outcall=_is_oc)
    return {
        "messages": [missing_prompt],
        "new_state": "COLLECTING",
        "actions": []
    }


def handle_service_inquiry(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle service inquiry after booking.
    Forward complex inquiries to escort.

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    post_booking_messages = _increment_post_booking_messages(context)

    # Check if we should forward to escort
    if post_booking_messages >= MAX_POST_BOOKING_MESSAGES:
        return send_enquiry_prompt(context)

    # Provide service descriptions (post-booking, so no marketing)
    from templates.service_descriptions import get_all_experiences_description
    message = get_all_experiences_description(include_profile_link=True)
    message += "\n\nWould you like to book again?"

    return {
        "messages": [message],
        "new_state": None,
        "actions": []
    }


def handle_provide_field(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle generic messages in POST_BOOKING state.

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    message_text = context.get('message', '').lower()

    post_booking_messages = _increment_post_booking_messages(context)

    # Check if we should send enquiry prompt
    if post_booking_messages >= MAX_POST_BOOKING_MESSAGES:
        return send_enquiry_prompt(context)

    # Check for common intents
    if any(word in message_text for word in ['thanks', 'thank you', 'amazing', 'great', 'loved']):
        return {
            "messages": [THANK_YOU_RESPONSE],
            "new_state": None,
            "actions": []
        }

    elif any(word in message_text for word in ['book', 'appointment', 'when', 'available']):
        # Redirect to booking flow
        return handle_book_appointment(context)

    else:
        # Generic response
        return {
            "messages": [
                "Thanks for reaching out! Would you like to book again?"
            ],
            "new_state": None,
            "actions": []
        }


def handle_goodbye(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle goodbye in POST_BOOKING state.
    Return to NEW state.

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    phone_number = context['phone_number']
    state_manager = context['state_manager']

    # Clear post-booking state
    state_manager.clear_booking(phone_number)

    return {
        "messages": [GOODBYE_RESPONSE],
        "new_state": "NEW",
        "actions": []
    }


# ============================================================================
# Helper Functions
# ============================================================================

def send_enquiry_prompt(context: dict[str, Any]) -> dict[str, Any]:
    """
    Send enquiry prompt and forward complex queries to escort.

    This is sent after 3+ messages in POST_BOOKING state to limit
    extended conversations after the appointment.

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    phone_number = context['phone_number']
    state_manager = context['state_manager']
    context.get('state') or {}

    logger.info(f"Sending enquiry prompt to {phone_number} - message limit reached")

    from templates.enquiry_templates import get_post_booking_limit_message
    message = get_post_booking_limit_message()

    # Don't clear booking - stay in POST_BOOKING but with enquiry prompt sent
    state_manager.update_fields(phone_number, {
        'enquiry_prompt_sent': True
    })

    return {
        "messages": [message],
        "new_state": None,  # Stay in POST_BOOKING
        "actions": []
    }

