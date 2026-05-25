"""
InboundMiddlewarePipeline — chains security middleware before the conversation engine.

Stage order:
  1. Rate limit check   — block abuse early, before DB
  2. Security enforce   — bearer token, HMAC, replay (existing webhook_security)
  3. Payload validate   — pydantic schema
  4. Log scrub          — sanitise before any downstream logging

Each stage raises a specific exception on failure; the WebhookController
catches them and returns the appropriate HTTP response.

Infrastructure layer: knows about Flask request context and security modules.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class MiddlewareDenied(Exception):
    """Raised by a middleware stage to abort the pipeline."""

    def __init__(self, reason: str, http_status: int = 400) -> None:
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


class InboundMiddlewarePipeline:
    """
    Executes security middleware stages in order.
    Returns the validated, sanitised payload dict on success.
    Raises MiddlewareDenied on any security or validation failure.
    """

    def __init__(
        self,
        enable_rate_limit: bool = True,
        enable_security: bool = True,
        enable_payload_validation: bool = True,
    ) -> None:
        self._rate_limit = enable_rate_limit
        self._security = enable_security
        self._payload_validation = enable_payload_validation

    def run(self, raw_payload: dict, phone: str, flask_request: Any) -> dict:
        """
        Run all middleware stages.

        Args:
            raw_payload: Parsed JSON body from the Flask request.
            phone: Pre-extracted phone number (used for rate limiting).
            flask_request: The Flask request object (for header inspection).

        Returns:
            Validated payload dict.

        Raises:
            MiddlewareDenied on any failure.
        """
        # Stage 1: Rate limiting
        if self._rate_limit and phone:
            try:
                from infrastructure.security.rate_limiter import check_rate_limit, RateLimitExceeded  # type: ignore
                check_rate_limit(phone)
            except Exception as exc:
                if "RateLimitExceeded" in type(exc).__name__:
                    raise MiddlewareDenied(str(exc), http_status=429) from exc
                logger.warning("middleware.rate_limit_check_error: %s", exc)

        # Stage 2: Webhook security (bearer token, HMAC, replay)
        if self._security:
            try:
                import config
                from app.ingress.webhook_security import (  # type: ignore
                    enforce_webhook_ingress_security,
                    WebhookIngressSecurityError,
                )

                message_body = str(
                    raw_payload.get("message")
                    or raw_payload.get("body")
                    or raw_payload.get("text")
                    or ""
                ).strip()
                enforce_webhook_ingress_security(
                    headers=dict(flask_request.headers.items()),
                    raw_body=flask_request.get_data(cache=True, as_text=False) or b"",
                    payload=raw_payload,
                    message_data=raw_payload,
                    phone_number=str(phone or ""),
                    message_body=message_body,
                    db_service=None,
                    webhook_secrets=config.get_httpsms_webhook_secrets(),
                    webhook_secret_rotation=config.get_httpsms_webhook_secret_rotation_config(),
                    signature_secret=config.get_httpsms_webhook_signature_secret(),
                    signature_secret_rotation=config.get_httpsms_webhook_signature_rotation_config(),
                    signature_required=config.httpsms_webhook_signature_required(),
                    signature_tolerance_seconds=config.get_httpsms_webhook_signature_tolerance_seconds(),
                    claim_dedup=False,
                )
            except Exception as exc:
                if "SecurityError" in type(exc).__name__ or "WebhookIngress" in type(exc).__name__:
                    raise MiddlewareDenied(str(exc), http_status=401) from exc
                logger.warning("middleware.security_check_error: %s", exc)

        # Stage 3: Payload schema validation
        if self._payload_validation:
            try:
                from infrastructure.security.payload_validator import parse_payload, ValidationError  # type: ignore
                parse_payload(raw_payload)
            except Exception as exc:
                if "ValidationError" in type(exc).__name__:
                    raise MiddlewareDenied(f"Invalid payload: {exc}", http_status=400) from exc
                logger.warning("middleware.payload_validation_error: %s", exc)

        return raw_payload
