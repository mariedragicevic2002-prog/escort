from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from app.security.rotation import (
    SecretValidationWindow,
    build_secret_validation_window,
    match_secret,
    resolve_secret_rotation_config,
)


def _header_value(headers: Mapping[str, Any] | None, name: str) -> str:
    if not isinstance(headers, Mapping):
        return ""
    target = (name or "").strip().lower()
    for key, value in headers.items():
        if str(key).strip().lower() == target:
            return str(value or "").strip()
    return ""


@dataclass(frozen=True)
class AuthVerificationResult:
    authorized: bool
    reason: str = ""
    key_version: str = "unknown"
    cutover_state: str = "unknown"


@dataclass(frozen=True)
class SignatureVerificationResult:
    verified: bool
    reason: str = ""
    key_version: str = "unknown"
    cutover_state: str = "unknown"


class SharedSecretVerifier:
    """Constant-time verification for shared-secret ingress authentication."""

    def __init__(
        self,
        *,
        secret_provider: Callable[[], str] | None = None,
        next_secret_provider: Callable[[], str] | None = None,
        deprecated_secret_provider: Callable[[], str] | None = None,
        cutover_state_provider: Callable[[], str] | None = None,
        header_name: str = "X-Gateway-Secret",
        allow_loopback_without_secret: bool = True,
    ) -> None:
        self._secret_provider = secret_provider or (lambda: "")
        self._next_secret_provider = next_secret_provider or (lambda: "")
        self._deprecated_secret_provider = deprecated_secret_provider or (lambda: "")
        self._cutover_state_provider = cutover_state_provider or (lambda: "")
        self._header_name = header_name
        self._allow_loopback_without_secret = allow_loopback_without_secret

    def verify(self, *, headers: Mapping[str, Any] | None, remote_addr: str | None) -> AuthVerificationResult:
        config = resolve_secret_rotation_config(
            active_key=self._secret_provider(),
            next_key=self._next_secret_provider(),
            deprecated_key=self._deprecated_secret_provider(),
            cutover_state=self._cutover_state_provider(),
        )
        window = build_secret_validation_window(config)
        remote = str(remote_addr or "").strip()

        if not window.accepted:
            if self._allow_loopback_without_secret and remote in {"127.0.0.1", "::1", "localhost"}:
                return AuthVerificationResult(
                    True,
                    "loopback_allowed_without_secret",
                    key_version="unconfigured",
                    cutover_state=config.cutover_state,
                )
            return AuthVerificationResult(
                False,
                "secret_not_configured",
                key_version="unconfigured",
                cutover_state=config.cutover_state,
            )

        provided = _header_value(headers, self._header_name)
        if not provided:
            return AuthVerificationResult(
                False,
                "missing_secret_header",
                key_version="none",
                cutover_state=window.cutover_state,
            )
        match = match_secret(provided, window)
        if match.matched:
            return AuthVerificationResult(
                True,
                "verified",
                key_version=match.version,
                cutover_state=match.cutover_state,
            )
        if match.deprecated_match:
            return AuthVerificationResult(
                False,
                "deprecated_secret_rejected",
                key_version=match.version,
                cutover_state=match.cutover_state,
            )
        return AuthVerificationResult(
            False,
            "secret_mismatch",
            key_version=match.version,
            cutover_state=match.cutover_state,
        )


def _normalized_signature_value(provided_signature: str) -> str:
    normalized = str(provided_signature or "").strip()
    if normalized.lower().startswith("sha256="):
        normalized = normalized.split("=", 1)[1].strip()
    return normalized


def _signature_digest(secret: str, *, timestamp: str, raw_body: bytes) -> str:
    return hmac.new(
        str(secret).encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + (raw_body or b""),
        "sha256",
    ).hexdigest()


def _match_signature_against_window(
    *,
    window: SecretValidationWindow,
    timestamp: str,
    raw_body: bytes,
    provided_signature: str,
) -> SignatureVerificationResult:
    normalized = _normalized_signature_value(provided_signature)
    if not timestamp or not normalized:
        return SignatureVerificationResult(
            False,
            reason="invalid_signature",
            key_version="none",
            cutover_state=window.cutover_state,
        )
    if not window.accepted:
        return SignatureVerificationResult(
            False,
            reason="secret_not_configured",
            key_version="unconfigured",
            cutover_state=window.cutover_state,
        )

    for option in window.accepted:
        expected = _signature_digest(option.value, timestamp=timestamp, raw_body=raw_body)
        if hmac.compare_digest(normalized, expected):
            return SignatureVerificationResult(
                True,
                reason="verified",
                key_version=option.version,
                cutover_state=window.cutover_state,
            )
    for option in window.deprecated:
        expected = _signature_digest(option.value, timestamp=timestamp, raw_body=raw_body)
        if hmac.compare_digest(normalized, expected):
            return SignatureVerificationResult(
                False,
                reason="deprecated_secret_rejected",
                key_version=option.version,
                cutover_state=window.cutover_state,
            )
    return SignatureVerificationResult(
        False,
        reason="invalid_signature",
        key_version="none",
        cutover_state=window.cutover_state,
    )


def verify_timestamped_hmac_signature_with_rotation(
    *,
    active_secret: str,
    timestamp: str,
    raw_body: bytes,
    provided_signature: str,
    next_secret: str = "",
    deprecated_secret: str = "",
    cutover_state: str = "",
) -> SignatureVerificationResult:
    config = resolve_secret_rotation_config(
        active_key=active_secret,
        next_key=next_secret,
        deprecated_key=deprecated_secret,
        cutover_state=cutover_state,
    )
    window = build_secret_validation_window(config)
    return _match_signature_against_window(
        window=window,
        timestamp=timestamp,
        raw_body=raw_body,
        provided_signature=provided_signature,
    )


def verify_timestamped_hmac_signature(
    *,
    secret: str,
    timestamp: str,
    raw_body: bytes,
    provided_signature: str,
) -> bool:
    """Verify a sha256=<digest> signature over '<timestamp>.<raw_body>'."""
    result = verify_timestamped_hmac_signature_with_rotation(
        active_secret=secret,
        timestamp=timestamp,
        raw_body=raw_body,
        provided_signature=provided_signature,
    )
    return result.verified
