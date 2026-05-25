from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


_HEADER_TIMESTAMP_KEYS = ("X-Request-Timestamp", "X-Webhook-Timestamp")
_HEADER_TIMESTAMP_KEYS_NORMALIZED = {key.lower() for key in _HEADER_TIMESTAMP_KEYS}
_PAYLOAD_TIMESTAMP_KEYS = ("timestamp", "received_at", "created_at", "sent_at")


class ReplayValidationError(ValueError):
    """Raised when replay-window checks fail."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


def extract_request_timestamp(headers: Mapping[str, Any] | None, *payloads: Mapping[str, Any] | None) -> str:
    if isinstance(headers, Mapping):
        for header_name, raw_value in headers.items():
            normalized_name = str(header_name).strip().lower()
            if normalized_name not in _HEADER_TIMESTAMP_KEYS_NORMALIZED:
                continue
            value = str(raw_value or "").strip()
            if value:
                return value

    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        for key in _PAYLOAD_TIMESTAMP_KEYS:
            value = str(payload.get(key) or "").strip()
            if value:
                return value

    return ""


def validate_request_timestamp(
    raw_timestamp: str | int | float,
    *,
    tolerance_seconds: int = 300,
    max_future_skew_seconds: int = 30,
    now: datetime | None = None,
) -> int:
    raw = str(raw_timestamp or "").strip()
    if not raw:
        raise ReplayValidationError("Missing request timestamp")

    try:
        parsed = int(float(raw))
    except (TypeError, ValueError) as exc:
        raise ReplayValidationError("Invalid request timestamp") from exc

    now_utc = now or datetime.now(timezone.utc)
    now_epoch = int(now_utc.timestamp())
    tolerance = max(1, int(tolerance_seconds))
    future_skew = max(0, int(max_future_skew_seconds))

    if parsed < (now_epoch - tolerance):
        raise ReplayValidationError("Stale request timestamp")
    if parsed > (now_epoch + future_skew):
        raise ReplayValidationError("Request timestamp too far in future")

    return parsed
