"""
Link resend handlers - Handle requests to resend upload or webform links.
"""

import logging
from typing import Any

logger = logging.getLogger("adella_chatbot.handlers.link_handlers")


def handle_resend_link(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle request to resend a link (upload link or webform link).
    
    Flow:
    - If message mentions screenshot/upload/deposit \u2192 send upload link
    - If in DEPOSIT_REQUIRED state \u2192 send upload link
    - Otherwise \u2192 send webform link
    
    Args:
        context: Context dict with phone_number, message, state
        
    Returns:
        Dict with messages, new_state, actions
    """
    phone_number = context['phone_number']
    message = context.get('message', '').lower()
    state = context.get('state', {})
    
    from templates.utility_templates import (
        get_upload_link_error_message,
        get_upload_link_success_message,
        get_webform_link_error_message,
        get_webform_link_success_message,
    )
    
    # Check if message mentions screenshot/upload/deposit
    upload_keywords = ['screenshot', 'upload', 'deposit', 'payment', 'payid']
    is_upload_request = any(keyword in message for keyword in upload_keywords)
    
    # Check if in DEPOSIT_REQUIRED state
    current_state = state.get('current_state', '')
    is_deposit_state = current_state == 'DEPOSIT_REQUIRED'
    
    # Check if deposit is required
    deposit_required = state.get('deposit_required', False)
    deposit_amount = state.get('deposit_amount', 100)
    
    if is_upload_request or is_deposit_state or deposit_required:
        # Send upload link
        try:
            upload_message = get_upload_link_success_message(phone_number, deposit_amount, force_new=True)
            return {
                "messages": [upload_message],
                "new_state": None,  # Stay in current state
                "actions": []
            }
        except Exception as e:
            logger.error(f"Error generating upload link for {phone_number}: {e}")
            return {
                "messages": [get_upload_link_error_message()],
                "new_state": None,
                "actions": []
            }
    else:
        # Send webform link
        try:
            webform_message = get_webform_link_success_message(phone_number)
            return {
                "messages": [webform_message],
                "new_state": None,  # Stay in current state
                "actions": []
            }
        except Exception as e:
            logger.error(f"Error generating webform link for {phone_number}: {e}")
            return {
                "messages": [get_webform_link_error_message()],
                "new_state": None,
                "actions": []
            }
