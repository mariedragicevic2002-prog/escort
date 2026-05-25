"""
Inbound webhook payload parsing — adapter layer.

Thin schema that normalises varied inbound formats into a
standard InboundWebhookPayload before calling the use case.

Adapter layer: may import from core/ and infrastructure/security/.
Must NOT contain business logic.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedPayload:
    phone: str
    text: str
    request_id: str
    raw: dict


def parse_flask_request(request_data: dict) -> ParsedPayload:
    """
    Normalise a raw Flask request JSON dict into a ParsedPayload.
    Raises KeyError / ValueError on missing required fields.
    """
    phone = (
        request_data.get("phone_number")
        or request_data.get("from")
        or request_data.get("phone")
        or ""
    ).strip()

    text = (
        request_data.get("message")
        or request_data.get("text")
        or request_data.get("body")
        or ""
    ).strip()

    request_id = (
        request_data.get("request_id")
        or request_data.get("id")
        or ""
    )

    if not phone:
        raise ValueError("Missing required field: phone_number")
    if not text:
        raise ValueError("Missing required field: message")

    return ParsedPayload(phone=phone, text=text, request_id=request_id, raw=request_data)
