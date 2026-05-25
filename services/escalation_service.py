"""
Deterministic escalation rules for manual review routing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from utils.structured_logging import log_quality_metric

_EMOTIONAL_ESCALATION_KWS = (
    "scam",
    "lawyer",
    "police",
    "report you",
    "angry",
    "furious",
    "upset",
    "threat",
    "unsafe",
    "refund now",
)

# Cooldown window: suppress re-escalation for the same client within this duration.
_ESCALATION_COOLDOWN_HOURS = 1


def evaluate_escalation(
    *,
    message: str,
    intent: str,
    current_state: dict[str, Any],
    client_context: dict[str, Any],
) -> dict[str, Any]:
    text = (message or "").strip().lower()
    total_bookings = int(client_context.get("total_bookings") or 0)
    profanity_count = int((current_state or {}).get("profanity_count") or 0)

    # Dedup: skip re-escalation if triggered recently (within cooldown window).
    _last_esc_str = (current_state or {}).get("escalation_triggered_at")
    if _last_esc_str:
        try:
            _last_esc = datetime.fromisoformat(str(_last_esc_str))
            if _last_esc.tzinfo is None:
                _last_esc = _last_esc.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - _last_esc < timedelta(hours=_ESCALATION_COOLDOWN_HOURS):
                return {"triggered": False, "tags": [], "reasons": [], "cooldown_active": True}
        except Exception:
            pass  # Malformed timestamp — proceed with normal evaluation

    tags: list[str] = []
    reasons: list[str] = []

    if intent in ("unsafe_request", "rude_abusive"):
        tags.append("escalate_manual_review")
        reasons.append("unsafe_or_abusive_intent")

    if profanity_count >= 3:
        if "escalate_manual_review" not in tags:
            tags.append("escalate_manual_review")
        reasons.append("high_profanity_count")

    if any(k in text for k in _EMOTIONAL_ESCALATION_KWS):
        if "escalate_manual_review" not in tags:
            tags.append("escalate_manual_review")
        reasons.append("emotional_or_legal_escalation")

    if total_bookings >= 10:
        tags.append("escalate_vip_context")
        reasons.append("vip_client")

    triggered = bool(tags)
    if triggered:
        log_quality_metric(
            "escalation_triggered",
            intent=intent,
            reasons=",".join(reasons),
            tags=",".join(tags),
            total_bookings=total_bookings,
        )
    return {
        "triggered": triggered,
        "tags": tags,
        "reasons": reasons,
    }
