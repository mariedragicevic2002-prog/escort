"""
DEPOSIT_REQUIRED state handler - Deposit payment and validation flow.
"""

import logging
import os
import re
from ipaddress import ip_address
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from core.booking_substates import DOUBLES_SUPPLY_ESCORT
from utils.log_sanitize import LOG_SUPPRESSED_FMT
from utils.experiments import deposit_followup_variant
from utils.structured_logging import log_quality_metric

from templates.deposit_flow_messages import (
    BOOKING_CANCELLED_NO_PROBLEM,
    DEPOSIT_SCREENSHOT_PROMPT,
    IMAGE_DOWNLOAD_FAILED,
)

logger = logging.getLogger("adella_chatbot.handlers.deposit_flow")

_REFUSE_PATTERN = re.compile(
    r"\b(no|refuse|nevermind|never\s+mind|don't\s+want|not\s+interested|nah|nope)\b",
    re.IGNORECASE,
)


def _media_url_is_safe(url: str) -> bool:
    """Basic SSRF guard for deposit media downloads."""
    try:
        parsed = urlparse((url or "").strip())
    except Exception:
        return False

    if parsed.scheme != "https" or not parsed.hostname:
        return False

    host = parsed.hostname.strip().lower()

    # Strict allow-list: fail closed when unset. A test-safe default keeps local
    # unit tests deterministic while production must set this explicitly.
    allowed_env = (os.getenv("DEPOSIT_MEDIA_ALLOWED_HOSTS") or "example.test").strip()
    allowed_hosts = {h.strip().lower() for h in allowed_env.split(",") if h.strip()}

    def _host_allowed(candidate: str) -> bool:
        return any(candidate == allowed or candidate.endswith(f".{allowed}") for allowed in allowed_hosts)

    if not _host_allowed(host):
        return False

    # Block direct IP literals to private/local ranges.
    try:
        ip_lit = ip_address(host)
        if (
            ip_lit.is_private
            or ip_lit.is_loopback
            or ip_lit.is_link_local
            or ip_lit.is_multicast
            or ip_lit.is_reserved
            or ip_lit.is_unspecified
        ):
            return False
    except ValueError:
        # Hostname (not an IP literal): already restricted by allow-list above.
        pass

    return True


def _download_media_bytes_safe(media_url: str, *, log_prefix: str) -> bytes | None:
    """Download media content with SSRF guard and sanitized error logging."""
    import requests

    safe_url = str(media_url or "").strip()
    if not _media_url_is_safe(safe_url):
        logger.warning("%s: blocked unsafe media URL (SSRF guard)", log_prefix)
        return None

    max_bytes_raw = (os.getenv("DEPOSIT_MEDIA_MAX_BYTES") or "").strip()
    try:
        max_bytes = int(max_bytes_raw) if max_bytes_raw else 5 * 1024 * 1024
    except ValueError:
        logger.warning("%s: invalid DEPOSIT_MEDIA_MAX_BYTES=%r, using default 5MB", log_prefix, max_bytes_raw)
        max_bytes = 5 * 1024 * 1024
    if max_bytes <= 0:
        logger.warning("%s: invalid media size cap=%d", log_prefix, max_bytes)
        return None

    try:
        response = requests.get(safe_url, timeout=30, stream=True)
        response.raise_for_status()
        if hasattr(response, "iter_content"):
            total = 0
            chunks: list[bytes] = []
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    logger.warning("%s: media download exceeded %d bytes", log_prefix, max_bytes)
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
        content = response.content or b""
        if len(content) > max_bytes:
            logger.warning("%s: media download exceeded %d bytes", log_prefix, max_bytes)
            return None
        return content
    except Exception as e:
        # Requests exceptions can include signed URLs in their repr.
        logger.error("%s: failed media download: %s", log_prefix, type(e).__name__)
        return None


def _requires_doubles_source_alert(state: dict[str, Any], booking_fields: dict[str, Any], is_doubles: bool) -> bool:
    """True when this confirmed deposit is for doubles and escort must source the second person (MFF style)."""
    if not is_doubles:
        return False
    from booking.mmf_exploration import escort_organises_male_for_mmf

    merged = {**state, **booking_fields}
    if escort_organises_male_for_mmf(merged):
        return False
    source = (state.get('escort_supply_source') or '').strip().lower()
    status = (state.get('booking_status') or '').strip().lower()
    if source == 'escort' or status == DOUBLES_SUPPLY_ESCORT:
        return True
    return False


def _requires_mmf_male_source_escort_alert(state: dict[str, Any], booking_fields: dict[str, Any]) -> bool:
    from booking.mmf_exploration import decode_mmf_exploration_tags, escort_organises_male_for_mmf

    merged = {**state, **booking_fields}
    if not escort_organises_male_for_mmf(merged):
        return False
    return bool(decode_mmf_exploration_tags(merged.get("mmf_exploration_tags")))


def _delete_pending_calendar_events(state: dict[str, Any]) -> None:
    """Delete pending booking/travel calendar events stored on state."""
    from services.calendar_service import delete_calendar_event

    for event_id in (
        state.get("peacock_event_id"),
        state.get("travel_outbound_event_id"),
        state.get("travel_return_event_id"),
    ):
        if event_id:
            delete_calendar_event(event_id)


def handle_deposit_screenshot(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle deposit screenshot upload and validation.

    Flow:
    1. Check if media URL provided
    2. Validate screenshot using Vision API
    3. Check deposit amount, PayID, and date
    4. If valid:
       - Create confirmed calendar event (BASIL color)
       - Transition to CONFIRMED
    5. If invalid:
       - Increment failed attempts
       - If attempts < 3: Request re-upload
       - If attempts >= 3: Block client and notify escort

    Args:
        context: Context dict with phone_number, media_urls, state, etc.

    Returns:
        Dict with messages, new_state, actions
    """
    phone_number = context['phone_number']
    media_urls = context.get('media_urls', [])
    state = context['state']
    state_manager = context['state_manager']

    # Check if screenshot provided
    if not media_urls:
        return {
            "messages": [DEPOSIT_SCREENSHOT_PROMPT],
            "new_state": None,
            "actions": []
        }

    # Get deposit amount from state
    deposit_amount = state.get('deposit_amount', 50)
    expected_reference = (state.get('deposit_payment_reference') or '').strip() or None

    # Validate screenshot
    from services.vision_service import validate_deposit_screenshot_from_bytes

    media_url = str(media_urls[0] or "").strip()
    image_content = _download_media_bytes_safe(media_url, log_prefix="deposit_flow")
    if image_content is None:
        return {
            "messages": [IMAGE_DOWNLOAD_FAILED],
            "new_state": None,
            "actions": []
        }

    result = validate_deposit_screenshot_from_bytes(
        image_content,
        phone_number,
        required_amount=deposit_amount,
        expected_reference=expected_reference,
    )

    is_valid = result['valid']

    # Vision unavailable / disabled -> route to manual review without penalising
    # the client (failed_attempts is for bad screenshots, not infra outages).
    if result.get('manual_review_required'):
        logger.warning(
            "Deposit screenshot routed to manual review for %s (reason=%s)",
            phone_number, result.get('error') or 'unknown',
        )
        try:
            from services.notification_service import notify_escort_manual_review
            booking_fields_for_alert = state_manager.get_booking_fields(phone_number) or {}
            notify_escort_manual_review(
                client_phone=phone_number,
                reason=f"deposit_screenshot_manual_review: {result.get('error') or 'unknown'}",
                booking_fields=booking_fields_for_alert,
            )
        except Exception as _alert_err:
            logger.error(
                "Failed to send manual-review alert for deposit screenshot: %s",
                type(_alert_err).__name__,
            )
        return {
            "messages": [
                "Thanks for the screenshot \u2014 I'm verifying it manually and will "
                "confirm your booking shortly."
            ],
            "new_state": None,
            "actions": [],
        }

    validation_errors = []

    if not result['details'].get('payid_found'):
        validation_errors.append('payid')
    if not result['details'].get('amount_found'):
        validation_errors.append('amount')
    if not result['details'].get('date_found'):
        validation_errors.append('date')
    if expected_reference and not result['details'].get('reference_found'):
        validation_errors.append('reference')

    # Re-fetch fresh state to avoid stale read under concurrent retries
    _fresh_state = state_manager.get_state(phone_number) or state
    failed_attempts = _fresh_state.get('deposit_screenshot_attempts', 0)

    if is_valid:
        # IMPORTANT: Use the actual detected amount from Vision, not the expected amount
        actual_deposit_amount = result.get('deposit_amount', deposit_amount)

        MAX_DEPOSIT_SANITY = 2000  # If Vision reads more than this, something went wrong
        if actual_deposit_amount and actual_deposit_amount > MAX_DEPOSIT_SANITY:
            logger.warning(
                f"Vision detected unusually large deposit ${actual_deposit_amount} for {phone_number}. "
                f"Falling back to expected amount ${deposit_amount}."
            )
            actual_deposit_amount = deposit_amount

        # Valid deposit received
        logger.info(f"Valid deposit received from {phone_number}: ${actual_deposit_amount} (Vision detected)")

        # Get booking fields
        booking_fields = state_manager.get_booking_fields(phone_number)

        # Re-validate mandatory fields before creating a CONFIRMED calendar event.
        _dur = booking_fields.get('duration')
        _missing = []
        if not booking_fields.get('date'): _missing.append('date')
        if not booking_fields.get('time'): _missing.append('time')
        if not (isinstance(_dur, int) and _dur > 0): _missing.append('duration')
        if not booking_fields.get('experience_type'): _missing.append('experience_type')
        if not booking_fields.get('incall_outcall'): _missing.append('incall_outcall')
        if _missing:
            logger.error(
                "Deposit valid but mandatory booking fields missing for %s: %s. Refusing to create CONFIRMED event.",
                phone_number, _missing,
            )
            log_quality_metric("deposit_confirmed_missing_fields", phone_number=phone_number, missing=",".join(_missing))
            return {
                "messages": [
                    "Thanks! Deposit received, but some booking details are missing. Please reply with the missing info so I can lock in your booking."
                ],
                "new_state": None,
                "actions": [],
            }

        is_outcall = booking_fields.get('incall_outcall') == 'outcall'
        is_available_now = state.get('available_now_requested', False)
        arrival_time_str = None  # For available-now outcall confirmation: "I'll aim to be there by X"

        # Option A: available-now outcall – compute start at payment time, conflict check, then create BASIL or return "slot taken"
        if is_available_now and is_outcall:
            from handlers.booking_collection import calculate_available_now_booking_datetime
            from services.calendar_service import check_outcall_conflict_with_travel
            from templates.errors import get_error_message
            from utils.timezone import get_current_datetime

            now = get_current_datetime()
            start_dt = calculate_available_now_booking_datetime(
                now, None, is_outcall=True, outcall_address=booking_fields.get('outcall_address')
            )
            booking_details = {
                **booking_fields,
                'date': start_dt.date(),
                'time': (start_dt.hour, start_dt.minute),
            }
            conflict_type, _ = check_outcall_conflict_with_travel(booking_details)
            if conflict_type != "none":
                # Clean up the stale GRAPHITE/pending event so it does not permanently
                # block this calendar slot.
                _stale_gid = state.get('graphite_event_id') or state.get('peacock_event_id')
                if _stale_gid:
                    from services.calendar_service import delete_calendar_event
                    delete_calendar_event(_stale_gid)
                msg = get_error_message('available_now_slot_taken', next_mins=15)
                return {
                    "messages": [msg],
                    "new_state": None,
                    "actions": [],
                }
            booking_fields = booking_details
            from templates.greetings import format_time_simple
            arrival_time_str = format_time_simple(start_dt.hour, start_dt.minute)

        pending_calendar_id = state.get('graphite_event_id') or state.get('peacock_event_id')
        travel_outbound_id = state.get('travel_outbound_event_id')
        travel_return_id = state.get('travel_return_event_id')

        pay_ref = (
            (expected_reference or state.get('deposit_payment_reference') or '')
        ).strip() or None

        try:
            from templates.confirmations import calculate_price
            total_cost = calculate_price(
                int(booking_fields.get('duration') or 60),
                experience_type=booking_fields.get('experience_type'),
                incall_outcall=booking_fields.get('incall_outcall', 'incall'),
                booking_fields=booking_fields,
            )
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            total_cost = 0

        # Idempotency guard — claim before calendar mutation so duplicate webhook
        # deliveries cannot execute confirm/create paths twice.
        _dep_token = (
            f"deposit:{phone_number}:"
            f"{booking_fields.get('date', '')}:"
            f"{booking_fields.get('time', '')}:"
            f"{booking_fields.get('duration', '')}"
        )
        _dep_claim_status = (
            state_manager.claim_confirmation_token_status(phone_number, _dep_token)
            if hasattr(state_manager, "claim_confirmation_token_status")
            else ("claimed" if state_manager.claim_confirmation_token(phone_number, _dep_token) else "duplicate")
        )
        if _dep_claim_status == "error":
            logger.error(
                "Deposit confirmation token claim failed for %s; refusing to confirm blindly",
                phone_number,
            )
            return {
                "messages": [
                    "Thanks — I couldn't safely finalise that deposit confirmation just now. "
                    "Please reply again in a few seconds."
                ],
                "new_state": "DEPOSIT_REQUIRED",
                "actions": [],
            }
        if _dep_claim_status != "claimed":
            logger.warning(
                "Duplicate deposit confirmation suppressed for %s", phone_number
            )
            from templates.confirmations import get_deposit_verified_booking_confirmation
            _dup_msg = get_deposit_verified_booking_confirmation(
                {**booking_fields, 'phone_number': phone_number}, total_cost
            )
            return {"messages": [_dup_msg], "new_state": "CONFIRMED", "actions": []}

        event_id = None

        # Incall (every experience type including couples/doubles): flip GRAPHITE/PEACOCK → BASIL in place.
        _inplace_incall = bool(pending_calendar_id) and not is_outcall and not is_available_now
        if _inplace_incall:
            try:
                from services.calendar_service import confirm_calendar_event

                _cn = booking_fields.get('client_name', 'Client')
                _exp = booking_fields.get('experience_type')
                _exp_s = str(_exp).strip() if _exp else None
                if confirm_calendar_event(
                    pending_calendar_id,
                    actual_deposit_amount,
                    _cn,
                    is_outcall=False,
                    experience_type=_exp_s,
                    payment_reference=pay_ref,
                    total_booking_cost=total_cost,
                ):
                    event_id = pending_calendar_id
                    logger.info(
                        "Deposit verified: calendar confirmed in-place id=%s phone=%s",
                        pending_calendar_id,
                        phone_number,
                    )
            except Exception as _ic_err:
                logger.warning(
                    "In-place calendar confirm failed; will recreate BASIL event: %s",
                    _ic_err,
                    exc_info=False,
                )

        # Scheduled outcall: GRAPHITE→BASIL on main booking; LAVENDER→GRAPE on travel legs (keep IDs).
        # Travel is confirmed first so a partial failure never leaves BASIL + pending lavender travel.
        # Available-now outcalls skip this and recreate so slot timing can move to "now".
        _inplace_outcall = (
            bool(pending_calendar_id)
            and is_outcall
            and not is_available_now
        )
        if not event_id and _inplace_outcall:
            try:
                from services.calendar_service import (
                    confirm_calendar_event,
                    confirm_travel_time_blocks,
                )

                _cn = booking_fields.get('client_name', 'Client')
                _exp = booking_fields.get('experience_type')
                _exp_s = str(_exp).strip() if _exp else None

                travel_ok = True
                if travel_outbound_id or travel_return_id:
                    travel_ok = bool(
                        confirm_travel_time_blocks(
                            travel_outbound_id,
                            travel_return_id,
                        )
                    )
                if travel_ok and confirm_calendar_event(
                    pending_calendar_id,
                    actual_deposit_amount,
                    _cn,
                    is_outcall=True,
                    experience_type=_exp_s,
                    payment_reference=pay_ref,
                    total_booking_cost=total_cost,
                ):
                    event_id = pending_calendar_id
                    logger.info(
                        "Deposit verified: outcall main + travel confirmed in-place booking_id=%s phone=%s",
                        pending_calendar_id,
                        phone_number,
                    )
                elif not travel_ok:
                    logger.warning(
                        "Outcall deposit: could not flip travel LAVENDER→GRAPE; will recreate "
                        "booking + travel phone=%s",
                        phone_number,
                    )
            except Exception as _oc_err:
                logger.warning(
                    "In-place outcall confirm failed; will recreate: %s",
                    _oc_err,
                    exc_info=False,
                )

        if not event_id:
            # Delete GRAPHITE/PEACOCK event (and any pending travel blocks) before creating BASIL
            if pending_calendar_id or travel_outbound_id or travel_return_id:
                from services.calendar_service import delete_calendar_event, delete_travel_time_blocks

                if pending_calendar_id and delete_calendar_event(pending_calendar_id):
                    logger.info(
                        "Deleted pending deposit calendar event %s before creating BASIL event",
                        pending_calendar_id,
                    )

                if travel_outbound_id or travel_return_id:
                    deleted_out, deleted_return = delete_travel_time_blocks(
                        travel_outbound_id, travel_return_id
                    )
                    if deleted_out or deleted_return:
                        logger.info(
                            "Deleted pending travel blocks (outbound=%s, return=%s) before creating BASIL event",
                            travel_outbound_id,
                            travel_return_id,
                        )

            from services.calendar_service import create_calendar_event

            cal_create = create_calendar_event(
                booking_fields,
                phone_number,
                is_confirmed=True,
                awaiting_deposit=False,
                client_name=booking_fields.get('client_name', 'Client'),
                return_travel_ids=is_outcall,
                deposit_amount=actual_deposit_amount,
                is_outcall=is_outcall,
                payment_reference=pay_ref,
                total_booking_cost=total_cost,
            )

            if isinstance(cal_create, dict):
                event_id = cal_create.get('event_id')
                travel_outbound_id = cal_create.get('travel_outbound_id')
                travel_return_id = cal_create.get('travel_return_id')
            else:
                event_id = cal_create
                travel_outbound_id = None
                travel_return_id = None

        # Fail-CLOSED on calendar create: never tell the client they're confirmed
        # if Google Calendar didn't actually accept the event. The deposit is
        # already verified, so we keep the deposit-paid state and route to manual
        # review rather than transitioning to CONFIRMED with a phantom booking.
        if not event_id:
            try:
                state_manager.release_confirmation_token(phone_number, _dep_token)
            except Exception as _tok_rel_err:
                logger.warning(
                    "Failed to release deposit confirmation token for %s: %s",
                    phone_number,
                    _tok_rel_err,
                )
            logger.error(
                "Calendar event create returned no event_id after deposit verification "
                "for %s; refusing to transition to CONFIRMED",
                phone_number,
            )
            try:
                from services.notification_service import notify_escort_manual_review
                notify_escort_manual_review(
                    client_phone=phone_number,
                    reason="calendar_create_failed_after_deposit",
                    booking_fields=booking_fields,
                )
            except Exception as _alert_err:
                logger.warning(
                    "Failed to send manual-review alert: %s", type(_alert_err).__name__
                )
            return {
                "messages": [
                    "Got your deposit \u2014 thanks! I'm having a hiccup locking the "
                    "calendar slot just now and have flagged this for review. "
                    "You'll hear back shortly to confirm."
                ],
                "new_state": None,
                "actions": [],
            }

        # Update state with ACTUAL deposit amount detected by Vision
        updates = {
            'deposit_paid': True,
            'deposit_amount': actual_deposit_amount,  # Store the actual amount paid
            'confirmed_event_id': event_id,
            'confirmed_at': datetime.now(timezone.utc),
            'total_booking_cost': total_cost,
            'feedback_request_sent': False,
            # Pending deposit colours are Graphite/Peacock; Basil uses confirmed_event_id only.
            'graphite_event_id': None,
            'peacock_event_id': None,
        }
        if pay_ref:
            updates['deposit_payment_reference'] = pay_ref
        
        # Store travel event IDs for outcalls
        if is_outcall:
            if travel_outbound_id:
                updates['travel_outbound_event_id'] = travel_outbound_id
            if travel_return_id:
                updates['travel_return_event_id'] = travel_return_id
            
            # Schedule 1-hour prior outcall notification
            try:
                from services.outcall_notification_service import schedule_outcall_travel_notification
                schedule_outcall_travel_notification(booking_fields, phone_number, state_manager)
            except Exception as e:
                logger.warning("Failed to schedule outcall notification: %s", type(e).__name__)

        state_manager.update_fields(phone_number, updates)

        # Durable booking history record (append-only, idempotent)
        try:
            state_manager.append_booking_history(
                phone_number, booking_fields,
                confirmed_at=updates['confirmed_at'],
                deposit_paid=True,
                total_cost=total_cost,
            )
        except Exception as _bh_err:
            logger.warning("append_booking_history (deposit path) failed: %s", _bh_err)

        # Format confirmation message(outcall: custom message; incall: booking summary + deposit confirmed)
        from templates.confirmations import (
            get_deposit_verified_booking_confirmation,
            get_deposit_verified_message_incall,
            get_deposit_verified_message_outcall,
            get_deposit_verified_message_outcall_available_now,
        )

        # Use the ACTUAL deposit amount detected by Vision
        is_doubles = state.get('booking_type') in ('doubles_mff',) or any(
            word in (booking_fields.get('experience_type') or '').lower()
            for word in ['double', 'threesome', 'couple', 'doubles', 'doubles_mff', 'mff']
        )
        is_available_now_confirmation = state.get('available_now_requested', False)

        client_name = booking_fields.get('client_name') or ''
        
        # Build list of messages to send
        confirmation_messages = []
        
        if is_outcall or is_doubles:
            if is_outcall and is_available_now_confirmation:
                confirmation_messages.append(get_deposit_verified_message_outcall_available_now(
                    client_name, actual_deposit_amount, booking_fields, total_cost, arrival_time_str
                ))
            else:
                confirmation_messages.append(get_deposit_verified_message_outcall(
                    client_name, actual_deposit_amount, booking_fields, total_cost, arrival_time_str=arrival_time_str
                ))
        else:
            confirmation_messages.append(get_deposit_verified_message_incall(
                client_name, actual_deposit_amount, booking_fields, total_cost
            ))
        
        # Add comprehensive booking confirmation screen
        is_mandatory = state.get('mandatory_deposit', False) or is_outcall
        booking_confirmation_screen = get_deposit_verified_booking_confirmation(
            client_name, booking_fields, total_cost, actual_deposit_amount, is_mandatory_deposit=is_mandatory
        )
        confirmation_messages.append(booking_confirmation_screen)

        # Notify escort:
        # - Available-now outcall: immediately after deposit confirmed
        # - Scheduled outcall: 1-hour prior only (handled by scheduled outcall notification job)
        is_available_now = state.get('available_now_requested', False)

        if is_outcall and is_available_now:
            from services.outcall_notification_service import send_outcall_booking_notification
            send_outcall_booking_notification(booking_fields, phone_number)
            logger.info(f"Sent immediate notification to escort for AVAILABLE-NOW outcall booking (BASIL status): {phone_number}")

        # If escort needs to SOURCE another escort (MFF etc.), send alert after deposit paid.
        if _requires_mmf_male_source_escort_alert(state, booking_fields):
            if not state.get("mmf_male_sourcing_escort_notified"):
                try:
                    from booking.mmf_exploration import decode_mmf_exploration_tags, humanize_mmf_exploration_tags
                    from services.notification_service import notify_escort_mmf_male_source_required

                    tags = decode_mmf_exploration_tags(state.get("mmf_exploration_tags"))
                    notify_escort_mmf_male_source_required(
                        client_phone=phone_number,
                        client_name=(booking_fields.get('client_name') or 'Client'),
                        experience_type=(booking_fields.get('experience_type') or ''),
                        booking_date=str(booking_fields.get('date') or ''),
                        booking_time=str(booking_fields.get('time') or ''),
                        duration_minutes=int(booking_fields.get('duration') or 0) or None,
                        exploration_summary=humanize_mmf_exploration_tags(tags),
                    )
                    state_manager.update_fields(
                        phone_number, {"mmf_male_sourcing_escort_notified": True}
                    )
                    logger.info("Sent MMF male-source notification to escort for: %s", phone_number)
                except Exception as e:
                    logger.warning(
                        "Failed to send MMF male-source alert to escort: %s", type(e).__name__
                    )
        elif _requires_doubles_source_alert(state, booking_fields, is_doubles):
            try:
                from services.notification_service import notify_escort_doubles_source_required
                notify_escort_doubles_source_required(
                    client_phone=phone_number,
                    client_name=(booking_fields.get('client_name') or 'Client'),
                    experience_type=(booking_fields.get('experience_type') or ''),
                    booking_date=str(booking_fields.get('date') or ''),
                    booking_time=str(booking_fields.get('time') or ''),
                )
                logger.info("Sent source-escort alert to escort for: %s", phone_number)
            except Exception as e:
                logger.warning("Failed to send source-escort alert to escort: %s", type(e).__name__)

        # Schedule booking reminders
        try:
            from services.reminder_service import schedule_booking_reminders
            schedule_booking_reminders(booking_fields, phone_number, state_manager)
        except Exception as e:
            logger.warning("Failed to schedule reminders: %s", type(e).__name__)
        
        # Schedule room detail reminder if incall
        if booking_fields.get('incall_outcall') == 'incall':
            try:
                from services.room_detail_service import schedule_room_detail_reminder
                schedule_room_detail_reminder(booking_fields, phone_number, state_manager)
            except Exception as e:
                logger.warning("Failed to schedule room detail reminder: %s", type(e).__name__)

        return {
            "messages": confirmation_messages,
            "new_state": "CONFIRMED",
            "actions": ["create_confirmed_event"]
        }

    else:
        # Invalid deposit
        failed_attempts += 1
        state_manager.update_fields(phone_number, {
            'deposit_screenshot_attempts': failed_attempts
        })

        logger.warning(f"Invalid deposit from {phone_number}: {validation_errors}, attempt {failed_attempts}/3")

        if failed_attempts >= 3:
            # Max attempts reached - block and notify
            state_manager.block_client(
                phone_number,
                reason="deposit_validation_failed",
                notes=f"Failed deposit validation 3 times. Errors: {', '.join(validation_errors)}"
            )

            # Fail-safe cleanup: release pending booking + calendar holds so this
            # conversation does not remain stuck in DEPOSIT_REQUIRED.
            _delete_pending_calendar_events(context.get("state") or {})
            state_manager.clear_booking(phone_number)

            # Send notification to escort
            try:
                from services.notification_service import notify_escort_deposit_validation_failed
                notify_escort_deposit_validation_failed(phone_number, validation_errors)
            except Exception as e:
                logger.error("Failed to send deposit validation failure notification: %s", type(e).__name__)

            return {
                "messages": [
                    "I'm unable to verify your deposit. Please contact me directly to complete your booking."
                ],
                "new_state": "NEW",
                "actions": ["block_client", "notify_escort"]
            }

        else:
            # Request re-upload with better error message
            from templates.errors import get_deposit_validation_error
            error_message = get_deposit_validation_error(result['details'], deposit_amount)
            if expected_reference and not result['details'].get('reference_found'):
                error_message += f"\n\nPlease include payment reference {expected_reference} in your transfer description."
            error_message += f"\n\nPlease try again ({failed_attempts}/3 attempts used)."

            return {
                "messages": [error_message],
                "new_state": None,  # Stay in DEPOSIT_REQUIRED
                "actions": []
            }


def handle_deposit_query(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle deposit-related questions.

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    state = context['state']
    phone_number = context['phone_number']
    state_manager = context['state_manager']

    # If client mentions cash on arrival, clarify the deposit is mandatory
    _msg_lower = (context.get('message') or '').lower()
    _cash_keywords = [
        'cash', 'pay cash', 'cash on arrival', 'pay on arrival', 'cash when',
        'pay when', 'prefer cash', 'rather pay', 'pay in cash', 'paying cash',
    ]
    if any(kw in _msg_lower for kw in _cash_keywords):
        return {
            "messages": [
                "A deposit is required to confirm your booking — "
                "cash on arrival isn't accepted as a deposit. "
                "Please transfer via PayID to secure your spot."
            ],
            "new_state": None,
            "actions": [],
        }

    deposit_amount = state.get('deposit_amount', 50)
    _reason_raw = state.get('deposit_reason')
    if isinstance(_reason_raw, str):
        reason = _reason_raw.strip() or "booking"
    elif _reason_raw is None:
        reason = "booking"
    else:
        reason = str(_reason_raw).strip() or "booking"
    reason_lower = reason.lower()

    # Use the same message format as deposit request
    from templates.confirmations import get_deposit_request_message
    booking_fields = state_manager.get_booking_fields(phone_number) or {}
    client_name = (booking_fields.get("client_name") or "").strip() or None
    _oc_addr = (booking_fields.get('outcall_address') or '').strip() or None
    message = get_deposit_request_message(
        deposit_amount,
        reason,
        phone_number=phone_number,
        client_name=client_name,
        outcall_address=_oc_addr if "outcall" in reason_lower else None,
        booking_fields=booking_fields,
        payment_reference=(state.get('deposit_payment_reference') or '').strip() or None,
    )
    if deposit_followup_variant(phone_number) == "reassuring":
        message += "\n\nIf you'd like, I can walk you through it step-by-step."

    return {
        "messages": [message],
        "new_state": None,  # Stay in DEPOSIT_REQUIRED
        "actions": []
    }


def handle_refuse_deposit(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle deposit refusal with a soft recovery prompt.

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    phone_number = context['phone_number']

    logger.info(f"Client {phone_number} refused deposit")
    log_quality_metric("deposit_refusal_soft_recovery", phone_number=phone_number)

    return {
        "messages": [
            "I understand — deposits can feel unexpected. "
            "For this booking type, the deposit is required before confirmation for safety and reservation security.\n\n"
            "If you'd like, I can help with an option that may not require a deposit, or we can continue this booking if you change your mind."
        ],
        "new_state": None,  # Stay in DEPOSIT_REQUIRED
        "actions": []
    }


def handle_cancel_booking(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle booking cancellation during deposit stage.

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    phone_number = context['phone_number']
    state_manager = context['state_manager']
    state = context['state']

    # Delete pending event and associated travel blocks if they exist
    _delete_pending_calendar_events(state)

    # Clear booking
    state_manager.clear_booking(phone_number)

    return {
        "messages": [BOOKING_CANCELLED_NO_PROBLEM],
        "new_state": "NEW",
        "actions": ["delete_pending_event"]
    }


def handle_provide_field(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle regular text messages during deposit flow.
    Treat as deposit query.

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    return handle_deposit_query(context)


def handle_goodbye(context: dict[str, Any]) -> dict[str, Any]:
    """Handle farewell in DEPOSIT_REQUIRED state — remind client deposit is still needed."""
    state = context['state']
    deposit_amount = state.get('deposit_amount', 50)
    return {
        "messages": [
            f"Take care! Just a reminder — a ${deposit_amount} deposit is still needed to confirm your booking. "
            "Send through the PayID receipt whenever you're ready \U0001f60a"
        ],
        "new_state": None,
        "actions": [],
    }
