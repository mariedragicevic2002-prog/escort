"""Reusable ingress security primitives for the refactor layer."""

from app.security.auth import (
    AuthVerificationResult,
    SharedSecretVerifier,
    SignatureVerificationResult,
    verify_timestamped_hmac_signature_with_rotation,
)
from app.security.log_scrubbing import scrub_payload_for_logging
from app.security.rbac import PermissionDeniedError, has_permission, require_permission
from app.security.replay import ReplayValidationError, extract_request_timestamp, validate_request_timestamp
from app.security.rotation import (
    SecretRotationConfig,
    SecretValidationWindow,
    build_secret_validation_window,
    resolve_secret_rotation_config,
)

__all__ = [
    "AuthVerificationResult",
    "PermissionDeniedError",
    "ReplayValidationError",
    "SecretRotationConfig",
    "SecretValidationWindow",
    "SharedSecretVerifier",
    "SignatureVerificationResult",
    "build_secret_validation_window",
    "extract_request_timestamp",
    "has_permission",
    "require_permission",
    "resolve_secret_rotation_config",
    "scrub_payload_for_logging",
    "verify_timestamped_hmac_signature_with_rotation",
    "validate_request_timestamp",
]
