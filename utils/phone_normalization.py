"""Phone normalization helpers for matching AU mobile numbers."""

from __future__ import annotations

import re


_PHONE_TOKEN_RE = re.compile(r"(?:\+?\s*61|0)?\s*4(?:[\s\-]?\d){8}")


def normalize_au_mobile(value: object) -> str:
    """
    Normalize common Australian mobile formats to canonical digits: 61XXXXXXXXX.

    Returns empty string when the value cannot be parsed as an AU mobile number.
    """
    if value is None:
        return ""

    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return ""

    if digits.startswith("001161"):
        digits = digits[4:]
    elif digits.startswith("01161"):
        digits = digits[3:]

    if len(digits) == 11 and digits.startswith("61") and digits[2] == "4":
        return digits
    if len(digits) == 10 and digits.startswith("0") and digits[1] == "4":
        return "61" + digits[1:]
    if len(digits) == 9 and digits.startswith("4"):
        return "61" + digits
    return ""


def extract_normalized_au_mobile(value: object) -> str:
    """
    Extract and normalize an AU mobile from a free-form value.

    First tries whole-value normalization, then token extraction for messy cells.
    """
    normalized = normalize_au_mobile(value)
    if normalized:
        return normalized

    text = str(value or "")
    for match in _PHONE_TOKEN_RE.findall(text):
        normalized = normalize_au_mobile(match)
        if normalized:
            return normalized
    return ""

