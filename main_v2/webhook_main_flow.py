"""
main_v2/webhook_main_flow.py

Core webhook request processing. Called from application.py webhook() route handler.
"""
import re
import time as _time_sleep
from datetime import datetime, timezone

import config
from flask import jsonify, request
from app.ingress.rollout_controls import (
    WebhookIngressRolloutDecision,
    emit_webhook_ingress_rollout_metrics as _emit_webhook_rollout_metrics,
    resolve_webhook_ingress_rollout_decision as _resolve_webhook_rollout_decision,
)
from app.ingress.webhook_controller import process_webhook_ingress_with_rollout
from app.ingress.webhook_pipeline import run_refactor_webhook_ingress_pipeline
from app.ingress.webhook_security import (
    WebhookIngressSecurityError,
    enforce_webhook_ingress_security,
)

from handlers.booking_coll._shared_dinner_doubles import doubles_supply_patterns_touch
from handlers.escort_feedback import handle_escort_feedback_reply as _handle_escort_feedback_reply
from main_v2 import runtime as _runtime
from main_v2.conversation_guards import (
    check_frustration as _check_frustration,
    check_repeat_response as _check_repeat_response,
)
from main_v2.helpers import (
    _active_escort_sourced_doubles_flow,
    _asks_about_other_doubles_partner_media_or_identity,
    _build_enquiry_keyword_reply,
    _build_goodbye_reply,
    _build_location_reply,
    _build_photo_reply,
    _build_repeat_guard_final_message,
    _build_repeat_guard_message,
    _build_screenshot_link_reply,
    _build_webform_reply,
    build_doubles_other_escort_media_reply_bundle,
    _is_enquiry_with_description,
    _is_enquiry_keyword,
    _is_goodbye,
    _is_location_request,
    _is_photo_followup_request,
    _is_photo_request,
    _is_screenshot_link_request,
    _is_webform_request,
    _should_use_repeat_guard,
)
from main_v2.log import logger
from main_v2.rollout import (
    get_chatbot_rollout_percent as _get_chatbot_rollout_percent,
    is_phone_in_rollout_bucket as _is_phone_in_rollout_bucket,
    log_rollout_path_alert as _log_rollout_path_alert,
)
from main_v2.webhook_helpers import (
    collect_media_urls,
    extract_webhook_contact_phone,
    normalize_webhook_payload,
    webhook_json_fastpath_reply,
)
from main_v2.webhook_monitor import record_webhook_monitor as _record_webhook_monitor
from services.notification_service import notify_escort_safety_screening_match
from services.safety_screening_service import (
    get_screening_mode,
    is_screening_enabled,
    log_match as log_safety_match,
    lookup_flagged_number,
    should_notify_escort,
)
from services.sms_service import get_last_sms_error, send_escort_sms, send_sms
from templates import field_prompts
from templates.errors import get_system_error_message
from utils.log_sanitize import LOG_SUPPRESSED_FMT, sanitize_log_value
from utils.performance_timing import PerformanceTimer
from utils.request_tracer import add_trace_event, start_trace
from utils.structured_logging import (
    get_logger,
    log_booking_event,
    log_quality_metric,
    log_state_transition,
    set_observability_context,
)

structured_logger = get_logger("escort_chatbot.main")

_KNOWN_ACTION_TAGS = {
    "ai_fallback_used",
    "block_client",
    "check_calendar",
    "create_confirmed_event",
    "create_peacock_event",
    "create_pending_event",
    "delete_calendar_events",
    "delete_pending_event",
    "fallback_template_low_confidence",
    "fallback_template_used",
    "forward_to_escort",
    "notify_escort",
    "retrieval_policy_used",
    "save_tour_subscription",
    "escalate_manual_review",
    "escalate_vip_context",
    "transition_post_booking",
}

_FRUSTRATION_PATTERNS = re.compile(
    r"\b(hello+\??|why|again|still|same|what the|ugh|wtf|ffs|forget it|never mind|nvm|useless|not working)\b",
    re.IGNORECASE,
)


def _detect_field_contradiction(new_extraction: dict, existing_state: dict) -> list[str]:
    """
    Detect if newly extracted fields contradict already-confirmed fields.
    Returns list of contradicting field names. Empty list = no contradiction.
    """
    contradictions = []
    field_map = {
        "date": "event_date",
        "time": "event_time",
        "duration": "duration_minutes",
        "experience_type": "experience_type",
        "incall_outcall": "booking_type",
    }
    for extracted_key, state_key in field_map.items():
        new_val = (new_extraction or {}).get(extracted_key)
        existing_val = (existing_state or {}).get(state_key)
        if new_val is not None and existing_val is not None:
            new_str = str(new_val).strip().lower()
            existing_str = str(existing_val).strip().lower()
            if new_str != existing_str and new_str and existing_str:
                contradictions.append(extracted_key)
    return contradictions


def _detect_frustration(message: str) -> bool:
    """Detect client frustration signals in a message."""
    if not message:
        return False
    text = message.strip()
    alpha = "".join(ch for ch in text if ch.isalpha())
    if alpha and alpha.isupper() and len(alpha) > 3:
        return True
    if _FRUSTRATION_PATTERNS.search(text):
        return True
    return False


def _check_repetition(state: dict) -> bool:
    """Return True if bot has sent 3+ identical consecutive responses."""
    repeat_count = int((state or {}).get("_consecutive_same_response_count", 0))
    return repeat_count >= 3


def _norm_phone(p: str) -> str:
    # Digit-only strip for escort-phone comparison only.
    # Not the AU normalizer — see utils/phone_normalization.py for that.
    return ''.join(c for c in (p or '') if c.isdigit())


def _strip_profile_url_from_messages(messages: list[str], profile_url: str) -> list[str]:
    """Remove profile URL lines from messages for non-first-contact replies."""
    if not profile_url:
        return messages
    purl = profile_url.strip()
    result = []
    for msg in messages:
        lines = msg.split('\n')
        filtered = [line for line in lines if line.strip() != purl]
        # Collapse consecutive blank lines into one
        collapsed: list[str] = []
        prev_blank = False
        for line in filtered:
            is_blank = not line.strip()
            if is_blank and prev_blank:
                continue
            collapsed.append(line)
            prev_blank = is_blank
        result.append('\n'.join(collapsed).strip())
    return result


_FUNNEL_STEP_BY_STATE = {
    "NEW": "qualification",
    "COLLECTING": "availability",
    "CHECKING_AVAILABILITY": "availability",
    "EXTENDED_ENQUIRY": "screening",
    "MANUAL_REVIEW_PENDING": "screening",
    "DEPOSIT_REQUIRED": "deposit",
    "CONFIRMED": "confirmation",
    "POST_BOOKING": "follow_up",
}

_DETERMINISTIC_CONFIDENCE_INTENTS = {
    "book_appointment",
    "provide_field",
    "quick_booking",
    "confirm_booking",
    "cancel_booking",
    "modify_booking",
    "ask_availability",
    "available_now",
    "pricing_inquiry",
    "deposit_query",
    "deposit_screenshot",
    "unsafe_request",
    "rude_abusive",
}


def _estimate_turn_confidence_score(
    *,
    intent: str,
    actions: list[str],
    handler_returned_empty: bool,
    message_failed_count: int,
    fallback_recovered: bool,
) -> float:
    score = 0.58
    if intent in _DETERMINISTIC_CONFIDENCE_INTENTS:
        score += 0.20
    if "retrieval_policy_used" in actions:
        score += 0.15
    if "ai_fallback_used" in actions:
        score -= 0.10
    if "fallback_template_low_confidence" in actions:
        score -= 0.20
    if handler_returned_empty:
        score -= 0.15
    if fallback_recovered:
        score += 0.05
    if message_failed_count > 0:
        score -= 0.20
    return max(0.0, min(1.0, score))


def _resolve_webhook_ingress_rollout_decision(phone_number: str) -> WebhookIngressRolloutDecision:
    return _resolve_webhook_rollout_decision(phone_number)


def _record_webhook_ingress_rollout_metrics(
    *,
    decision: WebhookIngressRolloutDecision,
    phone_number: str,
    request_id: str,
) -> None:
    try:
        _emit_webhook_rollout_metrics(
            decision=decision,
            phone_number=phone_number,
            request_id=request_id,
        )
    except Exception as exc:
        logger.warning("webhook ingress rollout metrics skipped (%s)", type(exc).__name__)


def _process_webhook_refactor(request_id: str):
    return run_refactor_webhook_ingress_pipeline(
        request_id=request_id,
        legacy_processor=_process_webhook_legacy,
        db_service=getattr(_runtime, "db_service", None),
    )


def _process_webhook(request_id: str):
    return process_webhook_ingress_with_rollout(
        request_id=request_id,
        request_obj=request,
        legacy_processor=_process_webhook_legacy,
        refactor_processor=_process_webhook_refactor,
        decision_resolver=_resolve_webhook_ingress_rollout_decision,
        metrics_recorder=_record_webhook_ingress_rollout_metrics,
    )




def _legacy_safe_add_request_trace_event(stage: str, **kwargs) -> None:
    try:
        add_trace_event(stage, **kwargs)
    except Exception as trace_err:
        logger.debug("request trace add skipped: %s", trace_err)



def _legacy_safe_finish_request_trace(trace, outcome: str = "ok") -> None:
    if trace is None:
        return
    try:
        trace.finish(outcome=outcome)
    except Exception as trace_err:
        logger.debug("request trace finish skipped: %s", trace_err)



def _legacy_canon_phone(p: str) -> str:
    p = (p or '').strip()
    if not p:
        return ''
    lead = '+' if p.startswith('+') else ''
    return lead + ''.join(c for c in p if c.isdigit())



def _legacy_build_context(request_id: str) -> dict:
    return {
        "request_id": request_id,
        "state_manager": _runtime.state_manager,
        "db_service": _runtime.db_service,
        "ai_service": _runtime.ai_service,
        "classifier": _runtime.classifier,
        "router": _runtime.router,
        "trace": None,
    }



def _legacy_run_handlers(ctx: dict, handlers) -> object:
    for handler in handlers:
        response = handler(ctx)
        if response is not None:
            return response
    return None



def _legacy_handle_uninitialized_system(ctx: dict):
    if ctx["state_manager"] and ctx["db_service"]:
        return None
    structured_logger.error("system_not_initialized", request_id=ctx["request_id"])
    try:
        to_phone = extract_webhook_contact_phone(request)
        if to_phone:
            sms_msg = get_system_error_message("")
            send_sms(to_phone, sms_msg)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
    return jsonify({"status": "error", "message": "System not initialized"}), 500



def _legacy_prepare_inbound_request(ctx: dict):
    request_id = ctx["request_id"]
    payload = normalize_webhook_payload(request)
    event_type = payload.get("event") or payload.get("event_type") or payload.get("type") or ""
    msg_data = payload.get("data") or payload
    if not isinstance(msg_data, dict):
        msg_data = payload if isinstance(payload, dict) else {}
    media_urls = collect_media_urls(msg_data)
    is_inbound = bool(
        event_type in ("message.phone.received", "message.received", "incoming")
        or (not event_type and msg_data.get("contact") and msg_data.get("content"))
    )
    if not is_inbound:
        logger.info("webhook event ignored: %r (payload keys: %s)", event_type, list(payload.keys()))
        _record_webhook_monitor(request_id=request_id, ignored_event=True)
        return jsonify({"status": "ok", "message": "event ignored"}), 200
    raw_phone = msg_data.get('contact', '')
    message_body = msg_data.get('content', '')
    phone_number = _legacy_canon_phone(raw_phone)
    digits = ''.join(c for c in phone_number if c.isdigit())
    ctx.update(
        {
            "payload": payload,
            "event_type": event_type,
            "msg_data": msg_data,
            "media_urls": media_urls,
            "raw_phone": raw_phone,
            "message_body": message_body,
            "phone_number": phone_number,
        }
    )
    if not phone_number or len(digits) < 8:
        logger.warning("inbound SMS rejected: invalid contact=%s", sanitize_log_value(raw_phone))
        log_quality_metric("webhook_invalid_phone", request_id=request_id)
        _record_webhook_monitor(request_id=request_id, ignored_event=True)
        return jsonify({"status": "ok", "message": "invalid contact"}), 200
    ctx["masked_phone"] = sanitize_log_value(phone_number)
    return None



def _legacy_enforce_security(ctx: dict):
    request_id = ctx["request_id"]
    try:
        ingress_security = enforce_webhook_ingress_security(
            headers=dict(request.headers.items()),
            raw_body=request.get_data(cache=True, as_text=False) or b"",
            payload=ctx["payload"],
            message_data=ctx["msg_data"],
            phone_number=ctx["phone_number"],
            message_body=ctx["message_body"],
            db_service=ctx["db_service"],
            webhook_secrets=config.get_httpsms_webhook_secrets(),
            webhook_secret_rotation=config.get_httpsms_webhook_secret_rotation_config(),
            signature_secret=config.get_httpsms_webhook_signature_secret(),
            signature_secret_rotation=config.get_httpsms_webhook_signature_rotation_config(),
            signature_required=config.httpsms_webhook_signature_required(),
            signature_tolerance_seconds=config.get_httpsms_webhook_signature_tolerance_seconds(),
        )
    except WebhookIngressSecurityError as sec_err:
        if sec_err.metric_name:
            log_quality_metric(sec_err.metric_name, request_id=request_id, **sec_err.observability_tags)
        return jsonify({"status": "error", "message": str(sec_err)}), sec_err.status_code
    ctx["ingress_security"] = ingress_security
    log_quality_metric(
        "webhook_secret_rotation_observed",
        request_id=request_id,
        auth_key_version=ingress_security.auth_key_version,
        auth_cutover_state=ingress_security.auth_cutover_state,
        signature_key_version=ingress_security.signature_key_version,
        signature_cutover_state=ingress_security.signature_cutover_state,
    )
    logger.info(
        "webhook raw payload (scrubbed): %s",
        sanitize_log_value(str(ingress_security.scrubbed_payload), max_len=500),
    )
    if ingress_security.dedup_key_missing:
        logger.warning(
            "dedup key missing from payload; using fallback key=%s",
            ingress_security.dedup_key,
        )
        log_quality_metric(
            "webhook_dedup_key_missing",
            request_id=request_id,
            auth_key_version=ingress_security.auth_key_version,
            auth_cutover_state=ingress_security.auth_cutover_state,
            signature_key_version=ingress_security.signature_key_version,
            signature_cutover_state=ingress_security.signature_cutover_state,
        )
    if ingress_security.duplicate:
        logger.info("duplicate suppressed (dedup_key=%s)", ingress_security.dedup_key[:120])
        log_quality_metric(
            "webhook_dedup_duplicate",
            request_id=request_id,
            auth_key_version=ingress_security.auth_key_version,
            auth_cutover_state=ingress_security.auth_cutover_state,
            signature_key_version=ingress_security.signature_key_version,
            signature_cutover_state=ingress_security.signature_cutover_state,
        )
        _record_webhook_monitor(request_id=request_id, ignored_event=True)
        return jsonify({"status": "ok", "message": "duplicate"}), 200
    return None



def _legacy_log_inbound_request(ctx: dict) -> None:
    set_observability_context(phone_number=ctx["phone_number"])
    logger.info(
        "[INBOUND] from=%s body=%s media=%d",
        sanitize_log_value(ctx["phone_number"]),
        sanitize_log_value(ctx["message_body"][:120]),
        len(ctx["media_urls"]),
    )
    structured_logger.info(
        "incoming_message",
        phone_number=ctx["phone_number"],
        message_preview=ctx["message_body"][:50],
        media_count=len(ctx["media_urls"]),
        request_id=ctx["request_id"],
    )



def _legacy_handle_escort_feedback(ctx: dict):
    escort_phone = config.get_escort_phone_number()
    ctx["escort_phone"] = escort_phone
    feedback_enabled = True
    try:
        from core.settings_manager import get_setting as _gs
        feedback_enabled = (_gs('client_feedback_enabled') or 'true').strip().lower() not in ('false', '0', 'no')
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
    if not feedback_enabled or not escort_phone or not ctx["phone_number"]:
        return None
    if _norm_phone(ctx["phone_number"]) != _norm_phone(escort_phone):
        return None
    try:
        success, reply_msg = _handle_escort_feedback_reply(
            ctx["message_body"], ctx["db_service"], ctx["state_manager"]
        )
        if reply_msg:
            send_escort_sms(escort_phone, reply_msg, category='feedback_replies')
        return jsonify({"status": "ok", "escort_feedback": True}), 200
    except Exception:
        logger.exception("Escort feedback handler failed")
        return jsonify({"status": "ok", "escort_feedback": True}), 200



def _legacy_handle_rate_limit(ctx: dict):
    try:
        from core.enhanced_rate_limiter import get_rate_limiter
        rate_limiter = get_rate_limiter()
        is_allowed, rate_limit_message = rate_limiter.check_rate_limit(ctx["phone_number"])
        if is_allowed:
            return None
        logger.warning(
            "Rate limit exceeded for %s: %s",
            sanitize_log_value(ctx["phone_number"]),
            rate_limit_message,
        )
        structured_logger.warning(
            "early_exit_rate_limited",
            phone_number=ctx["phone_number"],
            message=rate_limit_message,
            request_id=ctx["request_id"],
        )
        try:
            send_sms(ctx["phone_number"], "Please wait a moment before sending another message.")
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
        return jsonify({"status": "rate_limited", "message": rate_limit_message}), 429
    except ImportError:
        logger.error("Enhanced rate limiter not available — blocking request as fail-safe")
        return jsonify({"status": "error", "message": "Rate limiter unavailable"}), 429
    except Exception as rate_limit_error:
        logger.error("Rate limiting check failed (fail-safe block): %s", rate_limit_error)
        return jsonify({"status": "error", "message": "Rate limiter error"}), 429



def _legacy_handle_blocked_client(ctx: dict):
    if not ctx["state_manager"].is_blocked(ctx["phone_number"]):
        return None
    logger.warning("Blocked client attempted contact: %s", sanitize_log_value(ctx["phone_number"]))
    structured_logger.info(
        "early_exit_blocked_client",
        phone_number=ctx["phone_number"],
        request_id=ctx["request_id"],
    )
    return jsonify({"status": "blocked"}), 200



def _legacy_handle_safety_screening(ctx: dict):
    if not is_screening_enabled():
        return None
    screening_result = lookup_flagged_number(ctx["phone_number"])
    if not screening_result.get("matched"):
        return None
    screening_mode = get_screening_mode()
    normalized_phone = str(screening_result.get("normalized_phone") or "")
    notify_allowed = should_notify_escort(normalized_phone)
    escort_notified = False
    if notify_allowed:
        escort_notified = bool(
            notify_escort_safety_screening_match(
                client_phone=ctx["phone_number"],
                action_taken=screening_mode,
            )
        )
    if screening_mode == "auto_block":
        ctx["state_manager"].block_client(
            phone_number=ctx["phone_number"],
            reason="safety_screening_watchlist",
            notes="Matched uploaded safety screening watchlist",
        )
        structured_logger.warning(
            "early_exit_safety_screening_autoblock",
            phone_number=ctx["phone_number"],
            normalized_phone=normalized_phone,
            request_id=ctx["request_id"],
        )
        log_safety_match(
            phone_number=ctx["phone_number"],
            normalized_phone=normalized_phone,
            matched=True,
            action_taken="auto_block",
            escort_notified=escort_notified,
            note="Matched watchlist during inbound webhook; auto-block applied",
        )
        return jsonify({"status": "blocked", "safety_screening": True}), 200
    log_safety_match(
        phone_number=ctx["phone_number"],
        normalized_phone=normalized_phone,
        matched=True,
        action_taken="warn_only",
        escort_notified=escort_notified,
        note="Matched watchlist during inbound webhook; warn-only mode",
    )
    structured_logger.warning(
        "safety_screening_warn_only",
        phone_number=ctx["phone_number"],
        normalized_phone=normalized_phone,
        request_id=ctx["request_id"],
    )
    return None



def _legacy_handle_chatbot_disabled(ctx: dict):
    try:
        from core.settings_manager import get_setting
        chatbot_enabled = get_setting("chatbot_enabled")
        if chatbot_enabled != "0":
            return None
        logger.info(
            "Chatbot disabled – ignoring message from %s (no reply, no forward)",
            sanitize_log_value(ctx["phone_number"]),
        )
        structured_logger.info(
            "early_exit_chatbot_disabled",
            phone_number=ctx["phone_number"],
            request_id=ctx["request_id"],
        )
        return jsonify({"status": "ok", "chatbot_disabled": True}), 200
    except Exception as e:
        logger.warning("Chatbot enabled check failed: %s", e)
        return None



def _legacy_handle_blocked_phrase(ctx: dict):
    try:
        from core.settings_manager import get_setting
        block_enabled = (get_setting("blocked_words_block_enabled") or "true").lower() in ("true", "1", "yes")
        if not block_enabled:
            return None
        blocked_phrases_raw = (get_setting("blocked_phrases") or "").strip()
        if not blocked_phrases_raw or not ctx["message_body"]:
            return None
        phrases = [p.strip().lower() for p in blocked_phrases_raw.splitlines() if p.strip()]
        msg_lower = re.sub(r'\s+', ' ', ctx["message_body"].lower().strip())
        for phrase in phrases:
            phrase_norm = re.sub(r'\s+', ' ', phrase.strip().lower())
            if phrase_norm and phrase_norm in msg_lower:
                logger.info(
                    "Blocked phrase matched for %s: message not processed",
                    sanitize_log_value(ctx["phone_number"]),
                )
                structured_logger.info(
                    "early_exit_blocked_phrase",
                    phone_number=ctx["phone_number"],
                    phrase=phrase,
                    request_id=ctx["request_id"],
                )
                return jsonify({"status": "ok", "blocked_phrase": True}), 200
        return None
    except Exception as e:
        logger.warning("Blocked phrases check failed: %s", e)
        return None



def _legacy_build_incall_forward_message(ctx: dict, incall_state: dict) -> str:
    cname = (incall_state.get("client_name") or "Client").strip()
    body = (ctx["message_body"] or "").strip() or "[empty message]"
    media_note = ""
    if ctx["media_urls"]:
        media_note = "\n" + "\n".join(str(u) for u in ctx["media_urls"][:6])
    return f"Incall reply from {ctx['phone_number']} ({cname}):\n{body}{media_note}"[:1600]



def _legacy_log_incall_forward_receipt(ctx: dict) -> None:
    try:
        ctx["state_manager"].log_message(
            ctx["phone_number"], "inbound", ctx["message_body"], ctx["media_urls"]
        )
        ctx["state_manager"].touch(ctx["phone_number"])
    except Exception as log_err:
        logger.warning("Incall forward log: %s", log_err)



def _legacy_handle_incall_forward(ctx: dict):
    if not ctx["state_manager"]:
        return None
    if not ctx["message_body"]:
        return None
    if not ctx["phone_number"]:
        return None
    incall_state = ctx["state_manager"].get_state(ctx["phone_number"])
    if not incall_state or not incall_state.get("forward_incall_replies_to_escort"):
        return None
    try:
        from core.settings_manager import get_setting as _gs_incall
        if (_gs_incall("incall_reminder_forward_replies") or "false").strip().lower() not in (
            "true",
            "1",
            "yes",
        ):
            ctx["state_manager"].update_fields(ctx["phone_number"], {"forward_incall_replies_to_escort": False})
            return None
        from handlers.reschedule_response import get_escort_forwarding_phone
        to_esc = get_escort_forwarding_phone()
        if not to_esc:
            return None
        send_escort_sms(
            to_esc,
            _legacy_build_incall_forward_message(ctx, incall_state),
            category="incall_client_forwards",
        )
        _legacy_log_incall_forward_receipt(ctx)
        structured_logger.info(
            "early_exit_incall_forward",
            phone_number=ctx["phone_number"],
            request_id=ctx["request_id"],
        )
        return jsonify(
            {
                "status": "ok",
                "incall_forwarded": True,
                "request_id": ctx["request_id"],
            }
        ), 200
    except Exception as incall_err:
        logger.exception("Incall forward handling failed: %s", incall_err)
        return None



def _legacy_handle_rollout_hold(ctx: dict):
    rollout_percent = _get_chatbot_rollout_percent()
    if rollout_percent >= 100:
        return None
    rollout_state = ctx["state_manager"].get_state(ctx["phone_number"]) or {}
    rollout_state_name = str((rollout_state or {}).get("current_state") or "NEW").upper()
    is_existing_flow = rollout_state_name != "NEW" or bool(rollout_state.get("first_contact_sent"))
    in_bucket, rollout_bucket = _is_phone_in_rollout_bucket(ctx["phone_number"], rollout_percent)
    if is_existing_flow or in_bucket:
        return None
    rollout_msg = (
        "Thanks for your message. I'm running a staged chatbot rollout right now.\n"
        f"Please use my booking webform so I can help faster:\n{_build_webform_reply(ctx['phone_number'])}"
    )
    try:
        send_sms(ctx["phone_number"], rollout_msg)
        if ctx["state_manager"].get_state(ctx["phone_number"]):
            ctx["state_manager"].log_message(ctx["phone_number"], 'outbound', rollout_msg)
    except Exception as rollout_send_err:
        logger.warning(
            "Rollout hold send failed for %s: %s",
            sanitize_log_value(ctx["phone_number"]),
            rollout_send_err,
        )
    _log_rollout_path_alert(
        path="rollout_hold",
        phone_number=ctx["phone_number"],
        request_id=ctx["request_id"],
        state=rollout_state_name,
    )
    structured_logger.info(
        "early_exit_rollout_hold",
        phone_number=ctx["phone_number"],
        rollout_percent=rollout_percent,
        rollout_bucket=rollout_bucket,
        request_id=ctx["request_id"],
    )
    _record_webhook_monitor(request_id=ctx["request_id"], messages_sent=1)
    return jsonify(
        {
            "status": "ok",
            "rollout_hold": True,
            "rollout_percent": rollout_percent,
            "request_id": ctx["request_id"],
        }
    ), 200



def _legacy_handle_pre_state_guards(ctx: dict):
    return _legacy_run_handlers(
        ctx,
        [
            _legacy_handle_escort_feedback,
            _legacy_handle_rate_limit,
            _legacy_handle_blocked_client,
            _legacy_handle_safety_screening,
            _legacy_handle_chatbot_disabled,
            _legacy_handle_blocked_phrase,
            _legacy_handle_incall_forward,
            _legacy_handle_rollout_hold,
        ],
    )



def _legacy_handle_repeat_guard_followup(ctx: dict):
    rg_state = ctx["state_manager"].get_state(ctx["phone_number"]) or {}
    rg_status = ((rg_state.get("booking_status") or "").strip().lower())
    if rg_status == "repeat_guard_blocked":
        try:
            ctx["state_manager"].log_message(
                ctx["phone_number"], 'inbound', ctx["message_body"], ctx["media_urls"]
            )
            ctx["state_manager"].touch(ctx["phone_number"])
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
        return jsonify({"status": "success", "silenced": True, "request_id": ctx["request_id"]}), 200
    if rg_status != "repeat_guard_prompt_sent":
        return None
    if _is_enquiry_with_description(ctx["message_body"]):
        try:
            ctx["state_manager"].update_fields(ctx["phone_number"], {"booking_status": None})
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
        return None
    final_msg = _build_repeat_guard_final_message(ctx["phone_number"])
    fastpath_sent = 0
    fastpath_failed = 0
    try:
        ctx["state_manager"].log_message(
            ctx["phone_number"], 'inbound', ctx["message_body"], ctx["media_urls"]
        )
        ctx["state_manager"].touch(ctx["phone_number"])
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
    try:
        ctx["state_manager"].log_message(ctx["phone_number"], 'outbound', final_msg)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
    try:
        ctx["state_manager"].update_fields(ctx["phone_number"], {"booking_status": "repeat_guard_blocked"})
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
    try:
        if send_sms(ctx["phone_number"], final_msg):
            fastpath_sent = 1
        else:
            fastpath_failed = 1
    except Exception as send_err:
        logger.error(f"Repeat-guard final send failed: {send_err}")
        fastpath_failed = 1
    _record_webhook_monitor(
        request_id=ctx["request_id"],
        messages_sent=fastpath_sent,
        messages_failed=fastpath_failed,
    )
    return jsonify(
        {"status": "success", "messages_sent": fastpath_sent, "request_id": ctx["request_id"]}
    ), 200



def _legacy_handle_doubles_fastpath(ctx: dict):
    merged_doubles = {
        **(ctx["state_manager"].get_state(ctx["phone_number"]) or {}),
        **(ctx["state_manager"].get_booking_fields(ctx["phone_number"]) or {}),
    }
    cs_doubles = (merged_doubles.get("current_state") or "NEW").strip().upper()
    ctx["merged_doubles"] = merged_doubles
    ctx["cs_doubles"] = cs_doubles
    if not _asks_about_other_doubles_partner_media_or_identity(ctx["message_body"]):
        return None
    if not _active_escort_sourced_doubles_flow(merged_doubles, cs_doubles):
        return None
    if doubles_supply_patterns_touch(ctx["message_body"]):
        return None
    return webhook_json_fastpath_reply(
        ctx["state_manager"],
        ctx["phone_number"],
        ctx["message_body"],
        ctx["media_urls"],
        build_doubles_other_escort_media_reply_bundle(
            ctx["state_manager"], ctx["phone_number"], ctx["message_body"]
        ),
        ctx["request_id"],
        send_error_label="Doubles other-escort media fast-path send failed",
        fallback_on_send_fail="Sorry, that took too long. Please try again in a moment.",
    )



def _legacy_handle_photo_fastpath(ctx: dict):
    if not (
        _is_photo_request(ctx["message_body"])
        or _is_photo_followup_request(ctx["phone_number"], ctx["message_body"])
    ):
        return None
    merged_doubles = ctx.get("merged_doubles")
    cs_doubles = ctx.get("cs_doubles")
    if merged_doubles is None or cs_doubles is None:
        merged_doubles = {
            **(ctx["state_manager"].get_state(ctx["phone_number"]) or {}),
            **(ctx["state_manager"].get_booking_fields(ctx["phone_number"]) or {}),
        }
        cs_doubles = (merged_doubles.get("current_state") or "NEW").strip().upper()
    if doubles_supply_patterns_touch(ctx["message_body"]) and _active_escort_sourced_doubles_flow(
        merged_doubles, cs_doubles
    ):
        return None
    return webhook_json_fastpath_reply(
        ctx["state_manager"],
        ctx["phone_number"],
        ctx["message_body"],
        ctx["media_urls"],
        _build_photo_reply(),
        ctx["request_id"],
        send_error_label="Photo fast-path send failed",
        fallback_on_send_fail="Sorry, that took too long. Please try again in a moment.",
    )



def _legacy_handle_screenshot_fastpath(ctx: dict):
    if not _is_screenshot_link_request(ctx["message_body"]):
        return None
    return webhook_json_fastpath_reply(
        ctx["state_manager"],
        ctx["phone_number"],
        ctx["message_body"],
        ctx["media_urls"],
        _build_screenshot_link_reply(ctx["phone_number"], ctx["state_manager"]),
        ctx["request_id"],
        send_error_label="Screenshot link fast-path send failed",
    )



def _legacy_handle_webform_fastpath(ctx: dict):
    if not _is_webform_request(ctx["message_body"]):
        return None
    wf_state = ctx["state_manager"].get_state(ctx["phone_number"]) or {}
    wf_state_name = ((wf_state.get("current_state") or "NEW").strip().upper())
    wf_active_flow = wf_state_name != "NEW" or bool(wf_state.get("first_contact_sent"))
    if wf_active_flow:
        logger.info(
            "Skipping webform fast-path for active flow %s (%s)",
            ctx["phone_number"],
            wf_state_name,
        )
        return None
    return webhook_json_fastpath_reply(
        ctx["state_manager"],
        ctx["phone_number"],
        ctx["message_body"],
        ctx["media_urls"],
        _build_webform_reply(ctx["phone_number"]),
        ctx["request_id"],
        send_error_label="Webform fast-path send failed",
    )



def _legacy_handle_location_fastpath(ctx: dict):
    if not _is_location_request(ctx["message_body"]):
        return None
    fp_state = ctx["state_manager"].get_state(ctx["phone_number"]) or {}
    fp_bt = (fp_state.get("booking_type") or "").strip().lower()
    fp_cs = (fp_state.get("current_state") or "").strip().upper()
    if fp_bt == "dinner_date" and fp_cs == "COLLECTING":
        return None
    return webhook_json_fastpath_reply(
        ctx["state_manager"],
        ctx["phone_number"],
        ctx["message_body"],
        ctx["media_urls"],
        _build_location_reply(),
        ctx["request_id"],
        send_error_label="Location fast-path send failed",
    )



def _legacy_handle_enquiry_keyword_fastpath(ctx: dict):
    if not _is_enquiry_keyword(ctx["message_body"]):
        return None
    return webhook_json_fastpath_reply(
        ctx["state_manager"],
        ctx["phone_number"],
        ctx["message_body"],
        ctx["media_urls"],
        _build_enquiry_keyword_reply(),
        ctx["request_id"],
        send_error_label="Enquiry fast-path send failed",
    )



def _legacy_handle_enquiry_description_fastpath(ctx: dict):
    if not _is_enquiry_with_description(ctx["message_body"]):
        return None
    try:
        from handlers.reschedule_response import get_escort_forwarding_phone
        to_escort = get_escort_forwarding_phone()
        if to_escort:
            forward_body = (ctx["message_body"] or "")[:1600]
            fwd_msg = f"Enquiry from {ctx['phone_number']}:\n\n{forward_body}"
            send_escort_sms(to_escort, fwd_msg, category='enquiry_forwarding')
            logger.info("Enquiry forwarded to escort from %s", sanitize_log_value(ctx["phone_number"]))
        else:
            logger.warning("Enquiry forward skipped — escort phone not configured")
    except Exception as enquiry_err:
        logger.exception("Enquiry forward failed: %s", enquiry_err)
    try:
        from config import get_escort_name
        escort_name_local = get_escort_name() or "the escort"
    except Exception as escort_err:
        logger.warning(LOG_SUPPRESSED_FMT, escort_err, exc_info=False)
        escort_name_local = "the escort"
    ack = (
        f"Thanks, I've passed your message on to {escort_name_local} directly. "
        "She'll be in touch as soon as she can."
    )
    return webhook_json_fastpath_reply(
        ctx["state_manager"],
        ctx["phone_number"],
        ctx["message_body"],
        ctx["media_urls"],
        ack,
        ctx["request_id"],
        send_error_label="Enquiry ack send failed",
    )



def _legacy_handle_goodbye_fastpath(ctx: dict):
    if not _is_goodbye(ctx["message_body"]):
        return None
    return webhook_json_fastpath_reply(
        ctx["state_manager"],
        ctx["phone_number"],
        ctx["message_body"],
        ctx["media_urls"],
        _build_goodbye_reply(ctx["phone_number"]),
        ctx["request_id"],
        send_error_label="Goodbye fast-path send failed",
    )



def _legacy_handle_fastpaths(ctx: dict):
    return _legacy_run_handlers(
        ctx,
        [
            _legacy_handle_repeat_guard_followup,
            _legacy_handle_doubles_fastpath,
            _legacy_handle_photo_fastpath,
            _legacy_handle_screenshot_fastpath,
            _legacy_handle_webform_fastpath,
            _legacy_handle_location_fastpath,
            _legacy_handle_enquiry_keyword_fastpath,
            _legacy_handle_enquiry_description_fastpath,
            _legacy_handle_goodbye_fastpath,
        ],
    )



def _legacy_get_or_create_state(ctx: dict) -> None:
    current_state = ctx["state_manager"].get_state(ctx["phone_number"])
    if not current_state:
        ctx["state_manager"].create_state(ctx["phone_number"], "NEW")
        current_state = ctx["state_manager"].get_state(ctx["phone_number"])
        if not current_state:
            _time_sleep.sleep(0.05)
            current_state = ctx["state_manager"].get_state(ctx["phone_number"])
            if current_state:
                log_quality_metric("webhook_state_create_retry", request_id=ctx["request_id"])
            else:
                ctx["state_manager"].create_state(ctx["phone_number"], "NEW")
                current_state = ctx["state_manager"].get_state(ctx["phone_number"])
    ctx["current_state"] = current_state



def _legacy_reset_stale_conversation(ctx: dict) -> None:
    current_state = ctx.get("current_state")
    if not current_state or not current_state.get('last_message_at'):
        return
    last_msg = current_state['last_message_at']
    if not isinstance(last_msg, datetime):
        return
    try:
        now = datetime.now(last_msg.tzinfo) if last_msg.tzinfo else datetime.now()
        hours_since = (now - last_msg).total_seconds() / 3600
        timeout_hours = config.get_conversation_timeout_hours()
        if hours_since > timeout_hours:
            logger.info(
                "Resetting stale conversation for %s (inactive %.1f hours)",
                sanitize_log_value(ctx["phone_number"]),
                hours_since,
            )
            ctx["state_manager"].clear_booking(ctx["phone_number"])
            ctx["current_state"] = ctx["state_manager"].get_state(ctx["phone_number"])
    except Exception as timeout_err:
        logger.warning(
            "Timeout check failed for %s: %s",
            sanitize_log_value(ctx["phone_number"]),
            timeout_err,
        )



def _legacy_log_inbound_and_touch(ctx: dict):
    inbound_log_ok = ctx["state_manager"].log_inbound_and_touch(
        ctx["phone_number"], ctx["message_body"], ctx["media_urls"]
    )
    if inbound_log_ok:
        return None
    logger.error(
        "Inbound log/touch failed for %s; refusing to process message",
        sanitize_log_value(ctx["phone_number"]),
    )
    _record_webhook_monitor(request_id=ctx["request_id"], messages_failed=1)
    return jsonify(
        {
            "status": "error",
            "message": "Inbound persistence failed",
            "request_id": ctx["request_id"],
        }
    ), 503



def _legacy_set_state_observability(ctx: dict) -> None:
    current_state = ctx.get("current_state") or {}
    set_observability_context(state=str((current_state or {}).get("current_state") or "UNKNOWN"))
    if _detect_frustration(ctx["message_body"]):
        log_quality_metric("client_frustration_detected", phone=ctx["masked_phone"])



def _legacy_handle_silenced_bot(ctx: dict):
    current_state = ctx.get("current_state")
    silenced_until_str = current_state.get('silenced_until') if current_state else None
    if not silenced_until_str:
        return None
    try:
        silenced_until = datetime.fromisoformat(str(silenced_until_str))
        if silenced_until.tzinfo is None:
            silenced_until = silenced_until.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) < silenced_until:
            logger.info(
                'Bot silenced for %s until %s — ignoring inbound message',
                sanitize_log_value(ctx["phone_number"]),
                silenced_until_str,
            )
            return jsonify({"status": "success", "silenced": True, "request_id": ctx["request_id"]}), 200
    except Exception as sil_err:
        logger.warning(
            'Silence check failed for %s: %s',
            sanitize_log_value(ctx["phone_number"]),
            sil_err,
        )
    return None



def _legacy_handle_post_state_blocked_client(ctx: dict):
    if not ctx["state_manager"].is_blocked(ctx["phone_number"]):
        return None
    logger.warning("Blocked client reached fast-path guard: %s", sanitize_log_value(ctx["phone_number"]))
    return jsonify({"status": "blocked"}), 200



def _legacy_handle_awaiting_refund(ctx: dict):
    current_state = ctx.get("current_state")
    if not current_state or not current_state.get('awaiting_refund_details'):
        return None
    try:
        from handlers.reschedule_response import get_escort_forwarding_phone
        to_escort = get_escort_forwarding_phone()
        if to_escort:
            fwd_msg = f"Refund details from {ctx['phone_number']}:\n\n{ctx['message_body'][:500]}"
            send_escort_sms(to_escort, fwd_msg, category='refund_forwarding')
        ctx["state_manager"].transition(
            ctx["phone_number"],
            current_state['current_state'],
            updates={'awaiting_refund_details': False},
        )
        send_sms(
            ctx["phone_number"],
            "I've passed your details on for the refund. You'll be contacted if needed.",
        )
        return jsonify({"status": "ok", "forwarded_refund": True}), 200
    except Exception as e:
        logger.exception("Awaiting refund forward failed: %s", e)
        try:
            ctx["state_manager"].update_fields(ctx["phone_number"], {'awaiting_refund_details': False})
        except Exception as clear_err:
            logger.exception("Refund forward flag clear failed: %s", clear_err)
            log_quality_metric("refund_forward_flag_clear_failed", request_id=ctx["request_id"])
        return None



def _legacy_handle_pending_reschedule(ctx: dict):
    try:
        from handlers.reschedule_response import (
            get_pending_reschedule,
            handle_reschedule_cancel,
            handle_reschedule_confirm,
        )
        from services.calendar_service import get_calendar_service
        pending = get_pending_reschedule(ctx["phone_number"], ctx["db_service"])
        msg_upper = (ctx["message_body"] or "").strip().upper()
        if pending and msg_upper == "YES":
            replies = handle_reschedule_confirm(
                ctx["phone_number"], ctx["db_service"], get_calendar_service
            )
            if replies:
                for msg in replies:
                    ctx["state_manager"].log_message(ctx["phone_number"], 'outbound', msg)
                    send_sms(ctx["phone_number"], msg)
                return jsonify({"status": "ok", "reschedule_confirmed": True}), 200
        if pending and msg_upper == "CANCEL":
            replies, handled = handle_reschedule_cancel(
                ctx["phone_number"], ctx["db_service"], get_calendar_service, ctx["state_manager"]
            )
            if handled and replies:
                for msg in replies:
                    ctx["state_manager"].log_message(ctx["phone_number"], 'outbound', msg)
                    send_sms(ctx["phone_number"], msg)
                return jsonify({"status": "ok", "reschedule_cancelled": True}), 200
        if not pending:
            return None
        prompt = "Please reply YES to confirm the new time, CANCEL to cancel, or submit the booking webform for another time."
        ctx["state_manager"].log_message(ctx["phone_number"], 'outbound', prompt)
        send_sms(ctx["phone_number"], prompt)
        return jsonify({"status": "ok", "pending_reschedule_prompt": True}), 200
    except Exception as e:
        logger.exception("Pending reschedule check failed: %s", e)
        return None



def _legacy_handle_post_state_guards(ctx: dict):
    return _legacy_run_handlers(
        ctx,
        [
            _legacy_handle_silenced_bot,
            _legacy_handle_post_state_blocked_client,
            _legacy_handle_awaiting_refund,
            _legacy_handle_pending_reschedule,
        ],
    )



def _legacy_start_request_trace(ctx: dict) -> None:
    try:
        ctx["trace"] = start_trace(phone_number=ctx["phone_number"])
    except Exception as trace_err:
        logger.debug("request trace start skipped: %s", trace_err)



def _legacy_fetch_ai_message_history(ctx: dict) -> None:
    max_history_body_len = 2500
    ai_message_history = []
    try:
        hist_rows = ctx["db_service"].execute_query(
            """
            SELECT direction, message_body FROM message_history
            WHERE phone_number = %s
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (ctx["phone_number"],),
            fetch=True,
        ) or []
        hist_rows = list(reversed(hist_rows[1:]))
        for row in hist_rows:
            role = "user" if row.get("direction") == "inbound" else "assistant"
            body = (row.get("message_body") or "").strip()[:max_history_body_len]
            if body:
                ai_message_history.append({"role": role, "content": body})
    except Exception as hist_err:
        logger.warning(
            "Could not fetch AI message history for %s: %s",
            sanitize_log_value(ctx["phone_number"]),
            hist_err,
        )
    ctx["ai_message_history"] = ai_message_history



def _legacy_classify_intent(ctx: dict) -> None:
    with PerformanceTimer("intent_classification"):
        intent = ctx["classifier"].classify(
            ctx["message_body"],
            ctx["media_urls"],
            context={'state': ctx["current_state"], 'message_history': ctx["ai_message_history"]},
        )
    ctx["intent"] = intent
    set_observability_context(intent=str(intent))
    structured_logger.info(
        "intent_classified",
        phone_number=ctx["phone_number"],
        intent=intent,
        request_id=ctx["request_id"],
    )
    _legacy_safe_add_request_trace_event("intent_classified", intent=intent)
    from handlers.safety import track_profanity_signal
    track_profanity_signal(ctx["phone_number"], ctx["message_body"], ctx["state_manager"])
    if intent in ("rude_abusive", "unsafe_request"):
        _log_rollout_path_alert(
            path="safety_handler",
            phone_number=ctx["phone_number"],
            request_id=ctx["request_id"],
            intent=intent,
            state=ctx["current_state"].get("current_state", ""),
        )



def _legacy_build_conversation_context_core(ctx: dict) -> None:
    from core.conversation_context import ConversationContext
    from core.client_profile import build_client_profile_with_memory
    from services.client_memory_service import ClientMemoryService
    from services.episodic_memory_service import EpisodicMemoryService
    conversation_context = ConversationContext(ctx["db_service"])
    client_context = conversation_context.get_client_context(ctx["phone_number"])
    smart_defaults = conversation_context.get_smart_defaults(ctx["phone_number"])
    client_profile = build_client_profile_with_memory(
        ctx["current_state"],
        client_context,
        client_memory_service=ClientMemoryService(ctx["db_service"]),
        phone_number=ctx["phone_number"],
        history=ctx["ai_message_history"],
    )
    episodic_snippet = EpisodicMemoryService(ctx["db_service"]).get_episodic_context(
        ctx["phone_number"],
        ctx["message_body"],
    )
    if episodic_snippet:
        client_profile["episodic_prompt_snippet"] = episodic_snippet
    ctx["conversation_context"] = conversation_context
    ctx["client_context"] = client_context
    ctx["smart_defaults"] = smart_defaults
    ctx["client_profile"] = client_profile



def _legacy_load_semantic_memory_snippets(ctx: dict) -> None:
    semantic_memory_snippets = []
    try:
        from services.semantic_memory_service import SemanticMemoryService
        semantic_service = SemanticMemoryService(ctx["db_service"])
        semantic_memory_snippets = semantic_service.get_relevant_snippets(
            phone_number=ctx["phone_number"],
            query_text=ctx["message_body"],
            limit=3,
        )
    except Exception as sm_err:
        logger.warning(
            "semantic memory lookup failed for %s: %s",
            sanitize_log_value(ctx["phone_number"]),
            sm_err,
        )
    ctx["semantic_memory_snippets"] = semantic_memory_snippets



def _legacy_enqueue_semantic_memory_capture(ctx: dict) -> None:
    try:
        from services.ai_task_queue import enqueue_ai_task
        enqueue_ai_task(
            ctx["db_service"],
            task_type="semantic_memory_capture",
            payload={
                "phone_number": ctx["phone_number"],
                "message": ctx["message_body"],
                "intent": ctx["intent"],
                "state": ctx["current_state"].get("current_state"),
            },
        )
    except Exception as queue_err:
        logger.warning(
            "semantic memory queue enqueue failed for %s: %s",
            sanitize_log_value(ctx["phone_number"]),
            queue_err,
        )



def _legacy_evaluate_escalation(ctx: dict) -> None:
    escalation = {"triggered": False, "tags": [], "reasons": []}
    try:
        from services.escalation_service import evaluate_escalation
        escalation = evaluate_escalation(
            message=ctx["message_body"],
            intent=ctx["intent"],
            current_state=ctx["current_state"],
            client_context=ctx["client_context"],
        )
        if escalation.get("triggered") and "escalate_manual_review" in (escalation.get("tags") or []):
            ctx["state_manager"].update_fields(
                ctx["phone_number"],
                {
                    "manual_review_required": True,
                    "escalation_triggered_at": datetime.now(timezone.utc).isoformat(),
                },
            )
    except Exception as esc_err:
        logger.warning(
            "escalation evaluation failed for %s: %s",
            sanitize_log_value(ctx["phone_number"]),
            esc_err,
        )
    ctx["escalation"] = escalation



def _legacy_snapshot_pre_booking_fields(ctx: dict) -> None:
    pre_booking_fields = {}
    try:
        pre_booking_fields = ctx["state_manager"].get_booking_fields(ctx["phone_number"]) or {}
    except Exception as pre_booking_err:
        logger.warning(
            "pre-dispatch booking snapshot failed for %s: %s",
            ctx["masked_phone"],
            pre_booking_err,
        )
    ctx["pre_booking_fields"] = pre_booking_fields



def _legacy_dispatch_turn(ctx: dict) -> None:
    legacy_context = {
        'phone_number': ctx["phone_number"],
        'message': ctx["message_body"],
        'media_urls': ctx["media_urls"],
        'state': ctx["current_state"],
        'state_manager': ctx["state_manager"],
        'db_service': ctx["db_service"],
        'ai_service': ctx["ai_service"],
        'conversation_context': ctx["conversation_context"],
        'client_context': ctx["client_context"],
        'client_profile': ctx["client_profile"],
        'semantic_memory_snippets': ctx.get("semantic_memory_snippets", []),
        'smart_defaults': ctx["smart_defaults"],
        'message_history': ctx["ai_message_history"],
        'escalation': ctx["escalation"],
    }
    frustration_result = _check_frustration(
        ctx["message_body"],
        ctx["phone_number"],
        ctx["current_state"],
        ctx["state_manager"],
    )
    if frustration_result:
        ctx["result"] = frustration_result
        return
    from main_v2.state_machine_bridge import dispatch_message
    ctx["result"] = dispatch_message(
        phone_number=ctx["phone_number"],
        intent=ctx["intent"],
        legacy_context=legacy_context,
        router=ctx["router"],
        state_manager=ctx["state_manager"],
    )



def _legacy_record_field_contradictions(ctx: dict) -> None:
    try:
        post_booking_fields = ctx["state_manager"].get_booking_fields(ctx["phone_number"]) or {}
        new_extraction = {}
        for field in ("date", "time", "duration", "experience_type", "incall_outcall"):
            new_val = post_booking_fields.get(field)
            existing_val = ctx["pre_booking_fields"].get(field)
            if new_val is not None and new_val != existing_val:
                new_extraction[field] = new_val
        existing_state_for_contradiction = {
            "event_date": ctx["pre_booking_fields"].get("date"),
            "event_time": ctx["pre_booking_fields"].get("time"),
            "duration_minutes": ctx["pre_booking_fields"].get("duration"),
            "experience_type": ctx["pre_booking_fields"].get("experience_type"),
            "booking_type": ctx["pre_booking_fields"].get("booking_type")
            or ctx["pre_booking_fields"].get("incall_outcall"),
        }
        contradictions = _detect_field_contradiction(new_extraction, existing_state_for_contradiction)
        _legacy_safe_add_request_trace_event("fields_extracted", fields=list(new_extraction.keys()))
        if contradictions:
            log_quality_metric(
                "field_contradiction_detected",
                fields=contradictions,
                phone=ctx["masked_phone"],
            )
    except Exception as contradiction_err:
        logger.warning(
            "field contradiction detection failed for %s: %s",
            ctx["masked_phone"],
            contradiction_err,
        )



def _legacy_normalize_result(ctx: dict) -> None:
    result = ctx["result"]
    messages = result.get('messages', [])
    new_state = result.get('new_state')
    actions = result.get('actions', [])
    if not isinstance(actions, list):
        actions = []
    if ctx["escalation"].get("triggered"):
        for tag in ctx["escalation"].get("tags") or []:
            if tag and tag not in actions:
                actions.append(tag)
    ctx["messages"] = messages
    ctx["new_state"] = new_state
    ctx["actions"] = actions
    ctx["handler_returned_empty"] = False
    ctx["fallback_recovered"] = False



def _legacy_apply_rollout_path_alerts(ctx: dict) -> None:
    current_state = ctx["current_state"]
    new_state = ctx.get("new_state")
    if current_state.get("current_state") in ("CHECKING_AVAILABILITY", "DEPOSIT_REQUIRED"):
        _log_rollout_path_alert(
            path=current_state.get("current_state", "").lower(),
            phone_number=ctx["phone_number"],
            request_id=ctx["request_id"],
            intent=ctx["intent"],
            state=current_state.get("current_state", ""),
            new_state=new_state or "",
        )
    if new_state in ("CHECKING_AVAILABILITY", "DEPOSIT_REQUIRED"):
        _log_rollout_path_alert(
            path=f"transition_{new_state.lower()}",
            phone_number=ctx["phone_number"],
            request_id=ctx["request_id"],
            intent=ctx["intent"],
            state=current_state.get("current_state", ""),
            new_state=new_state,
        )



def _legacy_ensure_messages(ctx: dict) -> None:
    messages = ctx.get("messages")
    current_state = ctx["current_state"]
    terminal_states = frozenset({"CONFIRMED", "POST_BOOKING", "CANCELLED", "DEPOSIT_REQUIRED", "COMPLETED"})
    cur_state_name = (current_state.get("current_state") or "").upper()
    if not messages:
        ctx["handler_returned_empty"] = True
        structured_logger.warning(
            "empty_messages_from_handler",
            phone_number=ctx["phone_number"],
            state=current_state['current_state'],
            intent=ctx["intent"],
            request_id=ctx["request_id"],
        )
        log_quality_metric(
            "no_reply_prevented",
            phone_number=ctx["phone_number"],
            state=current_state['current_state'],
            intent=ctx["intent"],
        )
        if cur_state_name in terminal_states:
            structured_logger.warning(
                "empty_handler_in_terminal_state_no_fallback",
                phone_number=ctx["phone_number"],
                state=cur_state_name,
                intent=ctx["intent"],
                request_id=ctx["request_id"],
            )
            ctx["fallback_recovered"] = False
            return
        try:
            import config as _cfg
            from booking.field_collector import FieldCollector
            from utils.dinner_date import is_dinner_date_booking
            field_collector = FieldCollector(_cfg, ai_service=None)
            fields = ctx["state_manager"].get_booking_fields(ctx["phone_number"]) or {}
            missing = field_collector.get_missing_fields(fields)
            exp_ok = bool((fields.get("experience_type") or "").strip()) or is_dinner_date_booking(fields)
            is_outcall = str((fields.get("incall_outcall") or "")).lower() == "outcall"
            fallback = (
                field_prompts.get_prompt_for_missing_core_fields(
                    missing,
                    experience_already_set=exp_ok,
                    is_outcall=is_outcall,
                )
                if missing
                else None
            )
            ctx["messages"] = [
                fallback
                or field_prompts.get_ask_date_time_duration_prompt(
                    experience_already_set=exp_ok,
                    is_outcall=is_outcall,
                )
            ]
            ctx["fallback_recovered"] = True
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
            ctx["messages"] = [field_prompts.get_ask_date_time_duration_prompt()]
            ctx["fallback_recovered"] = True



def _legacy_apply_repeat_detection(ctx: dict) -> None:
    messages = ctx.get("messages")
    if not messages:
        return
    try:
        repeat_msg = _check_repeat_response(
            messages[0],
            ctx["phone_number"],
            ctx["db_service"],
            ctx["state_manager"],
        )
        if repeat_msg:
            messages = [repeat_msg]
    except Exception as repeat_err:
        logger.warning('Repeat detection error: %s', repeat_err)
        try:
            messages = [_build_repeat_guard_message(ctx["phone_number"])]
            logger.info(
                "Repeat guard exception for %s — substituting enquiry-template fallback",
                ctx["phone_number"],
            )
        except Exception as repeat_fallback_err:
            logger.warning('Repeat guard fallback build failed: %s', repeat_fallback_err)
    ctx["messages"] = messages



def _legacy_apply_state_transition(ctx: dict):
    current_state = ctx["current_state"]
    flow_version = (current_state or {}).get("flow_version") or "v1"
    new_state = ctx.get("new_state")
    transition_needed = flow_version != "v2" and new_state and new_state != current_state['current_state']
    transition_old_state = current_state['current_state'] if transition_needed else None
    transition_failed = False
    if transition_needed:
        try:
            success = ctx["state_manager"].transition(ctx["phone_number"], new_state)
            if success:
                log_state_transition(
                    phone_number=ctx["phone_number"],
                    old_state=transition_old_state,
                    new_state=new_state,
                    request_id=ctx["request_id"],
                )
                _legacy_safe_add_request_trace_event(
                    "state_transition",
                    from_state=transition_old_state,
                    to_state=new_state,
                )
            else:
                transition_failed = True
                structured_logger.error(
                    "state_transition_failed",
                    phone_number=ctx["phone_number"],
                    old_state=transition_old_state,
                    new_state=new_state,
                    request_id=ctx["request_id"],
                )
        except Exception as trans_err:
            transition_failed = True
            structured_logger.error(
                "state_transition_exception",
                phone_number=ctx["phone_number"],
                old_state=transition_old_state,
                new_state=new_state,
                error=str(trans_err),
                request_id=ctx["request_id"],
                exc_info=True,
            )
    if not transition_failed:
        return None
    _record_webhook_monitor(
        request_id=ctx["request_id"],
        handler_empty=ctx["handler_returned_empty"],
        messages_sent=0,
        messages_failed=0,
    )
    _legacy_safe_finish_request_trace(ctx.get("trace"), "state_transition_failed")
    return jsonify(
        {
            "status": "error",
            "message": "State persistence failed",
            "request_id": ctx["request_id"],
        }
    ), 503



def _legacy_strip_profile_url_for_followups(ctx: dict) -> None:
    current_state = ctx["current_state"]
    if not current_state.get("first_contact_sent"):
        return
    try:
        from config import get_profile_url as _get_purl
        profile_url = _get_purl()
        if profile_url:
            ctx["messages"] = _strip_profile_url_from_messages(ctx["messages"], profile_url)
    except Exception as profile_err:
        logger.warning("Profile URL strip failed: %s", profile_err)



def _legacy_extract_last_outbound_message(last_outbound_row) -> str:
    if isinstance(last_outbound_row, dict):
        return str(last_outbound_row.get("message_body") or "").strip()
    if isinstance(last_outbound_row, (list, tuple)) and last_outbound_row:
        return str(last_outbound_row[0] or "").strip()
    return ""



def _legacy_get_last_outbound_message(ctx: dict) -> str:
    if not ctx["messages"] or not ctx["db_service"]:
        return ""
    try:
        last_outbound_rows = ctx["db_service"].execute_query(
            """
            SELECT message_body FROM message_history
            WHERE phone_number = %s AND direction = 'outbound'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (ctx["phone_number"],),
            fetch=True,
        ) or []
        if last_outbound_rows:
            return _legacy_extract_last_outbound_message(last_outbound_rows[0])
    except Exception as same_err:
        logger.warning(
            "repeat tracking lookup failed for %s: %s",
            ctx["masked_phone"],
            same_err,
        )
    return ""



def _legacy_build_outbound_plan_entries(
    ctx: dict,
    current_state: dict,
    last_outbound_message: str,
    same_response_count: int,
):
    outbound_plan = []
    for msg in ctx["messages"]:
        msg_to_send = msg
        needs_rg = False
        if _should_use_repeat_guard(ctx["phone_number"], msg, current_state):
            msg_to_send = _build_repeat_guard_message(ctx["phone_number"])
            needs_rg = True
        tracked_message = str(msg_to_send or "").strip()
        if tracked_message and last_outbound_message and tracked_message == last_outbound_message:
            same_response_count += 1
        elif tracked_message:
            same_response_count = 1
        else:
            same_response_count = 0
        last_outbound_message = tracked_message
        outbound_plan.append((msg, msg_to_send, needs_rg))
    return outbound_plan, same_response_count



def _legacy_prepare_outbound_plan(ctx: dict) -> None:
    current_state = ctx["current_state"] or {}
    same_response_count = int((current_state or {}).get("_consecutive_same_response_count", 0) or 0)
    last_outbound_message = _legacy_get_last_outbound_message(ctx)
    outbound_plan, same_response_count = _legacy_build_outbound_plan_entries(
        ctx,
        current_state,
        last_outbound_message,
        same_response_count,
    )
    ctx["same_response_count"] = same_response_count
    ctx["outbound_plan"] = outbound_plan



def _legacy_persist_outbound_plan_batch(ctx: dict, outbound_plan) -> bool:
    if not outbound_plan or not ctx["db_service"]:
        return False
    try:
        with ctx["db_service"].transaction() as conn:
            for _orig, msg_to_send, needs_rg in outbound_plan:
                if needs_rg:
                    ctx["state_manager"].update_fields(
                        ctx["phone_number"],
                        {"booking_status": "repeat_guard_prompt_sent"},
                        conn=conn,
                    )
                ctx["state_manager"].log_message(
                    ctx["phone_number"],
                    "outbound",
                    msg_to_send,
                    conn=conn,
                )
            ctx["state_manager"].update_fields(
                ctx["phone_number"],
                {"_consecutive_same_response_count": ctx["same_response_count"]},
                conn=conn,
            )
        return True
    except Exception as ob_err:
        logger.exception("Outbound log transaction failed: %s", ob_err)
        return False



def _legacy_persist_outbound_plan_fallback(ctx: dict, outbound_plan) -> None:
    if not outbound_plan:
        return
    for _orig, msg_to_send, needs_rg in outbound_plan:
        if needs_rg:
            try:
                ctx["state_manager"].update_fields(
                    ctx["phone_number"],
                    {"booking_status": "repeat_guard_prompt_sent"},
                )
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e)
        try:
            ctx["state_manager"].log_message(ctx["phone_number"], "outbound", msg_to_send)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
    try:
        ctx["state_manager"].update_fields(
            ctx["phone_number"],
            {"_consecutive_same_response_count": ctx["same_response_count"]},
        )
    except Exception as same_update_err:
        logger.warning(
            "repeat tracking update failed for %s: %s",
            ctx["masked_phone"],
            same_update_err,
        )



def _legacy_persist_outbound_plan(ctx: dict) -> None:
    outbound_plan = ctx.get("outbound_plan") or []
    if outbound_plan and not _legacy_persist_outbound_plan_batch(ctx, outbound_plan):
        _legacy_persist_outbound_plan_fallback(ctx, outbound_plan)
    if outbound_plan and _check_repetition({"_consecutive_same_response_count": ctx["same_response_count"]}):
        logger.info(
            "3+ identical consecutive responses detected for %s (count=%s)",
            ctx["masked_phone"],
            ctx["same_response_count"],
        )



def _legacy_send_outbound_messages(ctx: dict) -> None:
    sent_count = 0
    failed_count = 0
    last_delivery_error = None
    for _orig, msg_to_send, _needs_rg in ctx.get("outbound_plan") or []:
        try:
            structured_logger.info(
                "sending_message",
                phone_number=ctx["phone_number"],
                message_preview=msg_to_send[:100],
                request_id=ctx["request_id"],
            )
            sms_ok = send_sms(ctx["phone_number"], msg_to_send)
            if sms_ok:
                sent_count += 1
                structured_logger.info(
                    "message_sent_ok",
                    phone_number=ctx["phone_number"],
                    request_id=ctx["request_id"],
                )
            else:
                failed_count += 1
                last_delivery_error = get_last_sms_error() or {}
                structured_logger.error(
                    "send_sms_returned_false",
                    phone_number=ctx["phone_number"],
                    message_preview=msg_to_send[:100],
                    delivery_error=last_delivery_error,
                    request_id=ctx["request_id"],
                )
        except Exception as send_err:
            failed_count += 1
            last_delivery_error = {
                "type": type(send_err).__name__,
                "message": str(send_err),
                "auth_error": False,
            }
            structured_logger.error(
                "send_message_failed",
                phone_number=ctx["phone_number"],
                error=str(send_err),
                request_id=ctx["request_id"],
                exc_info=True,
            )
    ctx["sent_count"] = sent_count
    ctx["failed_count"] = failed_count
    ctx["last_delivery_error"] = last_delivery_error



def _legacy_record_action_tags(ctx: dict) -> None:
    current_state = ctx["current_state"]
    for action in ctx["actions"]:
        action_tag = str(action or "").strip()
        if not action_tag:
            structured_logger.warning(
                "action_tag_malformed",
                phone_number=ctx["phone_number"],
                action_repr=repr(action),
                request_id=ctx["request_id"],
            )
            log_quality_metric(
                "action_tag_malformed",
                phone_number=ctx["phone_number"],
                state=current_state['current_state'],
                intent=ctx["intent"],
            )
            continue
        structured_logger.info(
            "action_executed",
            phone_number=ctx["phone_number"],
            action=action_tag,
            request_id=ctx["request_id"],
        )
        try:
            from services.conversation_event_service import record_conversation_event
            record_conversation_event(
                ctx["db_service"],
                phone_number=ctx["phone_number"],
                event_type="action_tag",
                from_state=current_state.get("current_state"),
                to_state=ctx.get("new_state") or current_state.get("current_state"),
                intent=ctx["intent"],
                metadata={"action_tag": action_tag, "request_id": ctx["request_id"]},
            )
        except Exception as ce_err:
            logger.warning(
                "conversation event logging failed for action tag %s: %s",
                action_tag,
                ce_err,
            )
        if action_tag not in _KNOWN_ACTION_TAGS:
            structured_logger.warning(
                "unknown_action_tag",
                phone_number=ctx["phone_number"],
                action=action_tag,
                request_id=ctx["request_id"],
            )
            log_quality_metric(
                "unknown_action_tag",
                phone_number=ctx["phone_number"],
                state=current_state['current_state'],
                intent=ctx["intent"],
                action=action_tag,
            )



def _legacy_handle_delivery_failure(ctx: dict):
    if ctx["sent_count"] != 0 or ctx["failed_count"] <= 0:
        return None
    if ctx.get("new_state") == 'CONFIRMED' and ctx.get("outbound_plan"):
        log_quality_metric(
            "confirmation_sms_undelivered",
            request_id=ctx["request_id"],
            failed_count=ctx["failed_count"],
        )
        structured_logger.error(
            "confirmation_sms_undelivered",
            phone_number=ctx["phone_number"],
            request_id=ctx["request_id"],
            messages_failed=ctx["failed_count"],
            delivery_error=ctx["last_delivery_error"] or {},
        )
    _record_webhook_monitor(
        request_id=ctx["request_id"],
        handler_empty=ctx["handler_returned_empty"],
        messages_sent=0,
        messages_failed=ctx["failed_count"],
    )
    _legacy_safe_finish_request_trace(ctx.get("trace"), "delivery_failed")
    return jsonify(
        {
            "status": "delivery_failed",
            "messages_sent": 0,
            "messages_failed": ctx["failed_count"],
            "request_id": ctx["request_id"],
            "delivery_error": ctx["last_delivery_error"] or {},
        }
    ), 200



def _legacy_record_turn_outcome(ctx: dict) -> None:
    effective_state = ctx.get("new_state") or ctx["current_state"].get("current_state") or "NEW"
    funnel_step = _FUNNEL_STEP_BY_STATE.get(effective_state, "unknown")
    turn_confidence = _estimate_turn_confidence_score(
        intent=ctx["intent"],
        actions=ctx["actions"],
        handler_returned_empty=ctx["handler_returned_empty"],
        message_failed_count=ctx["failed_count"],
        fallback_recovered=ctx["fallback_recovered"],
    )
    log_quality_metric(
        "turn_confidence_scored",
        phone_number=ctx["phone_number"],
        state=effective_state,
        intent=ctx["intent"],
        confidence=round(turn_confidence, 3),
        funnel_step=funnel_step,
    )
    try:
        from services.conversation_event_service import record_conversation_event
        record_conversation_event(
            ctx["db_service"],
            phone_number=ctx["phone_number"],
            event_type="turn_quality",
            from_state=ctx["current_state"].get("current_state"),
            to_state=effective_state,
            intent=ctx["intent"],
            metadata={
                "request_id": ctx["request_id"],
                "confidence_score": round(turn_confidence, 3),
                "funnel_step": funnel_step,
                "actions": [str(a or "") for a in ctx["actions"] if str(a or "").strip()],
                "messages_sent": ctx["sent_count"],
                "messages_failed": ctx["failed_count"],
            },
        )
        record_conversation_event(
            ctx["db_service"],
            phone_number=ctx["phone_number"],
            event_type="funnel_step",
            from_state=ctx["current_state"].get("current_state"),
            to_state=effective_state,
            intent=ctx["intent"],
            metadata={
                "request_id": ctx["request_id"],
                "step": funnel_step,
                "confidence_score": round(turn_confidence, 3),
            },
        )
    except Exception as ce_err:
        logger.warning("conversation event logging failed for turn quality: %s", ce_err)
    if ctx.get("new_state") == 'CONFIRMED':
        log_booking_event(
            event_type="confirmed",
            phone_number=ctx["phone_number"],
            request_id=ctx["request_id"],
        )
    _record_webhook_monitor(
        request_id=ctx["request_id"],
        handler_empty=ctx["handler_returned_empty"],
        messages_sent=ctx["sent_count"],
        messages_failed=ctx["failed_count"],
    )



def _legacy_build_success_response(ctx: dict):
    return jsonify(
        {
            "status": "success",
            "messages_sent": ctx["sent_count"],
            "messages_failed": ctx["failed_count"],
            "request_id": ctx["request_id"],
        }
    ), 200



def _legacy_handle_webhook_exception(request_id: str, trace, webhook_exc):
    error_msg = str(webhook_exc) if webhook_exc else "Unknown error"
    error_type = type(webhook_exc).__name__ if webhook_exc else "Exception"
    logger.error("Webhook error: %s", error_msg, exc_info=webhook_exc)
    try:
        structured_logger.error(
            "webhook_error",
            error=error_msg,
            error_type=error_type,
            request_id=request_id,
            exc_info=True,
        )
    except Exception as log_err:
        logger.warning(LOG_SUPPRESSED_FMT, log_err)
    err_l = (error_msg or "").lower()
    if "timeout" in err_l or "timed out" in err_l:
        sorry_msg = "Sorry, that took too long to process. Please try again in a moment."
    else:
        sorry_msg = "Sorry, something went wrong. Please try again later."
    try:
        to_phone = extract_webhook_contact_phone(request)
        if to_phone:
            from services.sms_service import send_sms as _send_sms
            _send_sms(to_phone, sorry_msg)
    except Exception as send_err:
        logger.error("Failed to send error fallback SMS: %s", send_err)
    status_code = 503 if ("timeout" in err_l or "timed out" in err_l) else 500
    safe_msg = (
        "Service temporarily unavailable. Please try again later."
        if status_code == 503
        else "An internal error occurred."
    )
    _legacy_safe_finish_request_trace(trace, "error")
    return jsonify({"status": "error", "message": safe_msg, "request_id": request_id}), status_code



def _process_webhook_legacy(request_id: str):
    """Legacy inbound webhook pipeline (fallback-safe processing path)."""
    ctx = _legacy_build_context(request_id)
    try:
        response = _legacy_handle_uninitialized_system(ctx)
        if response is not None:
            return response
        response = _legacy_prepare_inbound_request(ctx)
        if response is not None:
            return response
        response = _legacy_enforce_security(ctx)
        if response is not None:
            return response
        _legacy_log_inbound_request(ctx)
        response = _legacy_handle_pre_state_guards(ctx)
        if response is not None:
            return response
        response = _legacy_handle_fastpaths(ctx)
        if response is not None:
            return response
        _legacy_get_or_create_state(ctx)
        _legacy_reset_stale_conversation(ctx)
        response = _legacy_log_inbound_and_touch(ctx)
        if response is not None:
            return response
        _legacy_set_state_observability(ctx)
        response = _legacy_handle_post_state_guards(ctx)
        if response is not None:
            return response
        _legacy_start_request_trace(ctx)
        _legacy_fetch_ai_message_history(ctx)
        _legacy_classify_intent(ctx)
        _legacy_build_conversation_context_core(ctx)
        _legacy_load_semantic_memory_snippets(ctx)
        _legacy_enqueue_semantic_memory_capture(ctx)
        _legacy_evaluate_escalation(ctx)
        _legacy_snapshot_pre_booking_fields(ctx)
        _legacy_dispatch_turn(ctx)
        _legacy_record_field_contradictions(ctx)
        _legacy_normalize_result(ctx)
        _legacy_apply_rollout_path_alerts(ctx)
        _legacy_ensure_messages(ctx)
        _legacy_apply_repeat_detection(ctx)
        response = _legacy_apply_state_transition(ctx)
        if response is not None:
            return response
        _legacy_strip_profile_url_for_followups(ctx)
        _legacy_prepare_outbound_plan(ctx)
        _legacy_persist_outbound_plan(ctx)
        _legacy_send_outbound_messages(ctx)
        _legacy_record_action_tags(ctx)
        response = _legacy_handle_delivery_failure(ctx)
        if response is not None:
            return response
        _legacy_record_turn_outcome(ctx)
        _legacy_safe_finish_request_trace(ctx.get("trace"), "ok")
        return _legacy_build_success_response(ctx)
    except Exception as webhook_exc:
        return _legacy_handle_webhook_exception(request_id, ctx.get("trace"), webhook_exc)
