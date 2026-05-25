"""
handlers/new_conv/greeting_v2.py

V2 AI-powered greeting handler for NEW state.

The AI is given the escort's REAL working hours, next available calendar slots,
and current location so it can naturally answer any question a new client might
ask — including "What time do you start?", "Are you free tonight?", "When do
you usually work?" — without needing hardcoded rules or regex patterns.

Only messages that contain EXPLICIT booking details (a specific clock time like
"3pm", a duration like "1 hour", or a direct "book/appointment" request) are
routed to the v1 handler so the calendar, slot, and outcall logic runs unchanged.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("adella_chatbot.handlers.new_conv.greeting_v2")

_PLAIN_GREETING_RE = re.compile(
    r"^\s*(?:hi|hi there|hello|hello there|hey|hey there|hiya|"
    r"good morning|good afternoon|good evening)[\s.!?]*$",
    re.IGNORECASE,
)

# Route to the v1 calendar handler when the client has given EXPLICIT booking
# details OR is asking about availability.  Availability questions ("are you
# free later?", "are you available this afternoon?") must use the real
# calendar-backed v1 handler — never AI, which can fabricate specific times.
_EXPLICIT_BOOKING_RE = re.compile(
    r"(?:"
    r"\d{1,2}(?::\d{2})?\s*(?:am|pm)"           # specific clock: 3pm, 8:30am
    r"|at\s+\d{1,2}(?::\d{2})?(?:\s*(?:am|pm))?"  # "at 3", "at 8pm"
    r"|\d+\s*(?:hour|hr|hours|hrs|min|mins|minutes?)\b"  # duration: 1 hour
    r"|\b(?:book(?:ing)?|appointment)\b"          # "I want to book"
    r"|\b(?:incall|outcall|my place|my hotel|my room)\b"
    r"|come to me|come over"
    r"|\bare\s+you\s+(?:free|available)\b"        # "are you free", "are you available"
    r"|\byou\s+(?:free|available)\b"              # "you free?", "you available?"
    r"|\bfree\s+(?:later|today|tonight|tomorrow|this\s+\w+|now)\b"  # "free later/today/tonight/this afternoon"
    r"|\bavailable\s+(?:later|today|tonight|tomorrow|this\s+\w+|now)\b"
    r"|\bany\s+(?:openings?|availability|slots?)\b"
    r")",
    re.IGNORECASE,
)


def handle_greeting_v2(booking_ctx) -> tuple[str, dict[str, Any]]:
    """
    V2 entry point for (NEW, greeting).

    Returns ``(event, response_dict)`` as required by ``Router.route_v2``.

    Decision tree for first messages (first_contact_sent is False):
    1. Plain greeting only ("Hi", "Hello") → lightweight AI warm reply, stay in NEW.
    2. Any other message without explicit booking details → AI with FULL context
       (hours, slots, location) so the AI can answer any question naturally.
       Sets first_contact_sent so the follow-up routes correctly.
    3. Explicit booking details (specific time, duration, "I want to book") → v1
       handler (calendar, slot, outcall logic runs as normal).
    Subsequent messages (first_contact_sent is True) → always v1.
    """
    metadata = getattr(booking_ctx, "metadata", None) or {}
    message: str = metadata.get("message", "")
    booking_data: dict = getattr(booking_ctx, "booking_data", None) or {}
    current_state: str = str(getattr(booking_ctx, "state", "NEW") or "NEW")
    phone_number: str = str(getattr(booking_ctx, "user_id", "") or "")

    is_first_contact = not booking_data.get("first_contact_sent")

    if is_first_contact and not _has_explicit_booking_request(message):
        if _is_plain_greeting_only(message):
            # "Hi" / "Hello" — lightweight warm reply, no scheduling data needed yet.
            ai_reply = _generate_simple_ai_reply(message)
            if ai_reply:
                return "stay", {
                    "messages": [ai_reply],
                    "new_state": None,
                    "actions": ["v2_ai_greeting"],
                }
        else:
            # Any other conversational first message (schedule questions, general
            # chat, etc.) — AI gets real data so it can answer anything naturally.
            ai_reply = _generate_ai_contextual_reply(message, phone_number=phone_number)
            if ai_reply:
                return "stay", {
                    "messages": [ai_reply],
                    "new_state": None,
                    "actions": ["v2_ai_greeting"],
                    "updates": {"first_contact_sent": True},
                }

    # Explicit booking details, subsequent message, or AI failed → v1 pipeline.
    return _delegate_to_v1(booking_ctx, message, current_state)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _is_plain_greeting_only(message: str) -> bool:
    return bool(_PLAIN_GREETING_RE.match((message or "").strip()))


def _has_explicit_booking_request(message: str) -> bool:
    """True for messages with concrete booking details OR availability questions needing a calendar check."""
    return bool(_EXPLICIT_BOOKING_RE.search((message or "").strip()))


def _generate_simple_ai_reply(message: str) -> str | None:
    """Lightweight AI reply for plain greetings — no scheduling data needed."""
    try:
        from services.ai_service import AIService
        ai_service = AIService()
    except Exception as exc:
        logger.warning("greeting_v2: AIService() failed: %s", exc)
        return None

    try:
        from config import get_escort_name
        name = get_escort_name() or "Adella"
    except Exception:
        name = "Adella"

    from core.prompt_registry import append_prompt_metadata, get_runtime_persona_prompt

    persona = get_runtime_persona_prompt()
    base = (
        f"You are {name}, an escort. A new client just sent a greeting. "
        "Reply naturally and warmly — never use robotic or template-sounding language. "
        "Invite them to tell you what they're after. "
        "Do NOT quote any specific rates, times, availability, or addresses. "
        "Keep it to 1–2 casual, friendly sentences."
    )
    system_body = f"{base} {persona}".strip() if persona else base
    system = append_prompt_metadata(system_body, key="v2_greeting")

    try:
        reply = ai_service.chat(message or "Hi", system_prompt=system)
        if reply and isinstance(reply, str) and reply.strip():
            return reply.strip()
    except Exception as exc:
        logger.warning("greeting_v2: simple AI call failed: %s", exc)

    return None


def _generate_ai_contextual_reply(message: str, *, phone_number: str = "") -> str | None:
    """
    AI reply with FULL real-world context — working hours, calendar slots, location.

    This lets the AI naturally answer any question a client might ask without
    relying on regex patterns: schedule questions, availability questions,
    general curiosity about when/where the escort works, etc.
    """
    try:
        from services.ai_service import AIService
        ai_service = AIService()
    except Exception as exc:
        logger.warning("greeting_v2: AIService() failed: %s", exc)
        return None

    try:
        from config import get_escort_name
        name = get_escort_name() or "Adella"
    except Exception:
        name = "Adella"

    context_lines = _gather_greeting_context_lines(phone_number)

    from core.prompt_registry import append_prompt_metadata, get_runtime_persona_prompt

    persona = get_runtime_persona_prompt()
    context_block = "\n".join(context_lines) if context_lines else "(no scheduling data available)"
    base = (
        f"You are {name}, an escort replying to a new client's first SMS message. "
        "Reply naturally and warmly. Never sound robotic or use template language.\n\n"
        "REAL DATA — use this only, do not invent anything:\n"
        f"{context_block}\n\n"
        "Guidelines:\n"
        "- Answer whatever they asked using the real data above.\n"
        "- If they ask about schedule or start time, use Working hours.\n"
        "- If they ask about availability, mention the Next available times.\n"
        "- If it's general chat, invite them to tell you what they need.\n"
        "- Never quote specific dollar rates.\n"
        "- This is SMS — keep it concise, 2–4 short lines.\n"
        "- End with a warm invitation to book or a friendly question."
    )
    system_body = f"{base}\n{persona}".strip() if persona else base
    system = append_prompt_metadata(system_body, key="v2_greeting")

    try:
        reply = ai_service.chat(message, system_prompt=system)
        if reply and isinstance(reply, str) and reply.strip():
            return reply.strip()
    except Exception as exc:
        logger.warning("greeting_v2: contextual AI call failed: %s", exc)

    return None


def _gather_greeting_context_lines(phone_number: str) -> list:
    """Gather real scheduling/location context lines for the AI prompt."""
    lines: list[str] = []
    _extend_optional(lines, _get_working_hours_line())
    _extend_optional(lines, _get_location_line())
    lines.extend(_get_slots_lines(phone_number))
    lines.extend(_get_urls_lines(phone_number))
    return lines


def _extend_optional(target: list, value: "str | None") -> None:
    if value:
        target.append(value)


def _get_working_hours_line() -> "str | None":
    try:
        from config import get_available_hours
        hours = (get_available_hours() or "").strip()
        return f"Working hours: {hours}" if hours else None
    except Exception as exc:
        logger.warning("greeting_v2: get_available_hours failed: %s", exc)
        return None


def _get_location_line() -> "str | None":
    try:
        from config import get_current_incall_location
        loc = get_current_incall_location() or {}
        hotel = (loc.get("hotel_name") or "").strip()
        city = (loc.get("city") or "").strip()
        location_str = f"{hotel}, {city}".strip(", ") if (hotel or city) else ""
        return f"Location: {location_str}" if location_str else None
    except Exception as exc:
        logger.warning("greeting_v2: location fetch failed: %s", exc)
        return None


def _get_slots_lines(phone_number: str) -> list:
    try:
        from datetime import timedelta
        from utils.availability_slots import get_next_available_time_slots
        from utils.timezone import get_current_datetime
        now = get_current_datetime()
        grace = now + timedelta(minutes=30)
        grace = grace.replace(second=0, microsecond=0)
        rem = grace.minute % 15
        if rem:
            grace += timedelta(minutes=15 - rem)
        slots = get_next_available_time_slots(
            now, num_slots=3, check_calendar=True,
            start_from=grace,
            persist_slots_for_phone=phone_number or None,
        )
        label = f"Next available times: {', '.join(s[1] for s in slots)}" if slots else "Next available times: none immediately available"
        return [label]
    except Exception as exc:
        logger.warning("greeting_v2: slot fetch failed: %s", exc)
        return []


def _get_urls_lines(phone_number: str) -> list:
    try:
        from config import get_profile_url
        from core.webform_security import get_webform_url
        result = []
        webform_url = get_webform_url(phone_number) if phone_number else ""
        profile_url = (get_profile_url() or "").strip()
        if webform_url:
            result.append(f"Booking form: {webform_url}")
        if profile_url:
            result.append(f"Profile: {profile_url}")
        return result
    except Exception as exc:
        logger.warning("greeting_v2: URL fetch failed: %s", exc)
        return []


def _delegate_to_v1(
    booking_ctx, message: str, current_state: str
) -> tuple[str, dict[str, Any]]:
    """Fall back to the v1 greeting handler and wrap result as (event, response)."""
    legacy_ctx: dict[str, Any] = {
        "phone_number": str(getattr(booking_ctx, "user_id", "") or ""),
        "message": message,
        "state": getattr(booking_ctx, "booking_data", None) or {},
        **(getattr(booking_ctx, "metadata", None) or {}),
    }

    try:
        from handlers.new_conv.greeting import handle_greeting
        result = handle_greeting(legacy_ctx)
    except Exception as exc:
        logger.error("greeting_v2: v1 delegate failed: %s", exc, exc_info=True)
        result = {"messages": [], "new_state": None, "actions": []}

    from core.state_machine import target_state_to_event

    raw_new_state = result.get("new_state") if isinstance(result, dict) else None
    event = target_state_to_event(current_state, raw_new_state)
    response: dict[str, Any] = {
        "messages": (result.get("messages") or []) if isinstance(result, dict) else [],
        "actions": (result.get("actions") or []) if isinstance(result, dict) else [],
        "new_state": None,
    }
    return event, response
