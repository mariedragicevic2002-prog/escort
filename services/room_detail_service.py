"""
Room Detail Reminder Service
Sends intercom / lobby instructions to clients 1 hour before incall bookings.
Templates: non-Perth (intercom) vs Perth (lobby meet). Optional: forward client SMS to escort after send.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger("escort_chatbot.room_detail_service")


def _setting_true(key: str, default: str = "true") -> bool:
    try:
        from core.settings_manager import get_setting

        raw = (get_setting(key) or default).strip().lower()
        return raw in ("true", "1", "yes")
    except Exception as e:
        logger.warning("settings read %s: %s", key, e)
        return default.lower() in ("true", "1", "yes")


def incall_1h_reminder_enabled() -> bool:
    """Admin: send 1h incall SMS to clients (default on)."""
    return _setting_true("incall_1h_reminder_enabled", "true")


def incall_reminder_forward_replies_enabled() -> bool:
    """Admin: after 1h reminder, forward client messages to escort."""
    return _setting_true("incall_reminder_forward_replies", "false")


def _format_booking_time_12h(booking_fields: dict[str, Any]) -> str:
    bt = booking_fields.get("time")
    if isinstance(bt, tuple) and len(bt) >= 2:
        hour, minute = int(bt[0]), int(bt[1])
    else:
        hour_attr = getattr(bt, "hour", None)
        minute_attr = getattr(bt, "minute", None)
        if hour_attr is None or minute_attr is None:
            return str(bt or "")
        hour, minute = int(hour_attr), int(minute_attr)
    period = "pm" if hour >= 12 else "am"
    display_hour = hour % 12
    if display_hour == 0:
        display_hour = 12
    return f"{display_hour}:{minute:02d}{period}"


def _format_escort_location_display() -> str:
    from config import get_current_incall_location

    location = get_current_incall_location() or {}
    hotel = (location.get("hotel_name") or "").strip()
    address = (location.get("address") or "").strip()
    city = (location.get("city") or "").strip()
    if hotel and address:
        return f"{hotel}, {address}"
    if address:
        return address
    if hotel:
        return f"{hotel}, {city}" if city else hotel
    return city or "my location"


def _is_perth_incall() -> bool:
    from config import get_current_incall_location

    loc = get_current_incall_location() or {}
    city = (loc.get("city") or "").strip().lower()
    return city == "perth"


def schedule_room_detail_reminder(
    booking_fields: dict[str, Any],
    phone_number: str,
    state_manager
) -> bool:
    """
    Schedule room detail reminder for incall booking.
    
    Args:
        booking_fields: Dict with booking details
        phone_number: Client's phone number
        state_manager: State manager instance
        
    Returns:
        True if scheduled successfully
    """
    try:
        if booking_fields.get('incall_outcall') != 'incall':
            return False  # Only for incall bookings

        if not incall_1h_reminder_enabled():
            logger.info(
                "Skipping incall 1h reminder schedule (disabled in admin) for %s",
                phone_number,
            )
            return True

        date = booking_fields.get('date')
        time = booking_fields.get('time')
        
        if not date or not time:
            logger.warning(f"Cannot schedule room detail reminder - missing date/time for {phone_number}")
            return False
        
        # Parse datetime
        from utils.timezone import get_local_timezone

        tz = get_local_timezone()
        
        if isinstance(date, str):
            booking_date = datetime.strptime(date, "%Y-%m-%d").date()
        elif hasattr(date, 'date'):
            booking_date = date.date() if hasattr(date, 'date') else date
        else:
            booking_date = date
        
        if isinstance(time, tuple):
            hour, minute = time
        else:
            hour, minute = 0, 0
        
        # Create booking datetime
        booking_datetime = tz.localize(datetime.combine(booking_date, datetime.min.time().replace(hour=hour, minute=minute)))
        
        # Calculate reminder time (1 hour before booking)
        reminder_time = booking_datetime - timedelta(hours=1)
        
        # Store reminder time in state (reset forward flag until new reminder sends)
        state_manager.update_fields(phone_number, {
            'room_detail_reminder_scheduled': reminder_time.isoformat(),
            'room_detail_reminder_sent': False,
            'forward_incall_replies_to_escort': False,
        })
        
        logger.info(f"Scheduled room detail reminder for {phone_number}: {reminder_time}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to schedule room detail reminder for {phone_number}: {e}")
        return False


def send_room_detail_reminder(
    booking_fields: dict[str, Any],
    phone_number: str,
    state_manager=None,
) -> bool:
    """
    Send 1h incall reminder SMS to client (intercom template or Perth lobby template).

    If admin enabled "forward replies", sets forward_incall_replies_to_escort on success.
    """
    from config import get_escort_name
    from core.settings_manager import get_setting
    from services.sms_service import send_sms

    if not incall_1h_reminder_enabled():
        logger.info("Incall 1h reminder send skipped (disabled in admin) for %s", phone_number)
        return False

    try:
        client_name = (booking_fields.get("client_name") or "there").strip() or "there"
        escort_name = get_escort_name() or "Adella"
        time_str = _format_booking_time_12h(booking_fields)
        location_line = _format_escort_location_display()
        perth = _is_perth_incall()
        intercom = (get_setting("location_intercom", "") or "").strip()

        if perth:
            message = (
                f"Hi {client_name} just making sure you're still coming at {time_str} to {location_line}. "
                f"I'll meet you down at the lobby approx 5 mins prior to booking start time. "
                f"See you soon {escort_name} x"
            )
        else:
            if intercom:
                interbit = (
                    f"When you arrive ring {intercom} on the intercom and I will buzz you in. "
                )
            else:
                interbit = (
                    "When you arrive, use the intercom and I will buzz you in. "
                )
            message = (
                f"Hi {client_name} just making sure you're still coming at {time_str} to {location_line}. "
                f"{interbit}"
                f"See you soon {escort_name} x"
            )

        success = send_sms(phone_number, message)

        if success:
            logger.info("Sent incall 1h reminder to %s (perth=%s)", phone_number, perth)
            if state_manager and incall_reminder_forward_replies_enabled():
                state_manager.update_fields(
                    phone_number, {"forward_incall_replies_to_escort": True}
                )
                logger.info("Incall forward-to-escort enabled for %s", phone_number)

        return success

    except Exception as e:
        logger.error("Failed to send room detail reminder to %s: %s", phone_number, e)
        return False


def check_and_send_room_detail_reminders(state_manager, db_service) -> int:
    """
    Check for due room detail reminders and send them.
    Called periodically by background job.
    
    Args:
        state_manager: State manager instance
        db_service: Database service instance
        
    Returns:
        Number of reminders sent
    """
    try:
        if not incall_1h_reminder_enabled():
            return 0

        from utils.timezone import get_local_timezone

        tz = get_local_timezone()
        now = datetime.now(tz)
        
        # Find incall bookings with unsent reminders
        query = """
            SELECT phone_number, date, time, duration, experience_type, 
                   incall_outcall, client_name,
                   room_detail_reminder_scheduled,
                   room_detail_reminder_sent
            FROM conversation_states
            WHERE current_state = 'CONFIRMED'
              AND incall_outcall = 'incall'
              AND date IS NOT NULL
              AND time IS NOT NULL
              AND room_detail_reminder_scheduled IS NOT NULL
              AND room_detail_reminder_sent = FALSE
        """
        
        results = db_service.execute_query(query, fetch=True)
        if not results:
            return 0
        
        reminders_sent = 0
        
        for row in results:
            phone_number = row['phone_number']
            
            # Check if reminder is due
            reminder_time_str = row['room_detail_reminder_scheduled']
            if reminder_time_str:
                try:
                    reminder_time = datetime.fromisoformat(reminder_time_str.replace('Z', '+00:00'))
                    if reminder_time <= now:
                        # Send reminder
                        booking_fields = {
                            'date': row['date'],
                            'time': row['time'],
                            'duration': row['duration'],
                            'experience_type': row['experience_type'],
                            'incall_outcall': row['incall_outcall'],
                            'client_name': row['client_name']
                        }
                        
                        if send_room_detail_reminder(
                            booking_fields, phone_number, state_manager=state_manager
                        ):
                            state_manager.update_fields(phone_number, {
                                'room_detail_reminder_sent': True
                            })
                            reminders_sent += 1
                except Exception as e:
                    logger.error(f"Error processing room detail reminder for {phone_number}: {e}")
        
        if reminders_sent > 0:
            logger.info(f"Sent {reminders_sent} room detail reminders")
        
        return reminders_sent
        
    except Exception as e:
        logger.error(f"Error checking room detail reminders: {e}")
        return 0
