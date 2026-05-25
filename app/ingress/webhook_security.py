from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from psycopg2 import OperationalError

from services.database_service import DatabaseService

from app.security.auth import (
    AuthVerificationResult,
    verify_timestamped_hmac_signature_with_rotation,
)
from app.security.log_scrubbing import scrub_payload_for_logging
from app.security.replay import ReplayValidationError, extract_request_timestamp, validate_request_timestamp
from app.security.rotation import (
    SecretValidationWindow,
    build_secret_validation_window,
    match_secret,
    resolve_secret_rotation_config,
)
from services import httpsms_dedup


def _header_value(headers: Mapping[str, Any] | None, name: str) -> str:
    if not isinstance(headers, Mapping):
        return ""
    target = (name or "").strip().lower()
    for key, value in headers.items():
        if str(key).strip().lower() == target:
            return str(value or "").strip()
    return ""


def _extract_bearer_token(headers: Mapping[str, Any] | None) -> str:
    auth_header = _header_value(headers, "Authorization")
    if not auth_header:
        return ""
    scheme, _, token = auth_header.partition(" ")
    if scheme.strip().lower() != "bearer":
        return ""
    return token.strip()


def _decode_jwt_segment(segment: str) -> dict[str, Any]:
    padded = segment + "=" * ((4 - (len(segment) % 4)) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    parsed = json.loads(raw.decode("utf-8"))
    if isinstance(parsed, dict):
        return parsed
    return {}


def _is_signed_jwt_valid(
    token: str,
    *,
    window: SecretValidationWindow,
    now_epoch: int,
) -> tuple[bool, str, bool]:
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError:
        return False, "none", False

    try:
        header = _decode_jwt_segment(header_b64)
        payload = _decode_jwt_segment(payload_b64)
    except Exception:
        return False, "none", False

    if str(header.get("alg") or "").upper() != "HS256":
        return False, "none", False

    signature_input = f"{header_b64}.{payload_b64}".encode("ascii")
    try:
        signature_bytes = base64.urlsafe_b64decode(
            (signature_b64 + "=" * ((4 - (len(signature_b64) % 4)) % 4)).encode("ascii")
        )
    except Exception:
        return False, "none", False

    for option in window.accepted:
        expected = hmac.new(option.value.encode("utf-8"), signature_input, hashlib.sha256).digest()
        if hmac.compare_digest(signature_bytes, expected):
            try:
                exp = payload.get("exp")
                if exp is not None and int(float(exp)) < now_epoch:
                    return False, "none", False
            except (TypeError, ValueError):
                return False, "none", False
            return True, option.version, False
    for option in window.deprecated:
        expected = hmac.new(option.value.encode("utf-8"), signature_input, hashlib.sha256).digest()
        if hmac.compare_digest(signature_bytes, expected):
            return False, option.version, True
    return False, "none", False


def _rotation_value(rotation: Mapping[str, Any] | None, key: str) -> str:
    if not isinstance(rotation, Mapping):
        return ""
    return str(rotation.get(key) or "").strip()


def verify_webhook_bearer_authorization(
    headers: Mapping[str, Any] | None,
    secrets: Sequence[str] | None,
    *,
    rotation: Mapping[str, Any] | None = None,
) -> AuthVerificationResult:
    configured = [str(secret or "").strip() for secret in (secrets or []) if str(secret or "").strip()]
    rotation_config = resolve_secret_rotation_config(
        active_key=_rotation_value(rotation, "active_key"),
        next_key=_rotation_value(rotation, "next_key"),
        deprecated_key=_rotation_value(rotation, "deprecated_key"),
        cutover_state=_rotation_value(rotation, "cutover_state"),
        fallback_secrets=configured,
    )
    window = build_secret_validation_window(rotation_config)
    if not window.accepted:
        return AuthVerificationResult(
            True,
            "webhook_auth_not_configured",
            key_version="unconfigured",
            cutover_state=rotation_config.cutover_state,
        )

    token = _extract_bearer_token(headers)
    if not token:
        return AuthVerificationResult(
            False,
            "missing_bearer_token",
            key_version="none",
            cutover_state=window.cutover_state,
        )

    token_match = match_secret(token, window)
    if token_match.matched:
        return AuthVerificationResult(
            True,
            "webhook_secret_match",
            key_version=token_match.version,
            cutover_state=token_match.cutover_state,
        )
    if token_match.deprecated_match:
        return AuthVerificationResult(
            False,
            "deprecated_bearer_token",
            key_version=token_match.version,
            cutover_state=token_match.cutover_state,
        )

    signed, key_version, deprecated_match = _is_signed_jwt_valid(
        token,
        window=window,
        now_epoch=int(time.time()),
    )
    if signed:
        return AuthVerificationResult(
            True,
            "signed_bearer_jwt",
            key_version=key_version,
            cutover_state=window.cutover_state,
        )
    if deprecated_match:
        return AuthVerificationResult(
            False,
            "deprecated_bearer_token",
            key_version=key_version,
            cutover_state=window.cutover_state,
        )
    return AuthVerificationResult(
        False,
        "invalid_bearer_token",
        key_version="none",
        cutover_state=window.cutover_state,
    )


class WebhookIngressSecurityError(ValueError):
    """Raised when webhook ingress hardening checks fail."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 401,
        metric_name: str = "",
        observability_tags: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.metric_name = metric_name
        self.observability_tags = {
            str(key): str(value)
            for key, value in dict(observability_tags or {}).items()
            if str(key).strip()
        }


@dataclass(frozen=True)
class WebhookSecurityOutcome:
    scrubbed_payload: dict[str, Any]
    duplicate: bool
    dedup_key: str
    dedup_key_missing: bool
    auth_reason: str
    signature_verified: bool
    auth_key_version: str = "unknown"
    auth_cutover_state: str = "unknown"
    signature_key_version: str = "unknown"
    signature_cutover_state: str = "unknown"


def enforce_webhook_ingress_security(
    *,
    headers: Mapping[str, Any] | None,
    raw_body: bytes,
    payload: Mapping[str, Any] | None,
    message_data: Mapping[str, Any] | None,
    phone_number: str,
    message_body: str,
    db_service: DatabaseService | None,
    webhook_secrets: Sequence[str] | None = None,
    webhook_secret_rotation: Mapping[str, Any] | None = None,
    signature_secret: str = "",
    signature_secret_rotation: Mapping[str, Any] | None = None,
    signature_required: bool = False,
    signature_tolerance_seconds: int = 300,
    claim_dedup: bool = True,
) -> WebhookSecurityOutcome:
    payload_dict = payload if isinstance(payload, Mapping) else {}
    message_data_dict = message_data if isinstance(message_data, Mapping) else {}

    scrubbed_payload = scrub_payload_for_logging(
        payload_dict,
        allowlist=("event", "event_type", "type", "data", "message_id", "id", "timestamp", "received_at", "created_at"),
    )

    auth_result = verify_webhook_bearer_authorization(
        headers,
        webhook_secrets,
        rotation=webhook_secret_rotation,
    )
    auth_tags = {
        "auth_key_version": auth_result.key_version,
        "auth_cutover_state": auth_result.cutover_state,
    }
    if not auth_result.authorized:
        raise WebhookIngressSecurityError(
            "Unauthorized",
            status_code=401,
            metric_name="webhook_auth_rejected",
            observability_tags=auth_tags,
        )

    signature_verified = False
    signature_key_version = "none"
    signature_cutover_state = "unknown"
    if signature_required:
        signature_config = resolve_secret_rotation_config(
            active_key=str(signature_secret or "").strip(),
            next_key=_rotation_value(signature_secret_rotation, "next_key"),
            deprecated_key=_rotation_value(signature_secret_rotation, "deprecated_key"),
            cutover_state=_rotation_value(signature_secret_rotation, "cutover_state"),
        )
        signature_window = build_secret_validation_window(signature_config)
        signature_cutover_state = signature_window.cutover_state
        if not signature_window.accepted:
            raise WebhookIngressSecurityError(
                "Webhook signature secret not configured",
                status_code=503,
                metric_name="webhook_signature_misconfigured",
                observability_tags={
                    **auth_tags,
                    "signature_key_version": "unconfigured",
                    "signature_cutover_state": signature_cutover_state,
                },
            )

        timestamp = extract_request_timestamp(headers, message_data_dict, payload_dict)
        try:
            validate_request_timestamp(
                timestamp,
                tolerance_seconds=max(30, int(signature_tolerance_seconds or 300)),
            )
        except ReplayValidationError as exc:
            metric_name = "webhook_signature_invalid_timestamp"
            if "stale" in str(exc).lower():
                metric_name = "webhook_signature_stale"
            raise WebhookIngressSecurityError(
                str(exc),
                status_code=exc.status_code,
                metric_name=metric_name,
                observability_tags={
                    **auth_tags,
                    "signature_key_version": "none",
                    "signature_cutover_state": signature_cutover_state,
                },
            ) from exc

        signature = _header_value(headers, "X-Webhook-Signature") or _header_value(headers, "X-Signature")
        if not signature:
            raise WebhookIngressSecurityError(
                "Missing webhook signature",
                status_code=401,
                metric_name="webhook_signature_missing",
                observability_tags={
                    **auth_tags,
                    "signature_key_version": "none",
                    "signature_cutover_state": signature_cutover_state,
                },
            )

        verified = verify_timestamped_hmac_signature_with_rotation(
            active_secret=signature_config.active_key,
            next_secret=signature_config.next_key,
            deprecated_secret=signature_config.deprecated_key,
            cutover_state=signature_config.cutover_state,
            timestamp=timestamp,
            raw_body=raw_body or b"",
            provided_signature=signature,
        )
        signature_key_version = verified.key_version
        signature_cutover_state = verified.cutover_state
        if not verified.verified:
            metric_name = "webhook_signature_invalid"
            if verified.reason == "deprecated_secret_rejected":
                metric_name = "webhook_signature_deprecated_rejected"
            raise WebhookIngressSecurityError(
                "Invalid webhook signature",
                status_code=401,
                metric_name=metric_name,
                observability_tags={
                    **auth_tags,
                    "signature_key_version": signature_key_version,
                    "signature_cutover_state": signature_cutover_state,
                },
            )
        signature_verified = True

    dedup_key = httpsms_dedup.build_inbound_dedup_key(
        dict(message_data_dict),
        dict(payload_dict),
        phone_number=phone_number,
        message_body=message_body,
    )
    dedup_key_missing = False
    if not dedup_key:
        dedup_key_missing = True
        minute_bucket = int(time.time() // 60)
        digest = hashlib.sha256(f"{phone_number}|{message_body}|{minute_bucket}".encode("utf-8")).hexdigest()[:32]
        dedup_key = f"fallback:{digest}"

    claimed = True
    if claim_dedup:
        try:
            claimed = bool(httpsms_dedup.try_claim_httpsms_message_id(db_service, dedup_key))
        except OperationalError as exc:
            raise WebhookIngressSecurityError(
                "temporary database error",
                status_code=503,
                metric_name="webhook_dedup_transient_db",
            ) from exc
        except Exception as exc:
            raise WebhookIngressSecurityError(
                "temporary database error",
                status_code=503,
                metric_name="webhook_dedup_unexpected_error",
            ) from exc

    return WebhookSecurityOutcome(
        scrubbed_payload=scrubbed_payload,
        duplicate=not claimed,
        dedup_key=dedup_key,
        dedup_key_missing=dedup_key_missing,
        auth_reason=auth_result.reason,
        signature_verified=signature_verified,
        auth_key_version=auth_result.key_version,
        auth_cutover_state=auth_result.cutover_state,
        signature_key_version=signature_key_version,
        signature_cutover_state=signature_cutover_state,
    )
