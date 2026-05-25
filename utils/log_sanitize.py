"""Strip control characters from values before logging (mitigates log forging / S5145)."""

from __future__ import annotations

import re

# Sonar python:S1192 — single definition for the common exception-swallow log line
LOG_SUPPRESSED_FMT = "Suppressed error: %s"

_CTRL_OR_MULTISPACE = re.compile(r"[\x00-\x1f]+")


def sanitize_log_value(value: object, max_len: int = 240) -> str:
    """Return a single-line, length-limited string safe to embed in log records."""
    if value is None:
        return ""
    s = str(value)
    s = _CTRL_OR_MULTISPACE.sub(" ", s).strip()
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s
