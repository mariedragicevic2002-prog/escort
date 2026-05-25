"""MMF doubles — escort-sourced male exploration preferences (SMS + webform + calendar)."""

from __future__ import annotations

import json
import re
from typing import Any

# Canonical slugs stored in DB / JSON
MMF_EXPLORATION_SLUGS = ("humiliation", "voyeurism", "bisexual", "heterosexual")

TAG_LABELS: dict[str, str] = {
    "humiliation": "Humiliation",
    "voyeurism": "Voyeurism",
    "bisexual": "Bisexual",
    "heterosexual": "Heterosexual",
}


def _norm_booking_token(s: Any) -> str:
    """Lowercase + collapse spaces/dashes for robust comparisons (SMS/collector vs DB)."""
    return str(s or "").strip().lower().replace("-", "_").replace(" ", "_")


def _is_doubles_mmf_merged(merged: dict[str, Any]) -> bool:
    bt = _norm_booking_token(merged.get("booking_type"))
    exp = _norm_booking_token(merged.get("experience_type"))
    dt = str(merged.get("doubles_type") or "").strip().lower()

    # Explicit MFF paths — never treat as MMF.
    if bt == "doubles_mff" or exp in ("doubles_mff", "couples_mff"):
        return False

    doubles_signal = (
        bt in ("doubles_mmf", "doubles_mff")
        or "doubles" in exp
        or dt in ("mmf", "mff")
    )
    if not doubles_signal:
        return False

    return (
        bt == "doubles_mmf"
        or dt == "mmf"
        or "doubles_mmf" in exp
        or exp.replace("_", "") == "doublesmmf"
        # "doubles mmf" normalizes to Doubles MMF; "doubles mff" → doubles_mff (filtered above).
        or ("mmf" in exp and "mff" not in exp)
    )


def escort_organises_male_for_mmf(merged: dict[str, Any]) -> bool:
    if not _is_doubles_mmf_merged(merged):
        return False
    src = str(merged.get("escort_supply_source") or "").strip().lower()
    st = str(merged.get("booking_status") or "").strip().lower()
    return src == "escort" or st == "doubles_supply_escort"


def schedule_should_show_mmf_preferences(details: dict[str, Any] | None) -> bool:
    """Schedule UI / JSON: MMF exploration preferences only when escort sources the male — never dinner dates."""
    if not details:
        return False
    from utils.dinner_date import is_dinner_date_booking

    exp = str(details.get("experience") or "").strip()
    bt = str(details.get("booking_type") or "").strip()
    if is_dinner_date_booking({"booking_type": bt, "experience_type": exp}):
        return False

    org = str(details.get("organise_other_escort") or "").strip().lower()
    if org not in ("yes", "y", "true", "1", "escort"):
        return False

    merged = {
        "booking_type": bt,
        "experience_type": exp,
        "doubles_type": "",
        "escort_supply_source": "escort",
        "booking_status": "doubles_supply_escort",
    }
    return escort_organises_male_for_mmf(merged)


def scrub_schedule_mmf_preferences(details: dict[str, Any] | None) -> None:
    """Remove MMF preference text when the calendar row is not escort-sourced Doubles MMF (fixes stale phone-merge)."""
    if not details:
        return
    if schedule_should_show_mmf_preferences(details):
        return
    details["preferences"] = ""


def decode_mmf_exploration_tags(raw: Any) -> list[str]:
    """Return unique canonical slugs from DB JSON string or list."""
    if raw is None:
        return []
    if isinstance(raw, list):
        candidates = raw
    else:
        s = str(raw).strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            candidates = parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    out: list[str] = []
    for item in candidates:
        slug = str(item).strip().lower()
        if slug in MMF_EXPLORATION_SLUGS and slug not in out:
            out.append(slug)
    return out


def encode_mmf_exploration_tags(tags: list[str]) -> str:
    norm = decode_mmf_exploration_tags(tags)
    return json.dumps(norm)


def humanize_mmf_exploration_tags(tags: list[str]) -> str:
    labels = [TAG_LABELS[t] for t in decode_mmf_exploration_tags(tags)]
    return ", ".join(labels)


def format_mmf_exploration_calendar_line(tags: Any) -> str:
    """Single-line marker parsed into schedule ``preferences`` (Preferences section in UI)."""
    labels = humanize_mmf_exploration_tags(decode_mmf_exploration_tags(tags))
    return f"MMF Exploration: {labels}" if labels else ""


def parse_mmf_exploration_reply(message: str) -> list[str]:
    """Infer exploration tags from free-text SMS (multi-select)."""
    if not message or not str(message).strip():
        return []

    low = message.lower()
    found: list[str] = []

    def add(slug: str) -> None:
        if slug not in found:
            found.append(slug)

    if re.search(r"\bhumiliat", low):
        add("humiliation")
    if re.search(r"\bvoyeur|\bwatch\b.*\bfucked\b|\bbull\b", low):
        add("voyeurism")
    if re.search(r"\bbi\s*sexual\b|\bbisexual\b", low):
        add("bisexual")
    elif re.search(r"(?<![a-z])bi\b", low):
        add("bisexual")
    if re.search(r"\bhetero|\bstraight\b", low):
        add("heterosexual")

    # Digits 1–4 only when the reply is clearly a compact choice list (e.g. "1 3", "2&4").
    # Otherwise times/dates like "3pm" or "Jan 15" wrongly trigger bisexual / humiliation.
    digit_to_slug = {
        "1": "humiliation",
        "2": "voyeurism",
        "3": "bisexual",
        "4": "heterosexual",
    }
    compact = re.sub(r"[\s,/+&]+", "", low)
    if re.fullmatch(r"[1-4]+", compact):
        for ch in compact:
            slug = digit_to_slug.get(ch)
            if slug:
                add(slug)

    return found


def mmf_exploration_sms_prompt(booking_fields: dict[str, Any] | None = None) -> str:
    """
    Exploration checklist SMS (golden rule body from utils.golden_booking_rules).
    Outcall travel surcharge line is appended only for outcall bookings.
    """
    from utils.golden_booking_rules import GOLDEN_MMF_ESCORT_SOURCED_EXPLORATION_PROMPT

    base = GOLDEN_MMF_ESCORT_SOURCED_EXPLORATION_PROMPT
    oc = str((booking_fields or {}).get("incall_outcall") or "").strip().lower()
    if oc != "outcall":
        return base
    from core.rates_from_config import format_doubles_escort_arranges_second_outcall_travel_notice

    pair_line = format_doubles_escort_arranges_second_outcall_travel_notice()
    return f"{base}\n\n{pair_line}"


def mmf_exploration_followup_prompt() -> str:
    return (
        "I still need which MMF options apply — Humiliation, Voyeurism, "
        "Bisexual, and/or Heterosexual (reply with words, or numbers only e.g. 24 for 2+4)."
    )


def mmf_exploration_block_yes_prompt() -> str:
    return (
        "Before you confirm the booking, please send which MMF exploration options apply "
        "(Humiliation, Voyeurism, Bisexual, Heterosexual — you can pick more than one)."
    )


def should_append_mmf_exploration_to_calendar(booking_details: dict[str, Any]) -> bool:
    if not booking_details:
        return False
    tags = decode_mmf_exploration_tags(booking_details.get("mmf_exploration_tags"))
    if not tags:
        return False
    return escort_organises_male_for_mmf(booking_details)
