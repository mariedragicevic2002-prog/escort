"""
Error Templates
Standard error messages for various failure scenarios.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT

import logging
logger = logging.getLogger("adella_chatbot.errors")

ERROR_MESSAGES = {
    # Calendar errors
    'calendar_unavailable': "Sorry, I'm having trouble checking my calendar. Please text me directly or try again later.",
    'calendar_conflict': "That time is already booked. Could you suggest another time?",
    'calendar_conflict_with_alternatives': "That time is already booked. I'm available at {alternatives}. Which works for you?",

    # Validation errors
    'invalid_date': "That date doesn't look right. Please use format like: tomorrow, 12/02, or Friday",
    'invalid_time': "That time doesn't look right. Please use format like: 3pm, 15:00, or 8:30pm",
    'date_in_past': "That date/time has already passed. Please choose a future date and time.",
    'outside_hours': "I'm not available at that time. Please choose a time within my working hours.",
    'invalid_duration': "Duration must be at least 15 minutes (incall) or 1 hour (outcall). Common options: 1 hour, 2 hours, 3 hours.",
    'duration_too_long': "For bookings over 4 hours, please contact me directly.",

    # Booking errors
    'missing_fields': "I still need a few more details to complete your booking.",
    'missing_field_date': "What date works for you? (e.g., tomorrow, Friday, 12/02)",
    'missing_field_time': "What time? (e.g., 7pm, 8:30pm)",
    'missing_field_duration': "How long would you like to book for and what experience? (e.g. 1 hr PSE)\n\nSee the full menu: https://www.adella-allure.com.au/experience",
    'missing_field_experience': "Would you prefer GFE or PSE?\n\nSee the full menu: https://www.adella-allure.com.au/experience",
    'missing_field_location': "Incall or outcall?",
    'missing_field_address': "I need your hotel name or address for outcall bookings.",
    'booking_not_found': "I couldn't find an active booking for you. Would you like to make a new booking?",

    # Deposit errors
    'available_now_slot_taken': "That time has just been taken. Next available in about {next_mins} minutes – please try again then or I'll send you the next slot.",
    'deposit_required': "A deposit is required to secure this booking.",
    'deposit_invalid': "Your screenshot couldn't be validated. Please upload a clear screenshot showing the payment details.",
    'deposit_attempts_exceeded': "You've exceeded the maximum attempts. Please contact me directly to complete your booking.",

    # General errors
    'blocked': "I'm unable to assist you further. For enquiries, please contact via my website.",
    'rate_limited': "You're sending messages too quickly. Please wait a moment and try again.",
    # Fallback when AI is unavailable or fails
    'system_error': "I'm having trouble responding right now. Please wait a moment or text me directly if it continues.",
}


def get_error_message(error_type: str, **kwargs) -> str:
    """Get error message by type.

    Args:
        error_type: Type of error (key from ERROR_MESSAGES)
        **kwargs: Optional parameters for message formatting
            original_message: the client's original SMS text (used for AI context)

    Returns:
        Error message string
    """
    client_message = kwargs.pop("original_message", "") or ""

    # For conversational error types, try AI first for a warmer, more natural reply.
    # calendar_unavailable uses the static template directly \u2014 AI-generated variants
    # were vague ("booking system is temporarily down") and worse than the static copy.
    if error_type == 'booking_not_found':
        try:
            from handlers.ai_fallback import get_ai_booking_not_found_response
            ai_reply = get_ai_booking_not_found_response(client_message)
            if ai_reply:
                return ai_reply
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)

    # system_error: use static template only (no AI), so clients never see AI-generated
    # "need more information to process your booking" when a handler throws.
    message = ERROR_MESSAGES.get(error_type, ERROR_MESSAGES['system_error'])

    # Format with kwargs if provided
    try:
        return message.format(**kwargs)
    except (KeyError, IndexError):
        return message


def get_system_error_message(original_message: str = "") -> str:
    """
    High-level helper for unexpected system errors.

    Tries AI error clarification first, then falls back to the generic system_error template.
    """
    # Reuse get_error_message so AI handling stays in one place
    try:
        return get_error_message('system_error', original_message=original_message)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        return ERROR_MESSAGES['system_error']


def get_deposit_validation_error(details: dict, required_amount: int) -> str:
    """Get detailed deposit validation error message.

    Args:
        details: Dict with validation details (amount_found, payid_found, etc.)
        required_amount: Required deposit amount

    Returns:
        Detailed error message
    """
    from config import get_payid

    error_msg = "Your screenshot is invalid:\n\n"
    errors_found = []

    if not details.get('payid_found'):
        payid = get_payid()
        errors_found.append(f"\u274C PayID not visible ({payid})")

    if not details.get('account_name_found'):
        errors_found.append("\u274C Account name not visible")

    if not details.get('amount_found'):
        errors_found.append(f"\u274C Amount ${required_amount} not found")

    if not details.get('date_found'):
        errors_found.append("\u274C Today's date not visible")

    error_msg += "\n".join(errors_found)

    attempts_remaining = 3 - details.get('attempts', 0)
    error_msg += f"\n\nAttempts remaining: {attempts_remaining}\n\n"
    error_msg += "Please upload a clear screenshot showing:\n"
    error_msg += f"- PayID: {get_payid()}\n"
    error_msg += f"- Amount: ${required_amount}\n"
    error_msg += "- Today's date\n"
    error_msg += "- Account name"

    return error_msg


def get_enhanced_validation_error(errors: list, fields: dict) -> str:
    """Get enhanced, specific validation error messages with actionable feedback.

    Args:
        errors: List of error messages from validator
        fields: Dict with current booking fields

    Returns:
        Enhanced error message with specific guidance
    """
    if not errors:
        return "Sorry, there's an issue with your booking. Please check the details and try again."
    
    error_msg = "Sorry, there's an issue with your booking:\n\n"
    
    # Map common errors to specific, actionable messages
    enhanced_errors = []
    for error in errors:
        error_lower = error.lower()
        
        if 'date' in error_lower or 'past' in error_lower:
            if 'past' in error_lower:
                enhanced_errors.append("\u274C That date/time has already passed. Please choose a future date and time.\n   Examples: tomorrow, Friday, 12/02, next week")
            elif 'invalid' in error_lower:
                enhanced_errors.append("\u274C Date format not recognized. Please use:\n   - Tomorrow, Friday, next week\n   - 12/02, 28/12\n   - 28th Dec, 15th February")
        
        elif 'time' in error_lower:
            if 'outside' in error_lower or 'hours' in error_lower:
                # Get available hours dynamically
                try:
                    from config import get_available_hours
                    available_hours = get_available_hours()
                    enhanced_errors.append(f"\u274C That time is outside my hours ({available_hours}).\n   Please choose a time within my available hours.")
                except Exception as e:
                    logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
                    enhanced_errors.append("\u274C That time is outside my available hours.\n   Please choose a time within my available hours.")
            elif 'invalid' in error_lower:
                enhanced_errors.append("\u274C Time format not recognized. Please use:\n   - 7pm, 8:30pm, 9:15pm\n   - 19:00, 20:30, 21:15")
        
        elif 'duration' in error_lower:
            if 'too long' in error_lower or 'over' in error_lower:
                enhanced_errors.append("\u274C For bookings over 4 hours, please contact me directly.")
            elif 'too short' in error_lower or 'at least' in error_lower:
                try:
                    from core.rates_from_config import get_rates_for_duration_examples
                    examples = get_rates_for_duration_examples()
                    parts = [f"   - {label} (${price})" for label, price in examples]
                    enhanced_errors.append("\u274C Duration must be at least 15 minutes (incall) or 1 hour (outcall).\n   Common options:\n" + "\n".join(parts))
                except Exception as e:
                    logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
                    enhanced_errors.append("\u274C Duration must be at least 15 minutes (incall) or 1 hour (outcall).\n   Common options:\n   - 1 hour, 1.5 hours, 2 hours, 3 hours")
            elif 'invalid' in error_lower:
                enhanced_errors.append("\u274C Duration format not recognized. Please use:\n   - 1 hour, 2 hours, 1.5 hours\n   - 30 mins, 60 mins, 90 mins")
        
        elif 'address' in error_lower or 'outcall' in error_lower:
            if 'cbd' in error_lower or '15km' in error_lower:
                enhanced_errors.append("\u274C Outcall address must be within 15km of my current location.\n   Please provide a nearby hotel or apartment address.")
            elif 'address not found' in error_lower:
                enhanced_errors.append("\u274C I couldn't verify that address.\n   Please send a clearer hotel name or full street address.")
            elif 'verification failed' in error_lower or 'cannot verify location' in error_lower:
                enhanced_errors.append("\u274C I couldn't verify the location just now.\n   Please try again in a moment or send a clearer hotel name or street address.")
            elif 'invalid' in error_lower or 'missing' in error_lower:
                # Get current city dynamically
                try:
                    from config import get_current_incall_location
                    location = get_current_incall_location()
                    current_city = location.get('city', 'the city')
                except Exception as e:
                    logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
                    current_city = 'the city'
                enhanced_errors.append(f"\u274C For outcall bookings, I need your hotel name or address.\n   Examples: Hotel Grand {current_city}, 123 Main Street")
        
        elif 'experience' in error_lower:
            enhanced_errors.append(
                "\u274C Experience type must be one we offer (e.g. GFE, PSE, or DGFE).\n"
                "   - GFE: Girlfriend Experience\n"
                "   - PSE: Pornstar Experience\n"
                "   - DGFE: Dirty Girlfriend Experience\n"
                "   - Dinner Date and couples/doubles/group bookings are also accepted when applicable."
            )
        
        else:
            # Use original error if no enhancement found
            enhanced_errors.append(f"\u274C {error}")
    
    # If we couldn't build any enhanced messages, try AI clarification once
    if not enhanced_errors:
        try:
            from handlers.ai_fallback import get_ai_error_response
            ai_msg = get_ai_error_response(
                "There was a problem with the booking details you sent.",
                errors=errors,
                fields=fields,
            )
            if ai_msg:
                return ai_msg
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        return "Sorry, there's an issue with your booking. Please check the details and try again."
    
    error_msg += "\n".join(enhanced_errors)
    error_msg += "\n\nPlease correct these and try again!"
    
    return error_msg

