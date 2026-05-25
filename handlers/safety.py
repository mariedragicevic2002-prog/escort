"""
Safety intent handlers - Unsafe requests and abusive behavior.
These handlers work across ALL states (wildcard handlers).
"""

import logging
from typing import Any

from templates.safety_messages import (
    BLOCKED_UNABLE_TO_ASSIST,
    UNSAFE_REQUEST_RESPONSE,
)
from utils.log_sanitize import sanitize_log_value

logger = logging.getLogger("adella_chatbot.handlers.safety")


def handle_unsafe_request(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle unsafe_request intent (wildcard - all states).

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    phone_number = context['phone_number']
    state_manager = context.get('state_manager')

    logger.info(f"Unsafe request from {phone_number} — blocking client and replying with policy")

    if state_manager:
        try:
            state_manager.block_client(phone_number, reason="unsafe_request")
        except Exception as e:
            logger.warning("block_client failed for unsafe_request: %s", e)

    return {
        "messages": [UNSAFE_REQUEST_RESPONSE],
        "new_state": None,
        "actions": ["block_client"]
    }


def handle_rude_abusive(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle explicit abusive/threatening intent (wildcard - all states).

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    phone_number = context['phone_number']
    state_manager = context.get('state_manager')
    if state_manager:
        try:
            state_manager.block_client(phone_number, reason="abusive_language")
        except Exception as e:
            logger.warning("block_client failed in rude_abusive: %s", e)
    logger.warning("Blocked %s for abusive/threatening content", phone_number)
    return {
        "messages": [BLOCKED_UNABLE_TO_ASSIST],
        "new_state": None,
        "actions": ["block_client"]
    }


def track_profanity_signal(phone_number: str, message: str, state_manager: Any) -> None:
    """
    Silently track profanity-list word usage for deposit policy (no user interruption).

    This function updates:
    - profanity_count (cumulative)
    - profanity_detected (True once cumulative count reaches threshold)
    """
    if not state_manager or not message:
        return

    from core.settings_manager import get_setting
    profanity_enabled = (get_setting('profanity_deposit_enabled') or 'true').lower() in ('true', '1', 'yes')
    if not profanity_enabled:
        return

    from booking.deposit_handler import count_profanity_words
    message_profane_count = count_profanity_words(message)
    if message_profane_count <= 0:
        return

    state = state_manager.get_state(phone_number) or {}
    current_total = int(state.get('profanity_count', 0) or 0)
    total_profane = current_total + message_profane_count

    updates = {'profanity_count': total_profane}
    if total_profane >= 3:
        updates['profanity_detected'] = True

    state_manager.update_fields(phone_number, updates)
    logger.info(
        "Tracked profanity signal for %s: +%d (total=%d, detected=%s)",
        sanitize_log_value(phone_number),
        message_profane_count,
        total_profane,
        bool(updates.get('profanity_detected')),
    )
