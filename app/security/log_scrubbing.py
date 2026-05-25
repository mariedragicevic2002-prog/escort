from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any


_DEFAULT_SECRET_MARKERS = (
    "secret",
    "token",
    "password",
    "authorization",
    "api_key",
    "signature",
    "key",
    "encrypted",
)


def _is_secret_field(field_name: str, *, markers: Collection[str]) -> bool:
    normalized = str(field_name or "").strip().lower()
    return bool(normalized) and any(marker in normalized for marker in markers)


def _truncate(value: str, *, max_chars: int = 160) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}…"


def _scrub_value(value: Any, *, markers: Collection[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(k): ("[REDACTED]" if _is_secret_field(str(k), markers=markers) else _scrub_value(v, markers=markers))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub_value(item, markers=markers) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item, markers=markers) for item in value)
    if isinstance(value, str):
        return _truncate(value)
    return value


def scrub_payload_for_logging(
    payload: Mapping[str, Any] | None,
    *,
    allowlist: Collection[str] | None = None,
    secret_markers: Collection[str] | None = None,
) -> dict[str, Any]:
    """Keep only allowlisted keys and redact anything that looks secret-like."""
    if not isinstance(payload, Mapping):
        return {}

    markers = tuple(secret_markers or _DEFAULT_SECRET_MARKERS)
    allowed = {str(k).strip().lower() for k in (allowlist or payload.keys())}
    scrubbed: dict[str, Any] = {}
    for key, value in payload.items():
        key_name = str(key)
        if allowed and key_name.strip().lower() not in allowed:
            continue
        if _is_secret_field(key_name, markers=markers):
            scrubbed[key_name] = "[REDACTED]"
            continue
        scrubbed[key_name] = _scrub_value(value, markers=markers)
    return scrubbed
