"""
WebhookController — thin Flask route handler.

Responsibilities (and ONLY these):
  1. Extract raw payload from request
  2. Run InboundMiddlewarePipeline (auth, rate-limit, validate)
  3. Build InboundMessage and hand to ConversationEngine
  4. Pass ProcessingResult to ResponseComposer
  5. Dispatch via OutboundDispatcher
  6. Return HTTP 200 (or error code from middleware)

Zero business logic. Zero conditionals about conversation state.
Any branching on message content belongs in the pipeline stages.

Adapter layer: knows about Flask, nothing else.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from flask import Blueprint, jsonify, request

from app.orchestration.conversation_engine import ConversationEngine
from app.orchestration.middleware_pipeline import InboundMiddlewarePipeline, MiddlewareDenied
from app.orchestration.outbound_dispatcher import OutboundDispatcher
from app.orchestration.response_composer import ResponseComposer

logger = logging.getLogger(__name__)

bp = Blueprint("webhook_v3", __name__)

# Singletons — replaced by DI container when wired
_middleware = InboundMiddlewarePipeline()
_engine = ConversationEngine()
_composer = ResponseComposer()
_dispatcher = OutboundDispatcher()


def _extract_phone(payload: dict) -> str:
    return (
        payload.get("phone_number")
        or payload.get("from")
        or payload.get("phone")
        or ""
    ).strip()


@bp.post("/webhook/v3")
def inbound_v3():
    """
    Clean webhook endpoint backed by the orchestration pipeline.

    The existing /webhook route (in main_v2/webhook_main_flow.py) remains
    active during migration. This route is the target end-state.
    """
    request_id = str(uuid.uuid4().hex[:16])
    t0 = time.monotonic()

    try:
        raw = request.get_json(force=True, silent=True) or {}
        phone = _extract_phone(raw)

        # Middleware: rate-limit → security → payload validation
        try:
            _middleware.run(raw_payload=raw, phone=phone, flask_request=request)
        except MiddlewareDenied as denied:
            logger.warning(
                "webhook_v3.middleware_denied",
                extra={"request_id": request_id, "reason": denied.reason, "status": denied.http_status},
            )
            return jsonify({"error": denied.reason}), denied.http_status

        # Build InboundMessage for the pipeline
        from main_v2.pipeline.inbound_context import InboundMessage  # type: ignore
        inbound = InboundMessage(
            from_number=phone,
            body=raw.get("message") or raw.get("text") or raw.get("body") or "",
            message_sid=request_id,
            raw_payload=raw,
        )

        # Process through conversation engine
        result = _engine.process(inbound)

        # Compose and dispatch outbound messages
        messages = _composer.compose(result)
        if messages:
            _dispatcher.dispatch(phone, messages)

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "webhook_v3.ok",
            extra={
                "request_id": request_id,
                "phone": phone[:4] + "****",
                "messages_sent": len(messages),
                "elapsed_ms": elapsed_ms,
            },
        )
        return "", 200

    except Exception:
        logger.exception("webhook_v3.unhandled_error", extra={"request_id": request_id})
        return jsonify({"error": "internal_error"}), 500
