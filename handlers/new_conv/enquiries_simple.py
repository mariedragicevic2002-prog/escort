# ruff: noqa: F401,F403,F405
from handlers.new_conv._shared import *  # noqa: F401,F403
from handlers.new_conv.greeting import handle_greeting
from handlers.new_conv.booking import handle_book_appointment
from handlers.new_conv.availability_stages import handle_ask_availability
from typing import Any

import logging

from utils.log_sanitize import LOG_SUPPRESSED_FMT

logger = logging.getLogger("adella_chatbot.enquiries")


def handle_ask_rates(context: dict[str, Any]) -> dict[str, Any]:
    """Handle ask_rates intent in NEW state."""
    state = context.get('state') or {}
    from config import get_profile_url

    def _build_profile_rates_message(*, webform_url: str, profile_url: str) -> str:
        message = (
            "Hi thanks for your enquiry. For a full list of my rates and experiences I offer, "
            "check out my profile below:"
        )
        if profile_url:
            message += f"\n\n{profile_url}"
        message += "\n\nIf you would like to make a booking text me back"
        if webform_url:
            message += f" or instead use my booking webform {webform_url}"
        return message

    if not state.get('first_contact_sent'):
        from templates.greetings import extract_client_name

        phone_number = context.get("phone_number", "")
        state_manager = context.get("state_manager")
        client_name = extract_client_name(context.get("message", "") or "")
        updates: dict[str, Any] = {"first_contact_sent": True}
        if client_name:
            updates["client_name"] = client_name
        if state_manager and phone_number:
            state_manager.update_fields(phone_number, updates)
        try:
            from core.webform_security import get_webform_url

            webform_url = get_webform_url(phone_number)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            webform_url = ""

        profile_url = get_profile_url() or ""
        return {
            "messages": [_build_profile_rates_message(webform_url=webform_url, profile_url=profile_url)],
            "new_state": None,
            "actions": [],
        }
    phone_number = context.get("phone_number", "")
    try:
        from core.webform_security import get_webform_url

        webform_url = get_webform_url(phone_number)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        webform_url = ""

    profile_url = get_profile_url() or ""
    return {
        "messages": [_build_profile_rates_message(webform_url=webform_url, profile_url=profile_url)],
        "new_state": None,
        "actions": []
    }


def _special_intro_then_collecting_flow(
    context: dict[str, Any],
    intro_messages: list[str],
    state_updates: dict[str, Any],
) -> dict[str, Any]:
    """
    Keep special-booking intro copy, then continue through standard COLLECTING flow.
    """
    state_manager = context['state_manager']
    phone_number = context['phone_number']

    if not state_manager.update_fields(phone_number, state_updates):
        logger.error(
            "special_intro_then_collecting_flow: update_fields failed for %s — "
            "check DB schema (migrations/schema.sql - dinner columns). keys=%s",
            phone_number, list(state_updates.keys()),
        )
        return {
            "messages": [m for m in intro_messages if m],
            "new_state": "COLLECTING",
            "actions": [],
        }

    from handlers import booking_collection

    collecting_context = dict(context)
    collecting_context['state'] = state_manager.get_state(phone_number) or {}
    try:
        collecting_result = booking_collection.handle_provide_field(collecting_context) or {}
    except Exception as e:
        logger.exception(
            "special_intro_then_collecting_flow: handle_provide_field failed after intro for %s: %s",
            phone_number, e,
        )
        collecting_result = {}

    merged_messages = [m for m in intro_messages if m]
    for msg in collecting_result.get("messages", []) or []:
        if msg and msg not in merged_messages:
            merged_messages.append(msg)

    return {
        "messages": merged_messages,
        "new_state": collecting_result.get("new_state") or "COLLECTING",
        "actions": collecting_result.get("actions", []),
    }


def handle_location_enquiry(context: dict[str, Any]) -> dict[str, Any]:
    """Handle location enquiry — golden-rule first contact (location included in message)."""
    return _new_booking_first_contact(context, lead_with_location=True)


def handle_rate_negotiation(context: dict[str, Any]) -> dict[str, Any]:
    """Handle rate negotiation attempts."""
    from config import get_base_url, get_profile_url
    from templates.greetings import extract_client_name
    from templates.special_bookings import get_rate_negotiation_template

    client_name = extract_client_name(context.get('message', ''))
    state = context.get('state') or {}
    if not client_name:
        client_name = state.get('client_name', '')

    profile_url = get_profile_url() or ""
    base_url = get_base_url() or ""
    experience_url = f"{base_url}/experience" if base_url else ""

    phone_number = context.get('phone_number', '')
    try:
        from core.webform_security import get_webform_url
        webform_url = get_webform_url(phone_number)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        webform_url = f"{base_url}/booking"

    message = get_rate_negotiation_template(
        client_name=client_name,
        profile_url=profile_url,
        experience_url=experience_url,
        webform_url=webform_url,
    )
    return {"messages": [message], "new_state": None, "actions": []}


def handle_enquiry_keyword(context: dict[str, Any]) -> dict[str, Any]:
    """Client message starts with ENQUIRY — structured question lane (not booking slot blast)."""
    import re

    from templates.enquiry_templates import (
        get_enquiry_prompt_message,
        get_enquiry_question_received_message,
    )

    raw = (context.get("message") or "").strip()
    m = re.match(r"(?is)^\s*enquiry\b\s*(.*)$", raw)
    body = (m.group(1) if m else "").strip()
    # Meaningful question already included — acknowledge instead of repeating ENQUIRY how-to.
    if len(body) >= 8 or len(body.split()) >= 2:
        msg = get_enquiry_question_received_message(body)
    else:
        msg = get_enquiry_prompt_message()

    return {
        "messages": [msg],
        "new_state": None,
        "actions": [],
    }


def handle_wrong_number_opt_out(context: dict[str, Any]) -> dict[str, Any]:
    """Wrong-number apology — short polite close without availability templates."""
    _ = context
    return {
        "messages": [
            "No worries - sounds like this reached the wrong person. Sorry for the inconvenience; "
            "feel free to ignore or delete this thread."
        ],
        "new_state": None,
        "actions": [],
    }


def handle_new_ambiguous(context: dict[str, Any]) -> dict[str, Any]:
    """Low-signal NEW messages should request actionable booking details."""
    _ = context
    return {
        "messages": [
            "I didn't quite catch that. If you'd like to book, send your preferred date, time, duration, and whether you'd like incall or outcall."
        ],
        "new_state": None,
        "actions": [],
    }


def handle_service_inquiry(context: dict[str, Any]) -> dict[str, Any]:
    """Handle service inquiry in NEW/COLLECTING state — AI-powered natural answer."""
    message = (context.get("message") or "").strip()
    phone_number = context.get("phone_number", "")
    state = context.get('state') or {}
    state_manager = context.get("state_manager")

    # Mark first contact so follow-up routes correctly
    if not state.get('first_contact_sent') and state_manager and phone_number:
        state_manager.update_fields(phone_number, {"first_contact_sent": True})

    ai_reply = _generate_ai_service_reply(message, phone_number=phone_number)
    if ai_reply:
        return {
            "messages": [ai_reply],
            "new_state": None,
            "actions": [],
        }

    # AI unavailable → template fallback
    from templates.service_descriptions import get_all_experiences_description
    return {
        "messages": [get_all_experiences_description(include_profile_link=True)],
        "new_state": None,
        "actions": [],
    }


def _generate_ai_service_reply(message: str, *, phone_number: str = "") -> str | None:
    """
    AI-generated reply for service inquiries.

    The AI is given the real service descriptions so it can answer specific
    questions naturally (e.g. "Do you offer anal and deepthroat with PSE?")
    rather than just dumping a URL.
    """
    try:
        from services.ai_service import AIService
        ai_service = AIService()
    except Exception as exc:
        logger.warning("enquiries: AIService() failed: %s", exc)
        return None

    try:
        from config import get_escort_name
        name = get_escort_name() or "Adella"
    except Exception:
        name = "Adella"

    try:
        from config import get_profile_url
        profile_url = (get_profile_url() or "").strip()
    except Exception:
        profile_url = ""

    try:
        from core.webform_security import get_webform_url
        webform_url = get_webform_url(phone_number) if phone_number else ""
    except Exception:
        webform_url = ""

    try:
        from templates.service_descriptions import (
            get_dgfe_description,
            get_gfe_description,
            get_pse_description,
        )
        pse_desc = get_pse_description(include_profile_link=False)
        gfe_desc = get_gfe_description(include_profile_link=False)
        dgfe_desc = get_dgfe_description(include_profile_link=False)
        services_block = f"{gfe_desc}\n\n{dgfe_desc}\n\n{pse_desc}"
    except Exception as exc:
        logger.warning("enquiries: service descriptions failed: %s", exc)
        services_block = "(service descriptions unavailable)"

    from core.prompt_registry import append_prompt_metadata, get_runtime_persona_prompt
    persona = get_runtime_persona_prompt()

    url_line = ""
    if profile_url:
        url_line += f"Profile (for full details): {profile_url}"
    if webform_url:
        url_line += f"\nBooking form: {webform_url}"

    base = (
        f"You are {name}, an escort. A client has asked about the services you offer.\n\n"
        "REAL SERVICE DESCRIPTIONS — use these to answer their question:\n"
        f"{services_block}\n\n"
        f"{url_line}\n\n"
        "Guidelines:\n"
        "- Answer their specific question warmly and naturally — e.g. if they asked about "
        "  a specific act, confirm whether it is or isn't in a given experience.\n"
        "- Sound human, playful, and confident — not robotic or corporate.\n"
        "- After answering, point them to the Profile URL for the full breakdown.\n"
        "- Invite them to book at the end.\n"
        "- Never quote specific dollar rates.\n"
        "- SMS format — keep it to 3–5 short lines.\n"
        "- Do NOT describe anything that is NOT listed in the service descriptions above."
    )
    system_body = f"{base}\n{persona}".strip() if persona else base
    system = append_prompt_metadata(system_body, key="service_inquiry")

    try:
        reply = ai_service.chat(message, system_prompt=system)
        if reply and isinstance(reply, str) and reply.strip():
            return reply.strip()
    except Exception as exc:
        logger.warning("enquiries: service AI call failed: %s", exc)

    return None


def handle_new_conversation(context: dict[str, Any]) -> dict[str, Any]:
    """Main handler for new conversation state."""
    message = context.get('message', '').strip().lower()

    if any(word in message for word in ['hi', 'hello', 'hey', 'greetings']):
        return handle_greeting(context)
    elif any(word in message for word in ['book', 'appointment', 'see', 'meet']):
        return handle_book_appointment(context)
    elif any(word in message for word in ['available', 'when', 'time', 'schedule']):
        return handle_ask_availability(context)
    else:
        return handle_greeting(context)


def handle_flirt(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle flirtatious / casual-sexual messages in any state.

    Gives a warm, playful response that acknowledges the vibe without engaging
    in extended roleplay, and steers the conversation back toward booking.
    Does NOT advance the state machine or start booking collection.
    """
    try:
        from config import get_escort_name
        name = get_escort_name() or "Adella"
    except Exception:
        name = "Adella"

    try:
        from services.ai_service import AIService
        from core.prompt_registry import append_prompt_metadata, get_runtime_persona_prompt

        ai = AIService()
        persona = get_runtime_persona_prompt()
        message = (context.get("message") or "").strip()

        system_body = (
            f"You are {name}, a confident and playful escort. A client has sent a flirtatious "
            "or sexually casual message. Respond in character — warm, cheeky, and self-assured — "
            "but keep it short (1–2 sentences max) and steer back to whether they'd like to book. "
            "Do NOT start asking for booking details yet. Do NOT sound corporate or robotic. "
            "Do NOT say anything explicit — keep it suggestive but tasteful."
        )
        if persona:
            system_body = f"{system_body}\n{persona}"
        system = append_prompt_metadata(system_body, key="flirt")

        reply = ai.chat(message, system_prompt=system)
        if reply and isinstance(reply, str) and reply.strip():
            return {"messages": [reply.strip()], "new_state": None, "actions": []}
    except Exception as exc:
        logger.warning("handle_flirt: AI call failed: %s", exc)

    # Fallback if AI is unavailable
    return {
        "messages": [
            f"Ha, I like your style 😏 If you're keen to arrange something, just let me know "
            f"when you're thinking and I'll see what I can do."
        ],
        "new_state": None,
        "actions": [],
    }
