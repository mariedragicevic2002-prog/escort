# ruff: noqa: F401,F403,F405
from handlers.new_conv._shared import *  # noqa: F401,F403
from handlers.new_conv.availability_stages import (
    _handle_ask_availability_impl,
)
from typing import Any, Optional

import logging
import re

from utils.log_sanitize import LOG_SUPPRESSED_FMT
logger = logging.getLogger("adella_chatbot.greeting")


_PLAIN_GREETING_ONLY_RE = re.compile(
    r"^\s*(?:hi|hi there|hello|hello there|hey|hey there|hiya|good morning|good afternoon|good evening)[\s.!?]*$",
    re.IGNORECASE,
)


def _is_plain_greeting_only(message: str) -> bool:
    return bool(_PLAIN_GREETING_ONLY_RE.match((message or "").strip()))


def _plain_greeting_reply(message: str) -> str:
    text = (message or "").strip().lower()
    if text.startswith("good morning"):
        return "Good morning to you as well. Did you want to make a booking?"
    if text.startswith("good afternoon"):
        return "Good afternoon to you as well. Did you want to make a booking?"
    if text.startswith("good evening"):
        return "Good evening to you as well. Did you want to make a booking?"
    return "Hey how are you going? Did you want to make a booking?"

def handle_greeting(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle greeting intent in NEW state.
    Detects outcall/incall in first message and sends appropriate first contact.

    Args:
        context: Context dict with phone_number, message, state, etc.

    Returns:
        Dict with messages, new_state, actions
    """
    try:
        return _handle_greeting_impl(context)
    except Exception as e:
        logger.exception("handle_greeting failed: %s", e)
        return _greeting_fallback_response(context)


def _handle_greeting_impl(context: dict[str, Any]) -> dict[str, Any]:
    """Implementation of greeting handler (called from handle_greeting)."""
    state = context.get('state') or {}
    raw_message = context.get('message') or ''

    if not state.get('first_contact_sent') and _is_plain_greeting_only(raw_message):
        return {
            "messages": [_plain_greeting_reply(raw_message)],
            "new_state": None,
            "actions": [],
        }

    # Check if first contact already sent
    if not state.get('first_contact_sent'):
        # Unified golden-rule first contact: checks time, calendar, outcall intent, 3-slot display
        return _new_booking_first_contact(context)
    else:
        # First contact already sent \u2014 delegate to ask_availability handler which
        # checks specific times and sends the proper template.
        try:
            return _handle_ask_availability_impl(context)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            _msg = context.get('message', '')
            _is_oc_prompt = _has_outcall_intent(_msg)
            try:
                _sm, _pn = context.get('state_manager'), context.get('phone_number')
                if _sm and _pn:
                    _bf = _sm.get_booking_fields(_pn) or {}
                    _is_oc_prompt = _is_oc_prompt or str((_bf.get('incall_outcall') or '')).lower() == 'outcall'
            except Exception as _e2:
                logger.warning(LOG_SUPPRESSED_FMT, _e2, exc_info=False)
            return {
                "messages": [field_prompts.get_ask_date_time_duration_prompt(is_outcall=_is_oc_prompt)],
                "new_state": "COLLECTING",
                "actions": []
            }
