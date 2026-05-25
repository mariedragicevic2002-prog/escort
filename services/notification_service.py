"""
Notification Service
Handles sending notifications to the escort about bookings.
"""

import logging

from utils.log_sanitize import sanitize_log_value

logger = logging.getLogger("adella_chatbot.notification_service")


def send_booking_notification(phone_number: str, message: str) -> bool:
    """
    Send a booking notification SMS to the specified phone number.

    Args:
        phone_number: The phone number to send the notification to
        message: The notification message content

    Returns:
        True if notification sent successfully, False otherwise
    """
    from services.sms_service import send_sms

    try:
        success = send_sms(phone_number, message)

        if success:
            logger.info("Booking notification sent to %s", sanitize_log_value(phone_number))
        else:
            logger.error("Failed to send booking notification to %s", sanitize_log_value(phone_number))

        return success
    except Exception as e:
        logger.error(
            "Exception sending booking notification to %s: %s",
            sanitize_log_value(phone_number),
            e,
        )
        return False



def notify_escort_deposit_validation_failed(phone_number: str, validation_errors: list) -> bool:
    """
    Send notification to the escort when deposit validation fails 3 times.
    
    Args:
        phone_number: Client's phone number
        validation_errors: List of validation error messages
        
    Returns:
        True if notification sent successfully
    """
    from config import get_escort_phone_number
    from services.sms_service import send_escort_sms

    escort_phone = get_escort_phone_number()
    if not escort_phone:
        logger.warning("ESCORT_PHONE_NUMBER not configured - cannot send notification")
        return False

    errors_str = ", ".join(validation_errors[:3])  # Limit to first 3 errors

    message = f"""\u26A0\uFE0F DEPOSIT VALIDATION FAILED

Client: {phone_number}
Failed 3 deposit validation attempts.

Errors: {errors_str}

Client has been blocked. Please review manually."""

    success = send_escort_sms(escort_phone, message, category='deposit_validation_failed')

    if success:
        logger.info(f"Deposit validation failure notification sent to escort: {phone_number}")
    else:
        logger.error(f"Failed to send deposit validation failure notification: {phone_number}")
    
    return success


def notify_escort_mmf_male_source_required(
    *,
    client_phone: str,
    client_name: str = "",
    experience_type: str = "",
    booking_date: str = "",
    booking_time: str = "",
    duration_minutes: int | None = None,
    exploration_summary: str = "",
) -> bool:
    """Notify escort to source a male provider for MMF when deposit is paid."""
    from config import get_escort_phone_number
    from services.sms_service import send_escort_sms

    escort_phone = get_escort_phone_number()
    if not escort_phone:
        logger.warning("ESCORT_PHONE_NUMBER not configured - cannot send MMF male-source notification")
        return False

    name_value = client_name or "Client"
    experience_value = experience_type or "Doubles MMF"
    date_value = str(booking_date or "")
    time_value = str(booking_time or "")
    dur_part = ""
    if duration_minutes and duration_minutes > 0:
        if duration_minutes % 60 == 0 and duration_minutes >= 60:
            h = duration_minutes // 60
            dur_part = f"\nDuration: {h} hour{'s' if h != 1 else ''}"
        else:
            dur_part = f"\nDuration: {duration_minutes} min"
    explore = (exploration_summary or "").strip()
    explore_line = f"\nMMF Exploration: {explore}" if explore else ""

    message = (
        "ACTION REQUIRED — MMF booking: please source a male escort\n\n"
        f"Client: {name_value}\n"
        f"Phone: {client_phone}\n"
        f"Experience: {experience_value}\n"
        f"Date: {date_value}\n"
        f"Time: {time_value}"
        f"{dur_part}"
        f"{explore_line}\n\n"
        "Deposit paid — booking confirmed. Arrange the male provider to match their MMF exploration selections."
    )

    success = send_escort_sms(escort_phone, message, category='mmf_male_source_escort')
    if success:
        logger.info("MMF male-source notification sent for %s", sanitize_log_value(client_phone))
    else:
        logger.error("Failed to send MMF male-source notification for %s", sanitize_log_value(client_phone))
    return success


def notify_escort_doubles_source_required(
    *,
    client_phone: str,
    client_name: str = "",
    experience_type: str = "",
    booking_date: str = "",
    booking_time: str = "",
) -> bool:
    """Notify escort that a doubles booking requires sourcing the second person."""
    from config import get_escort_phone_number
    from services.sms_service import send_escort_sms

    escort_phone = get_escort_phone_number()
    if not escort_phone:
        logger.warning("ESCORT_PHONE_NUMBER not configured - cannot send doubles source notification")
        return False

    name_value = client_name or "Client"
    experience_value = experience_type or "Doubles"
    date_value = str(booking_date or "")
    time_value = str(booking_time or "")

    message = (
        "⚠️ ACTION REQUIRED - Doubles booking needs second escort sourced\n\n"
        f"Client: {name_value}\n"
        f"Phone: {client_phone}\n"
        f"Experience: {experience_value}\n"
        f"Date: {date_value}\n"
        f"Time: {time_value}\n\n"
        "Client requested that you organise the other person/escort for this doubles booking."
    )

    success = send_escort_sms(escort_phone, message, category='doubles_source_escort')
    if success:
        logger.info("Doubles source notification sent for %s", sanitize_log_value(client_phone))
    else:
        logger.error("Failed to send doubles source notification for %s", sanitize_log_value(client_phone))
    return success


def notify_escort_safety_screening_match(
    *,
    client_phone: str,
    action_taken: str = "warn_only",
) -> bool:
    """Notify escort when a client number matches uploaded safety-screening watchlist."""
    from config import get_escort_phone_number
    from services.sms_service import send_escort_sms

    escort_phone = get_escort_phone_number()
    if not escort_phone:
        logger.warning("ESCORT_PHONE_NUMBER not configured - cannot send safety screening notification")
        return False

    mode_line = (
        "Auto-block is ON: client was blocked immediately."
        if action_taken == "auto_block"
        else "Warn-only mode: please review before proceeding."
    )
    message = (
        "⚠️ SAFETY SCREENING MATCH\n\n"
        f"Client phone: {client_phone}\n"
        f"Action mode: {action_taken}\n\n"
        "This number is on your uploaded flagged watchlist.\n"
        "Please check the number against the Escorts & Babes Ugly Mugs list.\n\n"
        f"{mode_line}"
    )

    success = send_escort_sms(escort_phone, message, category='safety_screening')
    if success:
        logger.info("Safety screening alert sent for %s", sanitize_log_value(client_phone))
    else:
        logger.error("Failed to send safety screening alert for %s", sanitize_log_value(client_phone))
    return success


def notify_escort_manual_review(
    *,
    client_phone: str,
    reason: str,
    booking_fields: dict | None = None,
) -> bool:
    """Notify escort that a booking needs manual review (e.g. calendar create failed
    after deposit, integration outage, edge-case state) so it can be resolved by hand
    rather than left as a ghost confirmation."""
    from config import get_escort_phone_number
    from services.sms_service import send_escort_sms

    escort_phone = get_escort_phone_number()
    if not escort_phone:
        logger.warning("ESCORT_PHONE_NUMBER not configured - cannot send manual-review notification")
        return False

    bf = booking_fields or {}
    name_value = (bf.get('client_name') or 'Client')
    date_value = str(bf.get('date') or bf.get('booking_date') or '')
    time_value = str(bf.get('time') or bf.get('booking_time') or '')
    duration_value = str(bf.get('duration') or '')
    incall_outcall = str(bf.get('incall_outcall') or '')

    message = (
        "\u26A0\uFE0F MANUAL REVIEW REQUIRED\n\n"
        f"Client: {name_value}\n"
        f"Phone: {client_phone}\n"
        f"Reason: {reason}\n"
        f"Date: {date_value}\n"
        f"Time: {time_value}\n"
        f"Duration: {duration_value}\n"
        f"Mode: {incall_outcall}\n\n"
        "Booking is NOT confirmed in the calendar. Please resolve manually."
    )

    success = send_escort_sms(escort_phone, message, category='manual_review')
    if success:
        logger.info(
            "Manual-review alert sent (reason=%s) for %s",
            reason, sanitize_log_value(client_phone),
        )
    else:
        logger.error(
            "Failed to send manual-review alert (reason=%s) for %s",
            reason, sanitize_log_value(client_phone),
        )
    return success


