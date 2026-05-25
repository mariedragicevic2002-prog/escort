"""Unified inbound SMS confirmation detection (golden rule: plain YES / OK / confirm, etc.)."""

from __future__ import annotations

import re

# Tokens matched as discrete alphanumeric words in the message (see is_confirmation_token).
CONFIRMATION_WORD_TOKENS = frozenset({
    "yes", "yep", "yeah", "y", "ya", "yee", "yss", "yaa", "ok", "okay", "confirm",
    "confirmation",
    "yup", "sure", "correct", "confirmed", "absolutely", "definitely",
})

# Experience / service keywords skipped when scanning for a bare client name next to YES.
EXPERIENCE_SKIP_WORDS = frozenset({
    "gfe", "pse", "bse", "msog", "cim", "cof", "bbj", "daty", "69", "bbbj", "anal", "owo",
    "bisexual", "heterosexual", "humiliation", "voyeurism",
})

# YES-like tokens skipped during name extraction (same as confirmation + experience).
NAME_SCAN_SKIP_WORDS = CONFIRMATION_WORD_TOKENS | EXPERIENCE_SKIP_WORDS


def is_confirmation_token(message: str | None) -> bool:
    """True if the client message contains a booking confirmation token as its own word/token."""
    if not message or not str(message).strip():
        return False
    tokens = re.findall(r"[a-zA-Z0-9]+", message.lower())
    return bool(CONFIRMATION_WORD_TOKENS.intersection(tokens))
