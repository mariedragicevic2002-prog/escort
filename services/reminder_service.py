"""

Reminder Service - Automated booking reminders.
Sends reminders 24h and 2h before bookings.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger("adella_chatbot.reminder_service")


def schedule_booking_reminders(booking_fields: dict[str, Any], phone_number: str, state_manager) -> bool:
    """
    Schedule booking reminders for a confirmed booking.
    
    Args:
        booking_fields: Dict with booking details (date, time, etc.)
        phone_number: Client's phone number
        state_manager: State manager instance
        
    Returns:
        True if reminders scheduled successfully
    """
    try:
        date = booking_fields.get('date')
        time = booking_fields.get('time')
        
        if not date or not time:
            logger.warning(f"Cannot schedule reminders - missing date/time for {phone_number}")
            return False
        
        # Parse datetime
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
        
        # Create booking datetime (escort local = admin Location timezone)
        from utils.timezone import get_local_timezone

        tz = get_local_timezone()
        booking_datetime = tz.localize(datetime.combine(booking_date, datetime.min.time().replace(hour=hour, minute=minute)))
        
        # Calculate reminder times
        reminder_24h = booking_datetime - timedelta(hours=24)
        reminder_2h = booking_datetime - timedelta(hours=2)
        
        # Store reminder times in state
        state_manager.update_fields(phone_number, {
            'reminder_24h_scheduled': reminder_24h.isoformat(),
            'reminder_2h_scheduled': reminder_2h.isoformat(),
            'reminder_24h_sent': False,
            'reminder_2h_sent': False
        })
        
        logger.info(f"Scheduled reminders for {phone_number}: 24h={reminder_24h}, 2h={reminder_2h}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to schedule reminders for {phone_number}: {e}")
        return False


def send_booking_reminder(booking_fields: dict[str, Any], phone_number: str, reminder_type: str = "24h") -> bool:
    """
    Send a booking reminder to client.
    
    Args:
        booking_fields: Dict with booking details
        phone_number: Client's phone number
        reminder_type: "24h" or "2h"
        
    Returns:
        True if reminder sent successfully
    """
    from services.sms_service import send_sms
    from templates.confirmations import format_booking_summary
    
    try:
        # Format booking summary
        summary = format_booking_summary(booking_fields)
        
        if reminder_type == "24h":
            message = f"""\U0001F4C5 Reminder: Your booking is tomorrow!\n\n{summary}\n\nLooking forward to seeing you!"""
        else:  # 2h
            message = f"""\u23F0 Reminder: Your booking is in 2 hours!\n\n{summary}\n\nSee you soon!"""
        
        success = send_sms(phone_number, message)
        
        if success:
            logger.info(f"Sent {reminder_type} reminder to {phone_number}")
        
        return success
        
    except Exception as e:
        logger.error(f"Failed to send reminder to {phone_number}: {e}")
        return False


def schedule_confirmation_30min_followup(_booking_fields: dict[str, Any], phone_number: str, state_manager) -> bool:
    """
    Schedule 30-min post-confirmation follow-up for incall bookings.
    Sends "Are you still wanting to go ahead with your booking?" 30 mins after confirm.
    """
    try:
        from utils.timezone import get_local_timezone

        tz = get_local_timezone()
        now = datetime.now(tz)
        followup_time = now + timedelta(minutes=30)
        state_manager.update_fields(phone_number, {
            'confirmation_30min_scheduled': followup_time.isoformat(),
            'confirmation_30min_sent': False
        })
        logger.info(f"Scheduled 30-min confirmation follow-up for {phone_number}: {followup_time}")
        return True
    except Exception as e:
        logger.error(f"Failed to schedule 30-min confirmation follow-up for {phone_number}: {e}")
        return False


def send_confirmation_30min_followup(booking_fields: dict[str, Any], phone_number: str) -> bool:
    """Send 30-min post-confirmation message: are you still wanting to go ahead?"""
    from services.sms_service import send_sms
    from utils.date_formatting import format_date_australian, format_time_australian
    try:
        client_name = (booking_fields.get('client_name') or '').strip()
        date = booking_fields.get('date')
        time = booking_fields.get('time')
        date_str = format_date_australian(date) if date else ""
        if time is not None:
            if hasattr(time, 'hour') and hasattr(time, 'minute'):
                time = (time.hour, time.minute)
            time_str = format_time_australian(time)
        else:
            time_str = ""
        slot_str = f"{date_str} at {time_str}" if (date_str and time_str) else (date_str or time_str or "your booking")
        name = f" {client_name}" if client_name else ""
        name_prompt = "" if client_name else " as well as your name"
        message = f"Hey{name}, just checking in – are you still wanting to go ahead with your booking for {slot_str}? Reply YES{name_prompt} to confirm."
        return send_sms(phone_number, message)
    except Exception as e:
        logger.error(f"Error sending 30-min confirmation follow-up to {phone_number}: {e}")
        return False


def check_and_send_reminders(state_manager, db_service) -> int:
    """
    Check for due reminders and send them.
    Called periodically by background job.
    
    Args:
        state_manager: State manager instance
        db_service: Database service instance
        
    Returns:
        Number of reminders sent
    """
    try:
        from utils.timezone import get_local_timezone

        tz = get_local_timezone()
        now = datetime.now(tz)
        
        # Find bookings with unsent reminders
        query = """
            SELECT phone_number, date, time, duration, experience_type, 
                   incall_outcall, outcall_address, client_name,
                   reminder_24h_scheduled, reminder_2h_scheduled,
                   reminder_24h_sent, reminder_2h_sent
            FROM conversation_states
            WHERE current_state = 'CONFIRMED'
              AND date IS NOT NULL
              AND time IS NOT NULL
              AND (
                  (reminder_24h_scheduled IS NOT NULL AND reminder_24h_sent = FALSE)
                  OR (reminder_2h_scheduled IS NOT NULL AND reminder_2h_sent = FALSE)
              )
        """
        
        results = db_service.execute_query(query, fetch=True)
        if not results:
            return 0
        
        reminders_sent = 0
        
        for row in results:
            phone_number = row['phone_number']
            booking_fields = {
                'date': row['date'],
                'time': row['time'],
                'duration': row['duration'],
                'experience_type': row['experience_type'],
                'incall_outcall': row['incall_outcall'],
                'outcall_address': row['outcall_address'],
                'client_name': row['client_name']
            }
            
            # Check 24h reminder
            if row['reminder_24h_scheduled'] and not row['reminder_24h_sent']:
                reminder_time = datetime.fromisoformat(row['reminder_24h_scheduled'].replace('Z', '+00:00'))
                if reminder_time <= now:
                    if send_booking_reminder(booking_fields, phone_number, "24h"):
                        state_manager.update_fields(phone_number, {'reminder_24h_sent': True})
                        reminders_sent += 1
            
            # Check 2h reminder
            if row['reminder_2h_scheduled'] and not row['reminder_2h_sent']:
                reminder_time = datetime.fromisoformat(row['reminder_2h_scheduled'].replace('Z', '+00:00'))
                if reminder_time <= now:
                    if send_booking_reminder(booking_fields, phone_number, "2h"):
                        state_manager.update_fields(phone_number, {'reminder_2h_sent': True})
                        reminders_sent += 1
        
        if reminders_sent > 0:
            logger.info(f"Sent {reminders_sent} booking reminders")
        
        return reminders_sent
        
    except Exception as e:
        logger.error(f"Error checking reminders: {e}")
        return 0


def check_and_send_confirmation_30min_followups(state_manager, db_service) -> int:
    """
    Check for due 30-min post-confirmation follow-ups (incall) and send "still wanting to go ahead?" message.
    """
    try:
        from utils.timezone import get_local_timezone

        tz = get_local_timezone()
        now = datetime.now(tz)
        query = """
            SELECT phone_number, date, time, duration, experience_type,
                   incall_outcall, client_name,
                   confirmation_30min_scheduled, confirmation_30min_sent
            FROM conversation_states
            WHERE current_state = 'CONFIRMED'
              AND (incall_outcall IS NULL OR incall_outcall = 'incall')
              AND confirmation_30min_scheduled IS NOT NULL
              AND confirmation_30min_sent = FALSE
        """
        results = db_service.execute_query(query, fetch=True)
        if not results:
            return 0
        sent = 0
        for row in results:
            scheduled_str = row.get('confirmation_30min_scheduled')
            if not scheduled_str:
                continue
            try:
                scheduled = datetime.fromisoformat(scheduled_str.replace('Z', '+00:00'))
                if scheduled.tzinfo is None:
                    scheduled = tz.localize(scheduled)
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e)
                continue
            if scheduled > now:
                continue
            phone_number = row['phone_number']
            booking_fields = {
                'date': row['date'], 'time': row['time'], 'duration': row['duration'],
                'experience_type': row['experience_type'], 'client_name': row['client_name']
            }
            if send_confirmation_30min_followup(booking_fields, phone_number):
                state_manager.update_fields(phone_number, {'confirmation_30min_sent': True})
                sent += 1
        if sent > 0:
            logger.info(f"Sent {sent} 30-min confirmation follow-up(s)")
        return sent
    except Exception as e:
        logger.error(f"Error checking 30-min confirmation follow-ups: {e}")
        return 0
