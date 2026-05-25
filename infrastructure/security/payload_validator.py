"""
Inbound webhook payload schema validation.

Rejects malformed payloads at the infrastructure boundary before
any application or domain code runs.

Infrastructure layer — depends on pydantic (framework dependency).
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from pydantic import BaseModel, ValidationError

try:
    from pydantic import field_validator

    PYDANTIC_V2 = True
except ImportError:
    from pydantic import validator as field_validator  # type: ignore

    PYDANTIC_V2 = False

_PHONE_RE = re.compile(r"^\+?[1-9]\d{6,14}$")


class InboundWebhookPayload(BaseModel):
    """Validated inbound webhook payload."""

    phone_number: str
    message: str
    request_id: Optional[str] = None

    @field_validator("phone_number")
    def phone_must_be_valid(cls, value: str) -> str:
        value = value.strip()
        if not _PHONE_RE.match(value):
            raise ValueError(f"Invalid phone number format: {value!r}")
        return value

    @field_validator("message")
    def message_must_not_be_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message must not be empty")
        if len(value) > 4096:
            raise ValueError("Message exceeds maximum length (4096 chars)")
        return value

    @field_validator("request_id")
    def normalize_request_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None


def parse_payload(raw: Mapping[str, Any]) -> InboundWebhookPayload:
    """
    Parse and validate raw webhook dict.
    Raises pydantic.ValidationError on invalid input.
    """
    data = dict(raw or {})
    if PYDANTIC_V2:
        return InboundWebhookPayload.model_validate(data)
    return InboundWebhookPayload.parse_obj(data)


__all__ = ["InboundWebhookPayload", "parse_payload", "ValidationError"]
