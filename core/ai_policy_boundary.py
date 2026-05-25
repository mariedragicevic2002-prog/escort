"""
Centralized AI decision-boundary guardrails.

AI is allowed to interpret and phrase messages, but never to decide core business
policy (rates, deposits, availability, eligibility, safety outcomes).
"""

from __future__ import annotations

import re

from utils.structured_logging import log_quality_metric

AI_DECISION_BOUNDARY_PROMPT = (
    "AI role boundary: You are wording/interpretation support only. "
    "Do not invent or decide availability, rates, deposit requirements, eligibility, or safety actions. "
    "Use neutral language and defer those outcomes to booking policy checks."
)

_PRICE_CLAIM_RE = re.compile(
    r"(\$\s*\d|\b\d+\s*(?:/|per)\s*(?:hr|hour)\b|\brate(?:s)?\s+start(?:ing)?\s+at\b)",
    re.IGNORECASE,
)
_DEPOSIT_WAIVER_RE = re.compile(
    r"\b("
    r"no\s+deposit(?:\s+needed)?|"
    r"deposit\s+(?:is\s+)?(?:not|isn't)\s+required|"
    r"don't\s+need\s+(?:a\s+)?deposit|"
    r"cash\s+is\s+(?:fine|perfect)|"
    r"no\s+worries[^.?!]{0,40}\bdeposit\b"
    r")\b",
    re.IGNORECASE,
)
_DEPOSIT_ABSOLUTE_RE = re.compile(
    r"\b("
    r"deposit\s+(?:is\s+)?required(?:\s+to\s+secure)?\s+(?:for|on)?\s*all\s+bookings|"
    r"all\s+bookings[^.?!]{0,30}\bdeposit\s+(?:is\s+)?required|"
    r"deposit\s+required\s+for\s+all|"
    r"no\s+exceptions|"
    r"always\s+required"
    r")\b",
    re.IGNORECASE,
)
_SERVICE_DENIAL_RE = re.compile(
    r"\b("
    r"not\s+something\s+i\s+offer|"
    r"do\s+not\s+offer|"
    r"don't\s+offer|"
    r"can't\s+do|"
    r"cannot\s+do|"
    r"not\s+available"
    r")\b",
    re.IGNORECASE,
)

_REQUIRED_DEPOSIT_MESSAGE_KWS = (
    "overnight",
    "weekend",
    "filming",
    "fly me",
    "fly-to-you",
    "fly to",
    "interstate",
)
_SERVICE_MESSAGE_KWS = (
    "filming",
    "overnight",
    "weekend",
    "fly",
    "outcall",
    "incall",
    "doubles",
    "threesome",
    "gangbang",
    "couples",
)
_DEPOSIT_MESSAGE_KWS = ("deposit", "cash", "payid", "transfer", "payment")


def _normalize_sms(text: str, limit: int = 320) -> str:
    out = (text or "").strip()
    if len(out) <= limit:
        return out
    return out[: limit - 3].rsplit(" ", 1)[0] + "..."


def _has_any_keyword(message: str, keywords: tuple[str, ...]) -> bool:
    lower = (message or "").lower()
    return any(k in lower for k in keywords)


def _rewrite_rate_reply() -> str:
    return (
        "Rates are set by booking policy. "
        "Please share your date, time and booking type so I can confirm the right option."
    )


def apply_ai_decision_policy_guard(
    *,
    message: str,
    reply: str,
    confirmed_context: bool = False,
) -> str:
    """
    Keep AI replies policy-safe and aligned to deterministic business rules.
    """
    user_msg = (message or "").strip()
    safe = (reply or "").strip()
    if not safe:
        return safe
    safe_lower = safe.lower()

    if _PRICE_CLAIM_RE.search(safe):
        log_quality_metric("fallback_policy_rewrite", rule="price_claim")
        safe = _rewrite_rate_reply()
        safe_lower = safe.lower()

    asks_required_deposit_service = _has_any_keyword(user_msg, _REQUIRED_DEPOSIT_MESSAGE_KWS)
    if asks_required_deposit_service and "deposit" not in safe_lower:
        log_quality_metric("fallback_policy_rewrite", rule="required_deposit_missing")
        safe = (
            "For overnight, weekend, filming, and fly-me-to-you bookings, a deposit is required before confirmation. "
            "Share your date and time and I will guide the next step."
        )
        safe_lower = safe.lower()

    if _DEPOSIT_ABSOLUTE_RE.search(safe_lower):
        log_quality_metric("fallback_policy_rewrite", rule="deposit_blanket_claim")
        if asks_required_deposit_service:
            safe = (
                "For overnight, weekend, filming, and fly-me-to-you bookings, a deposit is required before confirmation. "
                "Share your date and time and I will guide the next step."
            )
        elif confirmed_context:
            safe = (
                "Thanks for letting me know. Payment and deposit requirements depend on your booking status, "
                "so I will confirm the exact next step for your booking."
            )
        else:
            safe = (
                "Deposit requirements depend on booking type and status. "
                "Share your booking details and I will confirm whether a deposit is required."
            )
        safe_lower = safe.lower()

    if _has_any_keyword(user_msg, _DEPOSIT_MESSAGE_KWS) and _DEPOSIT_WAIVER_RE.search(safe):
        log_quality_metric("fallback_policy_rewrite", rule="deposit_waiver")
        if confirmed_context:
            safe = (
                "Thanks for letting me know. Payment and deposit requirements depend on your booking status, "
                "so I will confirm the exact next step for your booking."
            )
        else:
            safe = (
                "Deposit requirements depend on booking type and status. "
                "I cannot waive deposit rules here, but I can confirm the correct next step once you share booking details."
            )
        safe_lower = safe.lower()

    if _has_any_keyword(user_msg, _SERVICE_MESSAGE_KWS) and _SERVICE_DENIAL_RE.search(safe_lower):
        log_quality_metric("fallback_policy_rewrite", rule="service_denial")
        safe = (
            "I can help with that booking request. "
            "Please share your preferred date, time, duration, and whether you want incall or outcall."
        )

    return _normalize_sms(safe, 320)
