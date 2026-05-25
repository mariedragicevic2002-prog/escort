"""
main_v2/sms_gateway.py

Inbound SMS endpoint for the local Pi gateway relay (sms_receive.py script).
Accepts POST {from, body} from a trusted local relay and feeds it into the
chatbot processing pipeline.

Note: When using httpSMS as the primary gateway, inbound SMS arrives via the
/webhook endpoint directly from httpsms.com. This endpoint serves as a
secondary/local relay path for Pi-based or self-hosted setups.

Auth: a simple shared secret in the X-Gateway-Secret header (GATEWAY_SECRET env var).
The secret prevents random processes from injecting messages.
"""

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, jsonify, request
from refactor.adapters.sms_outbound_adapter import SMSOutboundAdapter
from refactor.app.ingress.backpressure_policy import is_backpressure_reject_reason
from refactor.app.ingress.quick_ack import try_enqueue_sms_quick_ack
from refactor.app.ingress.rollout_controls import (
    SMSRolloutDecision,
    emit_sms_rollout_metrics as _emit_sms_rollout_metrics,
    resolve_sms_rollout_decision as _resolve_rollout_decision,
)
from refactor.app.outbound import (
    OutboundDispatcher,
    OutboundMessage,
    OutboundQueuePublisher,
    resolve_sms_outbound_delivery_mode,
    resolve_sms_outbound_queue_sync_fallback,
)
from refactor.app.outbound.contracts import OutboundDispatchResult
from refactor.app.queue import DatabaseOutboundQueueRepository
from refactor.app.runtime.response_composer import ComposedResponse, compose_response
from refactor.app.security.auth import SharedSecretVerifier
from refactor.app.security.log_scrubbing import scrub_payload_for_logging

logger = logging.getLogger(__name__)

sms_gateway_bp = Blueprint("sms_gateway", __name__)

# Shared secret — set GATEWAY_SECRET env var on the Pi (or leave empty to skip auth on localhost).
_GATEWAY_SECRET = (os.environ.get("GATEWAY_SECRET") or "").strip()
_GATEWAY_SECRET_NEXT = (os.environ.get("GATEWAY_SECRET_NEXT") or "").strip()
_GATEWAY_SECRET_DEPRECATED = (os.environ.get("GATEWAY_SECRET_DEPRECATED") or "").strip()
_GATEWAY_SECRET_CUTOVER_STATE = (os.environ.get("GATEWAY_SECRET_CUTOVER_STATE") or "").strip()


def _resolve_sms_rollout_decision(phone_number: str) -> SMSRolloutDecision:
    return _resolve_rollout_decision(phone_number)


def _force_legacy_runtime_for_sms(decision: SMSRolloutDecision) -> bool:
    return bool(decision.emergency_rollback)


def _record_sms_rollout_metrics(*, decision: SMSRolloutDecision, phone_number: str, request_id: str) -> None:
    try:
        _emit_sms_rollout_metrics(
            decision=decision,
            phone_number=phone_number,
            request_id=request_id,
        )
    except Exception as exc:
        logger.warning("sms_gateway: rollout metrics skipped (%s)", type(exc).__name__)


def _try_sms_quick_ack(
    *,
    phone_number: str,
    message_body: str,
    message_data: dict[str, Any],
    request_payload: dict[str, Any],
    request_id: str,
) -> Any:
    from main_v2 import runtime as _runtime

    return try_enqueue_sms_quick_ack(
        db_service=getattr(_runtime, "db_service", None),
        phone_number=phone_number,
        message_body=message_body,
        message_data=message_data,
        request_payload=request_payload,
        request_headers=dict(request.headers.items()),
        remote_addr=request.remote_addr,
        request_id=request_id,
    )


def _check_gateway_auth() -> bool:
    """Validate the X-Gateway-Secret header via refactor auth verifier."""
    verifier = SharedSecretVerifier(
        secret_provider=lambda: _GATEWAY_SECRET,
        next_secret_provider=lambda: _GATEWAY_SECRET_NEXT,
        deprecated_secret_provider=lambda: _GATEWAY_SECRET_DEPRECATED,
        cutover_state_provider=lambda: _GATEWAY_SECRET_CUTOVER_STATE,
        header_name="X-Gateway-Secret",
        allow_loopback_without_secret=False,
    )
    result = verifier.verify(headers=dict(request.headers.items()), remote_addr=request.remote_addr)
    if not result.authorized:
        logger.warning(
            "sms_gateway: auth rejected (%s) key_version=%s cutover_state=%s",
            result.reason,
            result.key_version,
            result.cutover_state,
        )
    return result.authorized


@sms_gateway_bp.route("/sms/incoming", methods=["POST"])
def sms_incoming():
    """
    Receive an SMS forwarded by a local relay script.

    Accepts EITHER:
      Encrypted:  {"encrypted": "<fernet-token>"} with X-Encrypted: true header
      Plaintext:  {"from": "+61412345678", "body": "Hello"}

    Returns JSON with the bot's reply (also dispatched via the active SMS gateway).
    """
    if not _check_gateway_auth():
        logger.warning("sms_gateway: unauthorized request from %s", request.remote_addr)
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    raw_data = request.get_json(silent=True) or {}

    # Decrypt if encrypted payload
    is_encrypted = (
        (request.headers.get("X-Encrypted") or "").strip().lower() == "true"
        or "encrypted" in raw_data
    )
    if is_encrypted:
        token = (raw_data.get("encrypted") or "").strip()
        if not token:
            return jsonify({"status": "error", "message": "Missing encrypted payload"}), 400
        try:
            from utils.sms_crypto import decrypt_payload, is_encryption_enabled
            if not is_encryption_enabled():
                logger.error("sms_gateway: received encrypted payload but SMS_ENCRYPTION_KEY not set")
                return jsonify({"status": "error", "message": "Encryption not configured on server"}), 500
            data = decrypt_payload(token)
            logger.info("sms_gateway: payload decrypted successfully")
        except Exception as e:
            logger.warning("sms_gateway: decryption failed from %s: %s", request.remote_addr, e)
            return jsonify({"status": "error", "message": "Decryption failed"}), 401
    else:
        # Plaintext — warn if encryption is available but not used
        try:
            from utils.sms_crypto import is_encryption_enabled
            if is_encryption_enabled():
                logger.warning(
                    "sms_gateway: received plaintext payload but encryption key is configured. "
                    "Update sms_receive.py to encrypt payloads."
                )
        except ImportError:
            pass
        data = raw_data

    sender = (data.get("from") or "").strip()
    body = (data.get("body") or "").strip()

    if not sender:
        return jsonify({"status": "error", "message": "Missing 'from' field"}), 400
    if not body:
        return jsonify({"status": "error", "message": "Missing 'body' field"}), 400

    # Canonicalize phone number (same logic as webhook_main_flow)
    lead = "+" if sender.startswith("+") else ""
    phone_number = lead + "".join(c for c in sender if c.isdigit())

    digits_only = "".join(c for c in phone_number if c.isdigit())
    if len(digits_only) < 8:
        logger.warning("sms_gateway: invalid phone number: %s", sender)
        return jsonify({"status": "error", "message": "Invalid phone number"}), 400

    logger.info(
        "[SMS-GATEWAY] from=%s payload=%s",
        phone_number[-4:],
        scrub_payload_for_logging(
            data if isinstance(data, dict) else {},
            allowlist=("from", "body", "message_id", "id", "timestamp", "received_at", "encrypted"),
        ),
    )

    request_id = (
        (request.headers.get("X-Request-ID") or "").strip()
        or (data.get("message_id") or data.get("id") or "").strip()
        or hashlib.sha1(f"{phone_number}:{body}".encode("utf-8")).hexdigest()[:16]
    )

    rollout_decision = _resolve_sms_rollout_decision(phone_number)
    _record_sms_rollout_metrics(
        decision=rollout_decision,
        phone_number=phone_number,
        request_id=request_id,
    )
    force_legacy_runtime = _force_legacy_runtime_for_sms(rollout_decision)
    composed_response = ComposedResponse()

    # Feed into chatbot processing pipeline
    if force_legacy_runtime:
        try:
            composed_response = compose_response(_process_sms_message(phone_number, body))
        except Exception as e:
            logger.exception(
                "sms_gateway: rollback legacy processing failed for %s: %s",
                phone_number[-4:],
                e,
            )
            return jsonify({"status": "error", "message": "Processing failed"}), 500
    else:
        if not rollout_decision.use_refactor_runtime:
            logger.info(
                "sms_gateway: rollout reason=%s observed; refactor runtime remains primary",
                rollout_decision.reason,
            )
        quick_ack = _try_sms_quick_ack(
            phone_number=phone_number,
            message_body=body,
            message_data=data if isinstance(data, dict) else {},
            request_payload=raw_data if isinstance(raw_data, dict) else {},
            request_id=request_id,
        )
        if quick_ack.accepted:
            logger.info(
                "sms_gateway: quick-ack accepted request_id=%s duplicate=%s reason=%s",
                request_id,
                bool(getattr(quick_ack, "duplicate", False)),
                getattr(quick_ack, "reason", "accepted"),
            )
            return jsonify(
                {
                    "status": "accepted",
                    "messages_sent": 0,
                    "messages_failed": 0,
                    "replies": [],
                }
            ), 202
        if is_backpressure_reject_reason(getattr(quick_ack, "reason", "")):
            logger.warning(
                "sms_gateway: quick-ack rejected by backpressure request_id=%s reason=%s",
                request_id,
                getattr(quick_ack, "reason", "backpressure_reject"),
            )
            return jsonify(
                {
                    "status": "rejected",
                    "messages_sent": 0,
                    "messages_failed": 0,
                    "replies": [],
                }
            ), 503
        try:
            refactor_result = _process_sms_message_refactor(
                phone_number=phone_number,
                message_body=body,
                message_data=data if isinstance(data, dict) else {},
                request_payload=raw_data if isinstance(raw_data, dict) else {},
                request_id=request_id,
                request_headers=dict(request.headers.items()),
                remote_addr=request.remote_addr,
            )
            composed_response, duplicate = _compose_refactor_result(refactor_result)
            if duplicate:
                logger.info("sms_gateway: duplicate inbound skipped for %s", phone_number[-4:])
        except Exception as e:
            from refactor.app.middleware.idempotency import RetryableInboundError
            from refactor.app.middleware.security_controls import InboundSecurityError
            from refactor.app.middleware.request_validation import InboundValidationError

            if isinstance(e, InboundValidationError):
                return jsonify({"status": "error", "message": str(e)}), e.status_code
            if isinstance(e, InboundSecurityError):
                return jsonify({"status": "error", "message": str(e)}), e.status_code
            if isinstance(e, RetryableInboundError):
                logger.warning("sms_gateway: idempotency check unavailable for %s", phone_number[-4:])
                return jsonify({"status": "error", "message": "Temporary processing failure"}), 503

            logger.exception("sms_gateway: refactor pipeline failed; falling back to legacy path: %s", e)
            try:
                composed_response = compose_response(_process_sms_message(phone_number, body))
            except Exception as fallback_error:
                logger.exception(
                    "sms_gateway: legacy fallback also failed for %s: %s",
                    phone_number[-4:],
                    fallback_error,
                )
                return jsonify({"status": "error", "message": "Processing failed"}), 500

    dispatch_result = _dispatch_sms_outbound(
        phone_number=phone_number,
        composed_response=composed_response,
        request_id=request_id,
    )

    return jsonify({
        "status": "ok",
        "messages_sent": dispatch_result.sent,
        "messages_failed": dispatch_result.failed,
        "replies": composed_response.messages,
    }), 200


def _process_sms_message_refactor(
    *,
    phone_number: str,
    message_body: str,
    message_data: dict[str, Any],
    request_payload: dict[str, Any],
    request_id: str,
    request_headers: dict[str, Any] | None = None,
    remote_addr: str | None = None,
) -> Any:
    from main_v2 import runtime as _runtime
    from refactor.app.runtime.context import InboundSMSMessage
    from refactor.app.runtime.orchestration_facade import build_default_sms_facade

    inbound = InboundSMSMessage(
        phone_number=phone_number,
        body=message_body,
        message_data=message_data,
        request_payload=request_payload,
        request_id=request_id,
        request_headers=request_headers or {},
        remote_addr=str(remote_addr or ""),
    )

    facade = build_default_sms_facade(
        state_manager=_runtime.state_manager,
        db_service=_runtime.db_service,
        legacy_processor=_process_sms_message,
    )

    outcome = facade.process_sms(inbound)
    return outcome


def _compose_refactor_result(result: Any) -> tuple[ComposedResponse, bool]:
    duplicate = bool(getattr(result, "duplicate", False))

    if isinstance(result, tuple):
        if len(result) == 2:
            messages, duplicate = result
            return compose_response(messages), bool(duplicate)
        if len(result) == 3:
            messages, actions, duplicate = result
            return compose_response({"messages": messages, "actions": actions}), bool(duplicate)

    composed = compose_response(result)
    return composed, duplicate


def _build_sms_outbound_dispatcher(*, phone_number: str, state_manager: Any | None) -> OutboundDispatcher:
    def _log_outbound(message: OutboundMessage) -> None:
        if state_manager is None:
            return
        try:
            state_manager.log_message(phone_number, "outbound", message.body)
        except Exception as exc:
            logger.warning("Outbound log failed: %s", exc)

    return OutboundDispatcher(
        adapters=[SMSOutboundAdapter(_send_reply)],
        before_send=_log_outbound,
    )


def _publish_sms_outbound_queue(
    *,
    phone_number: str,
    request_id: str,
    outbound_messages: list[OutboundMessage],
    db_service: Any | None,
):
    if db_service is None:
        raise RuntimeError("db_service is required for queue outbound mode")
    queue_repository = DatabaseOutboundQueueRepository(db_service=db_service)
    publisher = OutboundQueuePublisher(queue_repository=queue_repository)
    return publisher.publish_messages(
        aggregate_id=phone_number,
        messages=outbound_messages,
        request_id=request_id,
        correlation_id=request_id,
    )


def _dispatch_sms_outbound(
    *,
    phone_number: str,
    composed_response: ComposedResponse,
    request_id: str,
) -> OutboundDispatchResult:
    from main_v2 import runtime as _runtime

    dispatcher = _build_sms_outbound_dispatcher(
        phone_number=phone_number,
        state_manager=getattr(_runtime, "state_manager", None),
    )
    outbound_messages = [
        OutboundMessage(
            channel="sms",
            recipient=phone_number,
            body=message,
            metadata={"actions": composed_response.actions},
        )
        for message in composed_response.messages
    ]

    delivery_mode = resolve_sms_outbound_delivery_mode()
    if delivery_mode != "queue":
        return dispatcher.dispatch(outbound_messages)

    try:
        publish_result = _publish_sms_outbound_queue(
            phone_number=phone_number,
            request_id=request_id,
            outbound_messages=outbound_messages,
            db_service=getattr(_runtime, "db_service", None),
        )
    except Exception as exc:
        logger.warning("sms_gateway: outbound queue publish failed, using sync dispatch (%s)", type(exc).__name__)
        return dispatcher.dispatch(outbound_messages)

    accepted = publish_result.queued + publish_result.duplicates
    failed_indices = set(publish_result.failed_indices)
    if failed_indices and resolve_sms_outbound_queue_sync_fallback():
        fallback_messages = [
            message
            for index, message in enumerate(outbound_messages)
            if index in failed_indices
        ]
        fallback_result = dispatcher.dispatch(fallback_messages)
        return OutboundDispatchResult(
            attempted=len(outbound_messages),
            sent=accepted + fallback_result.sent,
            failed=fallback_result.failed,
        )
    return OutboundDispatchResult(
        attempted=len(outbound_messages),
        sent=accepted,
        failed=publish_result.failed,
    )


def _send_reply(phone: str, message: str) -> bool:
    """Send a reply SMS via the configured SMS gateway (sms_service facade)."""
    try:
        from services import sms_service
        if sms_service.send_sms(phone, message):
            return True
        logger.warning("SMS send failed for %s", phone[-4:])
    except Exception as e:
        logger.warning("SMS send error: %s", e)
    return False


def _process_sms_message(phone_number: str, message_body: str) -> list[str]:
    """
    Run the incoming message through the chatbot pipeline and return reply messages.
    This reuses the core logic from webhook_main_flow.
    """
    from main_v2 import runtime as _runtime
    from utils.structured_logging import set_observability_context

    state_manager = _runtime.state_manager
    db_service = _runtime.db_service
    ai_service = _runtime.ai_service
    classifier = _runtime.classifier
    router = _runtime.router

    if not state_manager or not db_service:
        logger.error("sms_gateway: system not initialized")
        from templates.errors import get_system_error_message
        return [get_system_error_message("")]

    set_observability_context(phone_number=phone_number)
    media_urls = []

    # Check if blocked
    if state_manager.is_blocked(phone_number):
        logger.warning("sms_gateway: blocked client %s", phone_number[-4:])
        return []

    # Check chatbot enabled
    try:
        from core.settings_manager import get_setting
        if get_setting("chatbot_enabled") == "0":
            logger.info("sms_gateway: chatbot disabled, ignoring %s", phone_number[-4:])
            return []
    except Exception:
        pass

    # Safety screening
    try:
        from services.safety_screening_service import is_screening_enabled, lookup_flagged_number, get_screening_mode
        if is_screening_enabled():
            result = lookup_flagged_number(phone_number)
            if result.get("matched") and get_screening_mode() == "auto_block":
                state_manager.block_client(
                    phone_number=phone_number,
                    reason="safety_screening_watchlist",
                    notes="Matched watchlist (SMS gateway)",
                )
                return []
    except Exception as e:
        logger.warning("Safety screening check failed: %s", e)

    # Get or create state
    current_state = state_manager.get_state(phone_number)
    if not current_state:
        state_manager.create_state(phone_number, "NEW")
        current_state = state_manager.get_state(phone_number)

    # Stale conversation reset
    if current_state and current_state.get("last_message_at"):
        last_msg = current_state["last_message_at"]
        if isinstance(last_msg, datetime):
            try:
                import config
                now = datetime.now(last_msg.tzinfo) if last_msg.tzinfo else datetime.now()
                hours_since = (now - last_msg).total_seconds() / 3600
                timeout_hours = config.get_conversation_timeout_hours()
                if hours_since > timeout_hours:
                    logger.info("Resetting stale conversation for %s (%.1fh)", phone_number[-4:], hours_since)
                    state_manager.clear_booking(phone_number)
                    current_state = state_manager.get_state(phone_number)
            except Exception as e:
                logger.warning("Timeout check failed: %s", e)

    # Log inbound
    state_manager.log_inbound_and_touch(phone_number, message_body, media_urls)

    # Silenced check
    silenced_until_str = current_state.get("silenced_until") if current_state else None
    if silenced_until_str:
        try:
            silenced_until = datetime.fromisoformat(str(silenced_until_str))
            if silenced_until.tzinfo is None:
                silenced_until = silenced_until.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) < silenced_until:
                return []
        except Exception:
            pass

    # Fetch message history for classifier context
    _ai_message_history = []
    try:
        rows = db_service.execute_query(
            """
            SELECT direction, message_body FROM message_history
            WHERE phone_number = %s
            ORDER BY created_at DESC
            LIMIT 9
            """,
            (phone_number,),
            fetch=True,
        ) or []
        rows = list(reversed(rows[1:]))
        for row in rows:
            role = "user" if row.get("direction") == "inbound" else "assistant"
            text = (row.get("message_body") or "").strip()
            if text:
                _ai_message_history.append({"role": role, "content": text})
    except Exception as e:
        logger.warning("Message history fetch failed: %s", e)

    # Classify intent
    intent = classifier.classify(
        message_body, media_urls,
        context={"state": current_state, "message_history": _ai_message_history},
    )
    logger.info("[SMS-GATEWAY] intent=%s state=%s", intent, (current_state or {}).get("current_state", "NEW"))

    # Build context
    try:
        from core.conversation_context import ConversationContext
        from core.client_profile import build_client_profile_with_memory
        from services.client_memory_service import ClientMemoryService
        from services.episodic_memory_service import EpisodicMemoryService

        conversation_context = ConversationContext(db_service)
        client_context = conversation_context.get_client_context(phone_number)
        smart_defaults = conversation_context.get_smart_defaults(phone_number)
        client_memory_service = ClientMemoryService(db_service)
        client_profile = build_client_profile_with_memory(
            current_state,
            client_context,
            client_memory_service=client_memory_service,
            phone_number=phone_number,
        )
        episodic_snippet = EpisodicMemoryService(db_service).get_episodic_context(phone_number, message_body)
        if episodic_snippet:
            client_profile["episodic_prompt_snippet"] = episodic_snippet
    except Exception as e:
        logger.warning("Context build failed: %s", e)
        client_context = {}
        smart_defaults = {}
        client_profile = {}
        conversation_context = None

    semantic_memory_snippets = []
    try:
        from services.semantic_memory_service import SemanticMemoryService
        sms = SemanticMemoryService(db_service)
        semantic_memory_snippets = sms.get_relevant_snippets(
            phone_number=phone_number, query_text=message_body, limit=3,
        )
    except Exception:
        pass

    context = {
        "phone_number": phone_number,
        "message": message_body,
        "media_urls": media_urls,
        "state": current_state,
        "state_manager": state_manager,
        "db_service": db_service,
        "ai_service": ai_service,
        "conversation_context": conversation_context,
        "client_context": client_context,
        "client_profile": client_profile,
        "semantic_memory_snippets": semantic_memory_snippets,
        "smart_defaults": smart_defaults,
        "message_history": _ai_message_history,
        "escalation": {"triggered": False, "tags": [], "reasons": []},
    }

    # Route to handler
    from main_v2.state_machine_bridge import dispatch_message
    result = dispatch_message(
        phone_number=phone_number,
        intent=intent,
        legacy_context=context,
        router=router,
        state_manager=state_manager,
    )

    messages = result.get("messages", [])
    new_state = result.get("new_state")

    # Transition state if needed
    if new_state and current_state:
        old = current_state.get("current_state", "NEW")
        if new_state != old:
            try:
                state_manager.transition(phone_number, new_state)
            except Exception as e:
                logger.warning("State transition failed: %s", e)

    return [m for m in messages if m and m.strip()]
