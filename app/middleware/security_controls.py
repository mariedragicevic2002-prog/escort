from __future__ import annotations

import logging
import os

from app.middleware.contracts import NextHandler
from app.runtime.context import OrchestrationContext
from app.security.auth import SharedSecretVerifier
from app.security.log_scrubbing import scrub_payload_for_logging
from app.security.replay import ReplayValidationError, extract_request_timestamp, validate_request_timestamp

logger = logging.getLogger(__name__)


class InboundSecurityError(ValueError):
    """Raised when ingress security checks fail."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


class SecurityControlsMiddleware:
    """Ingress security controls: auth verification, replay checks, safe request logging."""

    def __init__(
        self,
        *,
        auth_verifier: SharedSecretVerifier | None = None,
        replay_tolerance_seconds: int | None = None,
        replay_future_skew_seconds: int = 30,
        enforce_replay_window: bool = True,
    ) -> None:
        replay_tolerance = replay_tolerance_seconds
        if replay_tolerance is None:
            replay_tolerance = int(os.environ.get("SMS_REPLAY_TOLERANCE_SECONDS", "300") or 300)
        self._auth_verifier = auth_verifier
        self._replay_tolerance_seconds = max(1, int(replay_tolerance))
        self._replay_future_skew_seconds = max(0, int(replay_future_skew_seconds))
        self._enforce_replay_window = bool(enforce_replay_window)

    def __call__(self, context: OrchestrationContext, next_handler: NextHandler) -> list[str]:
        headers = context.inbound.request_headers
        if not isinstance(headers, dict):
            headers = dict(headers or {})
        remote_addr = (context.inbound.remote_addr or "").strip()

        if self._auth_verifier is not None:
            auth_result = self._auth_verifier.verify(headers=headers, remote_addr=remote_addr)
            context.metadata["auth_verified"] = auth_result.authorized
            context.metadata["auth_reason"] = auth_result.reason
            context.metadata["auth_key_version"] = auth_result.key_version
            context.metadata["auth_cutover_state"] = auth_result.cutover_state
            if not auth_result.authorized:
                raise InboundSecurityError("Unauthorized", status_code=401)

        request_payload = context.inbound.request_payload
        if not isinstance(request_payload, dict):
            request_payload = dict(request_payload or {})
        message_data = context.inbound.message_data
        if not isinstance(message_data, dict):
            message_data = dict(message_data or {})
        request_ts = extract_request_timestamp(headers, message_data, request_payload)
        context.metadata["request_timestamp_present"] = bool(request_ts)
        context.metadata["replay_window_enforced"] = bool(self._enforce_replay_window)
        if self._enforce_replay_window and request_ts:
            try:
                parsed_ts = validate_request_timestamp(
                    request_ts,
                    tolerance_seconds=self._replay_tolerance_seconds,
                    max_future_skew_seconds=self._replay_future_skew_seconds,
                )
            except ReplayValidationError as exc:
                context.metadata["replay_window_passed"] = False
                raise InboundSecurityError(str(exc), status_code=exc.status_code) from exc
            context.metadata["request_timestamp"] = parsed_ts
            context.metadata["replay_window_passed"] = True
        elif self._enforce_replay_window:
            context.metadata["replay_window_passed"] = False

        payload_for_logging = request_payload or message_data
        scrubbed_payload = scrub_payload_for_logging(
            payload_for_logging,
            allowlist=("from", "body", "message_id", "id", "timestamp", "received_at", "encrypted"),
        )
        context.metadata["scrubbed_ingress_payload"] = scrubbed_payload
        logger.info("refactor ingress payload=%s", scrubbed_payload)

        return next_handler(context)
