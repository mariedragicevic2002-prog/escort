"""
Admin prompt playground.

Lets the operator test AI behavior (classification, extraction) against arbitrary
messages without mutating any production state. Read-only / test-only.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Generator

from flask import Blueprint, Response, jsonify, render_template, request

from admin.auth import require_auth
import config
from services.database_service import get_shared_db

logger = logging.getLogger("escort_chatbot.admin.playground")
playground_bp = Blueprint("playground", __name__, template_folder="../templates")

PLAYGROUND_INTENTS = [
    "booking_request",
    "availability_check",
    "cancellation",
    "reschedule",
    "rates_inquiry",
    "general_inquiry",
    "confirmation",
    "unclear",
    "escalation",
]


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return value


def _resolve_model_used(ai_service, requested_model: str) -> str:
    model_name = str(getattr(ai_service, "_last_usage", {}).get("model") or "").lower()
    if "gemini" in model_name:
        return "gemini"
    if "claude" in model_name:
        return "claude"
    return requested_model


@playground_bp.route("/admin/playground", methods=["GET"])
@require_auth
def playground_page():
    return render_template("playground.html")


@playground_bp.route("/admin/playground/test", methods=["POST"])
@require_auth
def playground_test():
    try:
        payload = request.get_json(silent=True) or {}
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            return jsonify({"error": "message must be a non-empty string"}), 400
        message = message.strip()
        if len(message) > 500:
            return jsonify({"error": "message must be 500 characters or fewer"}), 400

        raw_state = payload.get("state")
        if raw_state is not None and not isinstance(raw_state, str):
            return jsonify({"error": "state must be a string or null"}), 400
        state = (raw_state or "").strip() or None

        model = str(payload.get("model") or "claude").strip().lower()
        if model not in {"claude", "gemini"}:
            return jsonify({"error": "model must be claude or gemini"}), 400

        get_shared_db(config.DATABASE_URL)

        from services import model_router
        from services.ai_service import AIService

        ai_service = AIService(provider=model)
        complexity = model_router.classify_complexity(message, state)
        intent = ai_service.classify_intent(
            message,
            possible_intents=PLAYGROUND_INTENTS,
            hint=f"state: {state or 'new'}",
        )
        extracted_raw = ai_service.extract_booking_fields(message)
        confidence_fields = {}
        if isinstance(extracted_raw, dict):
            confidence_fields = _json_safe(extracted_raw.get("_confidence") or {})
        extracted = {
            key: _json_safe(value)
            for key, value in (extracted_raw or {}).items()
            if not str(key).startswith("_")
        }
        return jsonify(
            {
                "complexity": complexity,
                "intent": intent,
                "extracted": extracted,
                "model_used": _resolve_model_used(ai_service, model),
                "confidence_fields": confidence_fields,
            }
        )
    except Exception as e:
        logger.exception("playground test failed")
        return jsonify({"error": str(e)}), 500


@playground_bp.route("/admin/playground/stream", methods=["POST"])
@require_auth
def playground_stream():
    """SSE endpoint: stream AI chat tokens as they arrive.

    Body: {"message": str, "system_prompt": str|null, "model": "claude"|"gemini"}
    Returns: text/event-stream with data: {"token": "..."} events, then data: [DONE]
    """
    payload = request.get_json(silent=True) or {}
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        return jsonify({"error": "message must be a non-empty string"}), 400
    message = message.strip()
    if len(message) > 2000:
        return jsonify({"error": "message must be 2000 characters or fewer"}), 400

    raw_sys = payload.get("system_prompt")
    system_prompt = (raw_sys or "").strip() or None

    model = str(payload.get("model") or "gemini").strip().lower()
    if model not in {"claude", "gemini"}:
        return jsonify({"error": "model must be claude or gemini"}), 400

    get_shared_db(config.DATABASE_URL)

    from services.ai_service import AIService

    ai_service = AIService(provider=model)

    def _sse_generate() -> Generator[str, None, None]:
        try:
            for chunk in ai_service.stream_chat(message, system_prompt=system_prompt, model=model, max_tokens=600):
                yield f"data: {json.dumps({'token': chunk})}\n\n"
        except Exception as exc:
            logger.error("playground stream error: %s", exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return Response(
        _sse_generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
