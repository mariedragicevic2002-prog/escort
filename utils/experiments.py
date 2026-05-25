"""
Deterministic A/B assignment helpers.
"""

from hashlib import md5


def assign_variant(subject_id: str, experiment: str, variants: tuple[str, ...]) -> str:
    """Deterministically assign a variant from a stable subject id."""
    safe_subject = (subject_id or "unknown").strip().lower()
    safe_experiment = (experiment or "default").strip().lower()
    key = f"{safe_experiment}:{safe_subject}".encode("utf-8")
    bucket = int(md5(key, usedforsecurity=False).hexdigest()[:8], 16)
    return variants[bucket % len(variants)]


def first_contact_variant(phone_number: str) -> str:
    return assign_variant(phone_number, "first_contact_copy", ("control", "warmer"))


def deposit_followup_variant(phone_number: str) -> str:
    return assign_variant(phone_number, "deposit_followup_copy", ("control", "reassuring"))
