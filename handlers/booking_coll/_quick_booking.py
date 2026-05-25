"""
handlers/booking_coll/_quick_booking.py

handle_quick_booking and its private helpers.
"""

import logging
from typing import Any

from templates import field_prompts

from handlers.booking_coll._shared import _extract_and_merge_booking_fields

logger = logging.getLogger("adella_chatbot.handlers.collecting")


def _is_outcall_booking(state_manager, phone_number: str) -> bool:
    f = state_manager.get_booking_fields(phone_number) or {}
    return str(f.get("incall_outcall") or "").lower() == "outcall"


def _quick_booking_first_contact(context: dict, phone_number: str, state: dict, state_manager) -> list:
    """Send first-contact message if not already sent. Returns list of messages to prepend."""
    if state.get("first_contact_sent"):
        return []
    from config import get_available_hours, get_current_incall_location, get_profile_url
    from core.webform_security import get_webform_url
    from templates import greetings

    location = get_current_incall_location() or {}
    client_name = greetings.extract_client_name(context.get("message", ""))
    webform_url = get_webform_url(phone_number)
    first_contact = greetings.get_first_contact_message(
        city=location.get("city", ""),
        hotel_name=location.get("hotel_name", ""),
        location_description=location.get("display_name", location.get("hotel_name", "")),
        available_hours=get_available_hours(),
        profile_url=get_profile_url(),
        booking_type="incall",
        webform_url=webform_url,
        client_name=client_name,
        persist_slots_for_phone=phone_number,
        persist_slots_state_manager=state_manager,
    )
    updates = {"first_contact_sent": True}
    if client_name and greetings.is_valid_client_name(client_name):
        updates["client_name"] = client_name
    state_manager.update_fields(phone_number, updates)
    return [first_contact]


def _build_prefs_str(smart_defaults: dict) -> str:
    """Build a human-readable preferences string from smart defaults."""
    prefs = []
    if smart_defaults.get("duration"):
        h, m = smart_defaults["duration"] // 60, smart_defaults["duration"] % 60
        if m > 0:
            prefs.append(f"{h}h {m}m")
        else:
            prefs.append(f"{h} hour{'s' if h > 1 else ''}")
    if smart_defaults.get("experience_type"):
        prefs.append(smart_defaults["experience_type"])
    if smart_defaults.get("incall_outcall"):
        prefs.append(smart_defaults["incall_outcall"])
    return ", ".join(prefs) if prefs else "your usual preferences"


def _quick_usual(messages: list, phone_number: str, state_manager, conversation_context, missing_fields: list) -> dict:
    """Handle 'book my usual' — pre-fill smart defaults and report what remains."""
    smart_defaults = conversation_context.get_smart_defaults(phone_number)
    if smart_defaults:
        state_manager.update_fields(phone_number, {k: v for k, v in smart_defaults.items()})
        prefs_str = _build_prefs_str(smart_defaults)
        if not missing_fields:
            messages.append(f"\n\nPerfect! I've set up {prefs_str}. I have everything — let me check my availability!")
            return {"messages": messages, "new_state": "CHECKING_AVAILABILITY", "actions": ["check_calendar"]}
        need_msg = field_prompts.build_missing_fields_message(
            missing_fields, is_outcall=_is_outcall_booking(state_manager, phone_number)
        )
        messages.append(f"\n\nPerfect! I've set up {prefs_str}. I still need: {need_msg}")
        return {"messages": messages, "new_state": None, "actions": []}
    if not missing_fields:
        messages.append("\n\nI have everything I need. Let me check my availability!")
        return {"messages": messages, "new_state": "CHECKING_AVAILABILITY", "actions": ["check_calendar"]}
    need_msg = field_prompts.build_missing_fields_message(
        missing_fields, is_outcall=_is_outcall_booking(state_manager, phone_number)
    )
    messages.append("\n\nI don't have your usual preferences saved yet. I still need: " + need_msg)
    return {"messages": messages, "new_state": None, "actions": []}


def _quick_same_as_last(messages: list, phone_number: str, state_manager, client_context: dict, missing_fields: list, field_collector) -> dict:
    """Handle 'same as last time' — copy last booking details and report what remains."""
    booking_history = client_context.get("booking_history", [])
    if booking_history:
        last_booking = booking_history[0]
        updates = {
            k: last_booking[k]
            for k in ("duration", "experience_type", "incall_outcall")
            if last_booking.get(k)
        }
        if updates:
            state_manager.update_fields(phone_number, updates)
            updated_fields = state_manager.get_booking_fields(phone_number)
            missing_fields = field_collector.get_missing_fields(updated_fields)
            if not missing_fields:
                messages.append("\n\nGreat! I've set up the same details as your last booking. I have everything — let me check my availability!")
                return {"messages": messages, "new_state": "CHECKING_AVAILABILITY", "actions": ["check_calendar"]}
            need_msg = field_prompts.build_missing_fields_message(
                missing_fields, is_outcall=_is_outcall_booking(state_manager, phone_number)
            )
            messages.append("\n\nGreat! I've set up the same details as your last booking. I still need: " + need_msg)
            return {"messages": messages, "new_state": None, "actions": []}
    if not missing_fields:
        messages.append("\n\nI couldn't find your last booking details, but I have enough to check. Let me check my availability!")
        return {"messages": messages, "new_state": "CHECKING_AVAILABILITY", "actions": ["check_calendar"]}
    need_msg = field_prompts.build_missing_fields_message(
        missing_fields, is_outcall=_is_outcall_booking(state_manager, phone_number)
    )
    messages.append("\n\nI couldn't find your last booking details. I still need: " + need_msg)
    return {"messages": messages, "new_state": None, "actions": []}


def _quick_next_available(messages: list, phone_number: str, state_manager, conversation_context, missing_fields: list) -> dict:
    """Handle 'next available' — pre-fill defaults and show next 3 slots."""
    from services.calendar_service import find_alternative_slots
    from utils.timezone import get_current_datetime

    smart_defaults = conversation_context.get_smart_defaults(phone_number)
    if smart_defaults:
        state_manager.update_fields(phone_number, smart_defaults)

    now = get_current_datetime()
    default_duration = smart_defaults.get("duration", 60) if smart_defaults else 60
    details = {
        "date": now.date(),
        "time": now.time(),
        "duration": default_duration,
        "experience_type": smart_defaults.get("experience_type") if smart_defaults else None,
        "incall_outcall": smart_defaults.get("incall_outcall", "incall") if smart_defaults else "incall",
    }
    alternatives = find_alternative_slots(details, max_results=3)
    if alternatives:
        alt_times = []
        for alt in alternatives[:3]:
            date_str = alt["date"].strftime("%A, %d %B") if hasattr(alt["date"], "strftime") else str(alt["date"])
            time_str = alt["time"].strftime("%I:%M%p") if hasattr(alt["time"], "strftime") else str(alt["time"])
            alt_times.append(f"{date_str} at {time_str}")
        times_str = "\n".join(f"\u2022 {t}" for t in alt_times)
        messages.append(f"\n\nHere are my next available times:\n\n{times_str}\n\nWhich works for you?")
        return {"messages": messages, "new_state": None, "actions": []}
    if not missing_fields:
        messages.append("\n\nI have everything. Let me check my calendar for next available times!")
        return {"messages": messages, "new_state": "CHECKING_AVAILABILITY", "actions": ["check_calendar"]}
    need_msg = field_prompts.build_missing_fields_message(
        missing_fields, is_outcall=_is_outcall_booking(state_manager, phone_number)
    )
    messages.append("\n\nI'm checking my calendar. I still need: " + need_msg)
    return {"messages": messages, "new_state": None, "actions": []}


def handle_quick_booking(context: dict[str, Any]) -> dict[str, Any]:
    """Handle quick booking shortcuts: 'book my usual', 'same as last time', 'next available'.

    Always extracts date/time/duration from the current message so we never ask for them again.
    """
    from core.conversation_context import ConversationContext

    phone_number = context["phone_number"]
    message = context["message"].lower()
    state_manager = context["state_manager"]
    state = context.get("state", {})

    _, missing_fields, field_collector = _extract_and_merge_booking_fields(context)
    messages = _quick_booking_first_contact(context, phone_number, state, state_manager)

    db_service = context.get("db_service")
    if not db_service:
        if not missing_fields:
            suffix = "\n\nI have everything I need. Let me check my availability!" if messages else "I have everything I need. Let me check my availability!"
            messages.append(suffix)
            return {"messages": messages, "new_state": "CHECKING_AVAILABILITY", "actions": ["check_calendar"]}
        need_msg = field_prompts.build_missing_fields_message(
            missing_fields, is_outcall=_is_outcall_booking(state_manager, phone_number)
        )
        prefix = "\n\n" if messages else ""
        messages.append(f"{prefix}I'd love to help with a quick booking. I still need: {need_msg}")
        return {"messages": messages, "new_state": None, "actions": []}

    conversation_context = ConversationContext(db_service)
    client_context = conversation_context.get_client_context(phone_number)

    if any(kw in message for kw in ("usual", "regular", "normal")):
        return _quick_usual(messages, phone_number, state_manager, conversation_context, missing_fields)

    if any(kw in message for kw in ("same as last", "like last", "repeat")):
        return _quick_same_as_last(messages, phone_number, state_manager, client_context, missing_fields, field_collector)

    if any(kw in message for kw in ("next available", "earliest", "soonest")):
        return _quick_next_available(messages, phone_number, state_manager, conversation_context, missing_fields)

    if not missing_fields:
        suffix = "\n\nI have everything. Let me check my availability!" if messages else "I have everything. Let me check my availability!"
        messages.append(suffix)
        return {"messages": messages, "new_state": "CHECKING_AVAILABILITY", "actions": ["check_calendar"]}
    need_msg = field_prompts.build_missing_fields_message(
        missing_fields, is_outcall=_is_outcall_booking(state_manager, phone_number)
    )
    prefix = "\n\n" if messages else ""
    messages.append(f"{prefix}I'd love to help with a quick booking! I still need: {need_msg}")
    return {"messages": messages, "new_state": None, "actions": []}
