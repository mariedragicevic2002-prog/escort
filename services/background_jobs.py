"""
Background Job Scheduler - Handles periodic tasks.
Uses APScheduler for simple scheduling (can be upgraded to Celery for production).
"""

import logging
import os
import threading

logger = logging.getLogger("adella_chatbot.background_jobs")

# Try to import APScheduler
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    HAS_APSCHEDULER = True
except ImportError:
    HAS_APSCHEDULER = False
    BackgroundScheduler = None
    CronTrigger = None
    IntervalTrigger = None
    logger.warning("APScheduler not installed - background jobs will be disabled")

# Global scheduler instance
_scheduler = None
_scheduler_lock = threading.Lock()


def _resolve_timezone(timezone_name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(timezone_name)
    except Exception as e:
        logger.warning(
            "Invalid timezone %r for ugly-mugs sync schedule (%s). Falling back to Australia/Adelaide.",
            timezone_name,
            e,
        )
        from zoneinfo import ZoneInfo

        return ZoneInfo("Australia/Adelaide")


def _job_exists(scheduler, job_id: str) -> bool:
    if not scheduler or not job_id:
        return False
    try:
        get_job = getattr(scheduler, "get_job", None)
        if callable(get_job):
            return get_job(job_id) is not None
    except Exception:
        pass
    try:
        return any((job or {}).get("id") == job_id for job in (getattr(scheduler, "jobs", None) or []))
    except Exception:
        return False


def init_scheduler(state_manager, db_service):
    """
    Initialize background job scheduler.

    Args:
        state_manager: State manager instance
        db_service: Database service instance
    """
    global _scheduler

    if not HAS_APSCHEDULER:
        logger.warning("APScheduler not available - background jobs disabled")
        return

    assert BackgroundScheduler is not None
    assert IntervalTrigger is not None

    with _scheduler_lock:
        if _scheduler:
            logger.warning("Scheduler already initialized")
            return
        _scheduler = BackgroundScheduler(daemon=True)

    scheduler = _scheduler
    if scheduler is None:
        return
    
    # Schedule reminder checks every 5 minutes
    scheduler.add_job(
        func=check_reminders_job,
        trigger=IntervalTrigger(minutes=5),
        args=[state_manager, db_service],
        id='check_reminders',
        name='Check and send booking reminders',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    
    # Schedule cleanup jobs daily at 2 AM
    scheduler.add_job(
        func=cleanup_job,
        trigger=IntervalTrigger(hours=24),
        args=[db_service],
        id='cleanup',
        name='Daily cleanup tasks',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Daily PostgreSQL logical backup (pg_dump + gzip); requires pg_dump on PATH
    try:
        import config as app_config

        if getattr(app_config, "AUTO_BACKUP_ENABLED", True) and CronTrigger is not None:
            try:
                from zoneinfo import ZoneInfo

                tz = ZoneInfo("UTC")
            except Exception as e:
                logger.warning("ZoneInfo(UTC) failed for backup schedule: %s", e)
                tz = None
            h = getattr(app_config, "BACKUP_HOUR_UTC", 3)
            m = getattr(app_config, "BACKUP_MINUTE_UTC", 15)
            if tz is not None:
                backup_trigger = CronTrigger(hour=h, minute=m, timezone=tz)
            else:
                backup_trigger = CronTrigger(hour=h, minute=m)
            scheduler.add_job(
                func=automated_backup_job,
                trigger=backup_trigger,
                id="pg_dump_backup",
                name="Daily PostgreSQL backup",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info(
                "Scheduled daily backup at %02d:%02d UTC -> %s",
                h,
                m,
                os.path.abspath(getattr(app_config, "BACKUP_DIR", "backups")),
            )
    except Exception as e:
        logger.warning("Could not register automated backup job: %s", e)

    # Daily EscortsAndBabes Lookup -> safety watchlist sync
    try:
        from services.ugly_mugs_sync_service import get_ugly_mugs_sync_schedule

        schedule = get_ugly_mugs_sync_schedule()
        if CronTrigger is not None:
            sync_timezone = _resolve_timezone(schedule.timezone)
            sync_trigger = CronTrigger(
                hour=schedule.hour,
                minute=schedule.minute,
                timezone=sync_timezone,
            )
            scheduler.add_job(
                func=ugly_mugs_sync_job,
                trigger=sync_trigger,
                id="ugly_mugs_sync_daily",
                name="Daily ugly-mugs safety watchlist sync",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info(
                "Scheduled ugly-mugs sync daily at %02d:%02d %s (enabled=%s)",
                schedule.hour,
                schedule.minute,
                schedule.timezone,
                schedule.enabled,
            )
    except Exception as e:
        logger.warning("Could not register ugly-mugs sync job: %s", e)

    try:
        import config as app_config
        from services.ai_task_queue import process_pending_tasks
        from services.database_service import get_shared_db

        if not _job_exists(scheduler, "ai_task_processor"):
            scheduler.add_job(
                func=lambda: process_pending_tasks(get_shared_db(app_config.DATABASE_URL), max_tasks=10),
                trigger=IntervalTrigger(minutes=5),
                id="ai_task_processor",
                name="Process pending AI tasks",
                replace_existing=True,
                misfire_grace_time=30,
                max_instances=1,
                coalesce=True,
            )
    except Exception as e:
        logger.warning("Could not register AI task processor job: %s", e)

    scheduler.start()
    logger.info("Background job scheduler started")


def check_reminders_job(state_manager, db_service):
    """Job to check and send booking reminders and operational notifications."""
    try:
        from services.client_feedback_service import check_and_send_feedback_requests
        from services.ai_task_queue import process_pending_ai_tasks
        from services.outcall_notification_service import check_and_send_outcall_notifications
        from services.pending_deposit_service import check_and_cancel_expired_pending_deposits
        from services.push_notification_service import check_and_send_booking_push_notifications
        from services.reminder_service import check_and_send_confirmation_30min_followups, check_and_send_reminders
        from services.room_detail_service import check_and_send_room_detail_reminders
        from services.semantic_memory_service import SemanticMemoryService
        from services.stalled_recovery_service import check_and_send_stalled_nudges

        # Check booking reminders
        reminder_count = check_and_send_reminders(state_manager, db_service)
        if reminder_count > 0:
            logger.info(f"Reminder job: Sent {reminder_count} reminders")

        # Server-side push notifications for closed-app mobile delivery
        push_count = check_and_send_booking_push_notifications(db_service)
        if push_count > 0:
            logger.info("Push notification job: Sent %s push notification(s)", push_count)

        # Check 30-min post-confirmation follow-up (incall: "still wanting to go ahead?")
        confirmation_30min_count = check_and_send_confirmation_30min_followups(state_manager, db_service)
        if confirmation_30min_count > 0:
            logger.info(f"Confirmation 30min job: Sent {confirmation_30min_count} follow-up(s)")

        # Check outcall travel notifications
        outcall_count = check_and_send_outcall_notifications(state_manager, db_service)
        if outcall_count > 0:
            logger.info(f"Outcall notification job: Sent {outcall_count} notifications")
        
        # Check room detail reminders
        room_detail_count = check_and_send_room_detail_reminders(state_manager, db_service)
        if room_detail_count > 0:
            logger.info(f"Room detail job: Sent {room_detail_count} reminders")
        
        # Check expired pending deposits
        cancelled_count = check_and_cancel_expired_pending_deposits(state_manager, db_service)
        if cancelled_count > 0:
            logger.info(f"Pending deposit job: Cancelled {cancelled_count} expired deposits")

        # Post-booking feedback request: 5 mins after booking end, send escort feedback SMS
        feedback_request_count = check_and_send_feedback_requests(state_manager, db_service)
        if feedback_request_count > 0:
            logger.info(f"Client feedback job: Sent {feedback_request_count} feedback request(s)")

        # Stalled conversation gentle nudge
        stalled_nudges_count = check_and_send_stalled_nudges(state_manager, db_service)
        if stalled_nudges_count > 0:
            logger.info(f"Stalled nudge job: Sent {stalled_nudges_count} nudge(s)")

        # Process queued non-critical AI tasks (e.g., semantic memory capture)
        def _handle_semantic_memory_capture(payload, _db):
            svc = SemanticMemoryService(_db)
            svc.store_memory(
                phone_number=str(payload.get("phone_number") or ""),
                memory_type="message_observation",
                memory_text=str(payload.get("message") or ""),
                metadata={
                    "intent": payload.get("intent"),
                    "state": payload.get("state"),
                    "source": "ai_task_queue",
                },
            )

        queued_done = process_pending_ai_tasks(
            db_service,
            handlers={"semantic_memory_capture": _handle_semantic_memory_capture},
            batch_size=20,
        )
        if queued_done > 0:
            logger.info("AI queue job: Processed %s task(s)", queued_done)

        # Touring city 2-day-prior notifications
        try:
            from handlers.touring_inquiry import check_and_send_touring_notifications
            touring_count = check_and_send_touring_notifications(db_service)
            if touring_count > 0:
                logger.info(f"Touring notification job: Sent {touring_count} 2-day-prior notification(s)")
        except Exception as e:
            logger.error(f"Error in touring notification job: {e}")

        # Deposit follow-up: SMS escort when a booking is waiting on deposit > 4h
        try:
            from services.deposit_followup_service import check_and_send_deposit_followups
            deposit_followup_count = check_and_send_deposit_followups(state_manager, db_service)
            if deposit_followup_count > 0:
                logger.info("Deposit follow-up job: Sent %s reminder(s)", deposit_followup_count)
        except Exception as e:
            logger.error("Error in deposit follow-up job: %s", e)

        # Pre-booking check-in: SMS escort ~2h before confirmed booking starts
        try:
            from services.checkin_service import check_and_send_prebooking_checkins
            checkin_count = check_and_send_prebooking_checkins(state_manager, db_service)
            if checkin_count > 0:
                logger.info("Pre-booking check-in job: Sent %s check-in(s)", checkin_count)
        except Exception as e:
            logger.error("Error in pre-booking check-in job: %s", e)
            
    except Exception as e:
        logger.error(f"Error in reminder job: {e}")


def automated_backup_job():
    """Run pg_dump backup (same as scripts/backup_database.py)."""
    try:
        from services.backup_service import run_pg_dump_backup

        ok, msg = run_pg_dump_backup()
        if ok:
            logger.info("Automated backup: %s", msg)
        else:
            logger.error("Automated backup failed: %s", msg)
    except Exception as e:
        logger.error("Automated backup error: %s", e, exc_info=True)


def ugly_mugs_sync_job():
    """Run daily EscortsAndBabes Lookup sync into safety watchlist."""
    try:
        from services.ugly_mugs_sync_service import run_ugly_mugs_sync

        result = run_ugly_mugs_sync()
        status = str(result.get("status", "")).strip().lower()
        if status == "success":
            logger.info(
                "Ugly-mugs sync job completed: inserted=%s unique=%s failed_pages=%s",
                result.get("inserted", 0),
                result.get("unique_count", 0),
                result.get("failed_pages_count", 0),
            )
        elif status == "skipped":
            logger.info("Ugly-mugs sync job skipped: %s", result.get("reason", "unspecified"))
        else:
            logger.warning("Ugly-mugs sync returned unexpected status: %s", result)
    except Exception as e:
        logger.error("Ugly-mugs sync job failed: %s", e, exc_info=True)
        raise


def cleanup_job(db_service):
    """Daily cleanup job."""
    try:
        # Clean old rate limit data
        db_service.execute_query("SELECT clean_old_rate_limit_data()")
        
        # Clean old analytics (keep last 90 days)
        db_service.execute_query("SELECT clean_old_analytics()")

        # Clean old message history (keep last 90 days by default)
        retention_days_raw = (os.getenv("MESSAGE_HISTORY_RETENTION_DAYS") or "").strip()
        try:
            retention_days = int(retention_days_raw) if retention_days_raw else 90
        except ValueError:
            retention_days = 90
        retention_days = max(7, min(3650, retention_days))
        db_service.execute_query(
            "SELECT clean_old_message_history(%s)",
            (retention_days,),
            fetch=False,
        )

        # Clean expired HMAC link tokens (older than 24h)
        try:
            from core.hmac_security import cleanup_expired_tokens
            cleanup_expired_tokens(db_service, max_age_hours=24)
        except Exception as e:
            logger.warning("Link token cleanup skipped: %s", e)
        
        logger.info("Cleanup job completed")
    except Exception as e:
        logger.error(f"Error in cleanup job: {e}")


def shutdown_scheduler():
    """Shutdown the scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown()
        _scheduler = None
        logger.info("Background job scheduler stopped")


def get_scheduler():
    """Get scheduler instance."""
    return _scheduler
