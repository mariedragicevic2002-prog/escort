from __future__ import annotations

from app.middleware.contracts import NextHandler
from app.runtime.context import OrchestrationContext


class InboundValidationError(ValueError):
    """Raised when inbound payload violates request contract."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class RequestValidationMiddleware:
    """Validates canonicalized inbound payload before orchestration."""

    def __init__(self, max_body_chars: int = 4000) -> None:
        self._max_body_chars = max_body_chars

    def __call__(self, context: OrchestrationContext, next_handler: NextHandler) -> list[str]:
        phone_number = (context.inbound.phone_number or "").strip()
        body = (context.inbound.body or "").strip()
        digit_count = 0
        for char in phone_number:
            if char.isdigit():
                digit_count += 1
                if digit_count >= 8:
                    break

        if digit_count < 8:
            raise InboundValidationError("Invalid phone number")
        if not body:
            raise InboundValidationError("Missing 'body' field")
        if len(body) > self._max_body_chars:
            raise InboundValidationError("Message body too large", status_code=413)

        return next_handler(context)
