"""
Sensitive data scrubbing for logs.

Scrubs phone numbers, bearer tokens, API keys, Authorization headers,
database DSNs, and payment-card data before log records are emitted.
Usable both as direct utility functions and as a logging.Filter.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Collection, Mapping
from typing import Any

_DEFAULT_SECRET_MARKERS = (
    "secret",
    "token",
    "password",
    "authorization",
    "api_key",
    "apikey",
    "signature",
    "private_key",
    "access_key",
)
_PHONE_FIELD_MARKERS = ("phone", "mobile", "msisdn")
_PAYMENT_FIELD_MARKERS = ("card", "pan", "cvv", "cvc", "expiry", "exp_date", "expiration")
_REDACTED = "[REDACTED]"
_STANDARD_LOG_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__.keys())

_PHONE_RE = re.compile(r"(?<!\d)(\+?\d[\d().\-\s]{6,}\d)(?!\d)")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+([a-z0-9._~+/=-]{8,})")
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?([^\s,;]+)")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|signature|password)\b(\s*[:=]\s*)(['\"]?)([^'\"\s,;]+)\3"
)
_DSN_RE = re.compile(
    r"(?i)(?P<scheme>(?:postgres(?:ql)?|mysql|mariadb|redis|rediss|amqp|mongodb(?:\+srv)?)://)(?P<user>[^:\s/@]+):(?P<password>[^@\s/]+)@"
)
_CARD_RE = re.compile(r"(?<!\d)((?:\d[ -]?){13,19})(?!\d)")


def _is_tagged(field_name: str, markers: Collection[str]) -> bool:
    normalized = str(field_name or "").strip().lower()
    return bool(normalized) and any(marker in normalized for marker in markers)


def _mask_phone(phone: str) -> str:
    stripped = str(phone or "").strip()
    digits = re.sub(r"\D", "", stripped)
    if len(digits) < 7:
        return stripped or _REDACTED
    prefix = digits[:4]
    suffix = digits[-2:]
    lead = "+" if stripped.startswith("+") else ""
    return f"{lead}{prefix}****{suffix}"


def _passes_luhn(number: str) -> bool:
    digits = [int(char) for char in number if char.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _mask_card(candidate: str) -> str:
    digits = re.sub(r"\D", "", candidate)
    if len(digits) < 13:
        return candidate
    return f"{digits[:6]}******{digits[-4:]}"


def _scrub_card_matches(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        candidate = match.group(1)
        return _mask_card(candidate) if _passes_luhn(candidate) else candidate

    return _CARD_RE.sub(replace, text)


def _scrub_phone_matches(text: str) -> str:
    return _PHONE_RE.sub(lambda match: _mask_phone(match.group(1)), text)


def _scrub_dsn(text: str) -> str:
    return _DSN_RE.sub(lambda match: f"{match.group('scheme')}{match.group('user')}:****@", text)


def scrub_text(text: str, *, field_name: str | None = None) -> str:
    """Scrub sensitive values embedded in text."""
    if text is None:
        return ""

    field = str(field_name or "")
    if _is_tagged(field, _DEFAULT_SECRET_MARKERS):
        return _REDACTED
    if _is_tagged(field, _PAYMENT_FIELD_MARKERS):
        masked = _scrub_card_matches(str(text))
        return masked if masked != str(text) else _REDACTED
    if _is_tagged(field, _PHONE_FIELD_MARKERS):
        return _mask_phone(str(text))

    scrubbed = str(text)
    scrubbed = _scrub_dsn(scrubbed)
    scrubbed = _AUTH_HEADER_RE.sub(lambda match: f"{match.group(1)}{_REDACTED}", scrubbed)
    scrubbed = _BEARER_RE.sub("Bearer [REDACTED]", scrubbed)
    scrubbed = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{_REDACTED}", scrubbed)
    scrubbed = _scrub_card_matches(scrubbed)
    scrubbed = _scrub_phone_matches(scrubbed)
    return scrubbed


def scrub_value(value: Any, *, field_name: str | None = None) -> Any:
    """Recursively scrub sensitive values in common logging payload types."""
    if isinstance(value, Mapping):
        return {str(key): scrub_value(item, field_name=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_value(item, field_name=field_name) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_value(item, field_name=field_name) for item in value)
    if isinstance(value, str):
        return scrub_text(value, field_name=field_name)
    return value


def scrub_payload_for_logging(
    payload: Mapping[str, Any] | None,
    *,
    allowlist: Collection[str] | None = None,
    secret_markers: Collection[str] | None = None,
) -> dict[str, Any]:
    """Keep only allowlisted keys and recursively scrub sensitive values."""
    if not isinstance(payload, Mapping):
        return {}

    markers = tuple(secret_markers or _DEFAULT_SECRET_MARKERS)
    allowed = {str(key).strip().lower() for key in (allowlist or payload.keys())}
    scrubbed: dict[str, Any] = {}
    for key, value in payload.items():
        key_name = str(key)
        if allowed and key_name.strip().lower() not in allowed:
            continue
        if _is_tagged(key_name, markers):
            scrubbed[key_name] = _REDACTED
            continue
        scrubbed[key_name] = scrub_value(value, field_name=key_name)
    return scrubbed


class SensitiveDataFilter(logging.Filter):
    """Logging filter that scrubs sensitive data from records in place."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub_text(record.msg)
        if record.args:
            record.args = scrub_value(record.args)
        for key, value in list(record.__dict__.items()):
            if key in _STANDARD_LOG_RECORD_ATTRS or key in {"msg", "args"}:
                continue
            record.__dict__[key] = scrub_value(value, field_name=key)
        return True


__all__ = [
    "SensitiveDataFilter",
    "scrub_payload_for_logging",
    "scrub_text",
    "scrub_value",
]
