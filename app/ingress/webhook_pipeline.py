from __future__ import annotations

import logging
from typing import Any, Callable

import config
from flask import jsonify, request

from main_v2 import runtime as _runtime
from main_v2.webhook_helpers import normalize_webhook_payload
from app.ingress.backpressure_policy import is_backpressure_reject_reason
from app.ingress.quick_ack import try_enqueue_webhook_quick_ack
from app.ingress.rollout_controls import load_webhook_ingress_quick_ack_settings
from app.ingress.webhook_security import (
    WebhookIngressSecurityError,
    enforce_webhook_ingress_security,
)
from utils.structured_logging import log_quality_metric

logger = logging.getLogger(__name__)

LegacyWebhookProcessor = Callable[[str], Any]


def _canon_phone(raw_phone: str) -> str:
    text = str(raw_phone or "").strip()
    if not text:
        return ""
    lead = "+" if text.startswith("+") else ""
    return lead + "".join(ch for ch in text if ch.isdigit())


def _extract_payload_parts() -> tuple[dict[str, Any], dict[str, Any], str]:
    payload = normalize_webhook_payload(request)
    event_type = str(payload.get("event") or payload.get("event_type") or payload.get("type") or "")
    msg_data = payload.get("data") or payload
    if not isinstance(msg_data, dict):
        msg_data = payload if isinstance(payload, dict) else {}
    return payload if isinstance(payload, dict) else {}, msg_data, event_type


def _is_inbound_event(event_type: str, msg_data: dict[str, Any]) -> bool:
    if event_type in ("message.phone.received", "message.received", "incoming"):
        return True
    return not event_type and bool(msg_data.get("contact") and msg_data.get("content"))


def run_refactor_webhook_ingress_pipeline(
    *,
    request_id: str,
    legacy_processor: LegacyWebhookProcessor,
) -> Any:
    """Feature-flagged quick-ack ingress bridge with fallback-safe legacy delegation."""
    settings = load_webhook_ingress_quick_ack_settings()
    if not settings.enabled:
        return legacy_processor(request_id)

    payload, msg_data, event_type = _extract_payload_parts()
    if not _is_inbound_event(event_type, msg_data):
        return legacy_processor(request_id)

    phone_number = _canon_phone(str(msg_data.get("contact") or ""))
    message_body = str(msg_data.get("content") or "")
    if len("".join(ch for ch in phone_number if ch.isdigit())) < 8 or not message_body.strip():
        return legacy_processor(request_id)

    db_service = getattr(_runtime, "db_service", None)
    if db_service is None:
        return legacy_processor(request_id)

    try:
        security = enforce_webhook_ingress_security(
            headers=dict(request.headers.items()),
            raw_body=request.get_data(cache=True, as_text=False) or b"",
            payload=payload,
            message_data=msg_data,
            phone_number=phone_number,
            message_body=message_body,
            db_service=db_service,
            webhook_secrets=config.get_httpsms_webhook_secrets(),
            webhook_secret_rotation=config.get_httpsms_webhook_secret_rotation_config(),
            signature_secret=config.get_httpsms_webhook_signature_secret(),
            signature_secret_rotation=config.get_httpsms_webhook_signature_rotation_config(),
            signature_required=config.httpsms_webhook_signature_required(),
            signature_tolerance_seconds=config.get_httpsms_webhook_signature_tolerance_seconds(),
            claim_dedup=False,
        )
    except WebhookIngressSecurityError as sec_err:
        if sec_err.metric_name:
            log_quality_metric(sec_err.metric_name, request_id=request_id, **sec_err.observability_tags)
        return jsonify({"status": "error", "message": str(sec_err)}), sec_err.status_code

    log_quality_metric(
        "webhook_secret_rotation_observed",
        request_id=request_id,
        auth_key_version=security.auth_key_version,
        auth_cutover_state=security.auth_cutover_state,
        signature_key_version=security.signature_key_version,
        signature_cutover_state=security.signature_cutover_state,
    )
    if security.dedup_key_missing:
        log_quality_metric(
            "webhook_dedup_key_missing",
            request_id=request_id,
            auth_key_version=security.auth_key_version,
            auth_cutover_state=security.auth_cutover_state,
            signature_key_version=security.signature_key_version,
            signature_cutover_state=security.signature_cutover_state,
        )

    quick_ack = try_enqueue_webhook_quick_ack(
        db_service=db_service,
        phone_number=phone_number,
        message_body=message_body,
        payload=payload,
        message_data=msg_data,
        request_headers=dict(request.headers.items()),
        remote_addr=request.remote_addr,
        request_id=request_id,
        dedup_key=security.dedup_key,
        dedup_key_missing=security.dedup_key_missing,
        auth_reason=security.auth_reason,
        signature_verified=security.signature_verified,
        auth_key_version=security.auth_key_version,
        auth_cutover_state=security.auth_cutover_state,
        signature_key_version=security.signature_key_version,
        signature_cutover_state=security.signature_cutover_state,
    )
    if quick_ack.accepted:
        logger.info(
            "webhook quick-ack accepted request_id=%s duplicate=%s reason=%s",
            request_id,
            quick_ack.duplicate,
            quick_ack.reason,
        )
        return jsonify(
            {
                "status": "accepted",
                "messages_sent": 0,
                "messages_failed": 0,
                "request_id": request_id,
            }
        ), 202
    if is_backpressure_reject_reason(quick_ack.reason):
        logger.warning(
            "webhook quick-ack rejected by backpressure request_id=%s reason=%s",
            request_id,
            quick_ack.reason,
        )
        return jsonify(
            {
                "status": "rejected",
                "messages_sent": 0,
                "messages_failed": 0,
                "request_id": request_id,
            }
        ), 503

    logger.warning(
        "webhook quick-ack skipped; falling back to sync path (reason=%s)",
        quick_ack.reason,
    )
    return legacy_processor(request_id)
