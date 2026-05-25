from __future__ import annotations

import logging
from typing import Callable

from app.middleware.contracts import NextHandler
from app.runtime.context import OrchestrationContext
from services.httpsms_dedup import build_inbound_dedup_key, try_claim_httpsms_message_id

logger = logging.getLogger(__name__)

KeyBuilder = Callable[[dict, dict, str, str], str]
KeyClaimer = Callable[[object, str], bool]


class RetryableInboundError(RuntimeError):
    """Raised when temporary infrastructure failures require sender retry."""


class IdempotencyMiddleware:
    """Claims provider message id before executing business logic."""

    def __init__(self, key_builder: KeyBuilder | None = None, key_claimer: KeyClaimer | None = None) -> None:
        self._uses_default_key_builder = key_builder is None
        self._key_builder = key_builder or self._default_key_builder
        self._key_claimer = key_claimer or try_claim_httpsms_message_id

    @staticmethod
    def _default_key_builder(message_data: dict, request_payload: dict, phone_number: str, body: str) -> str:
        return build_inbound_dedup_key(
            message_data,
            request_payload,
            phone_number=phone_number,
            message_body=body,
        )

    def __call__(self, context: OrchestrationContext, next_handler: NextHandler) -> list[str]:
        if self._uses_default_key_builder:
            message_data = (
                context.inbound.message_data
                if isinstance(context.inbound.message_data, dict)
                else dict(context.inbound.message_data or {})
            )
            request_payload = (
                context.inbound.request_payload
                if isinstance(context.inbound.request_payload, dict)
                else dict(context.inbound.request_payload or {})
            )
        else:
            message_data = dict(context.inbound.message_data or {})
            request_payload = dict(context.inbound.request_payload or {})
        dedup_key = self._key_builder(
            message_data,
            request_payload,
            context.inbound.phone_number,
            context.inbound.body,
        )
        context.metadata["idempotency_key"] = dedup_key

        if not dedup_key:
            logger.warning("idempotency key missing; processing request without dedup claim")
            context.metadata["idempotency_claimed"] = False
            context.metadata["idempotency_key_missing"] = True
            return next_handler(context)

        try:
            claimed = bool(self._key_claimer(context.runtime.db_service, dedup_key))
        except Exception as exc:
            logger.exception("idempotency claim failed for key=%s", dedup_key)
            raise RetryableInboundError("Idempotency store unavailable") from exc

        context.metadata["idempotency_claimed"] = claimed
        if not claimed:
            context.metadata["duplicate"] = True
            return []

        return next_handler(context)
