from __future__ import annotations


def normalize_intent(value: str | None) -> str:
    """Normalize an intent string: strip whitespace and lowercase."""
    return (value or "").strip().lower()
