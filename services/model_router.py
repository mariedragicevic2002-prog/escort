"""
Model router — cost-aware inference routing.

Classifies message complexity and routes to the appropriate model tier.
Trivial messages use cheap/fast models; complex ones use premium models.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger("adella_chatbot.model_router")

_DEFAULT_SHADOW_MODE = True  # Overridden by MODEL_ROUTING_SHADOW env var

_TRIVIAL_WORDS = {"yes", "no", "ok", "sure", "maybe", "nope", "yep", "yeah", "nah", "hello", "hi"}
_COMPLEX_SAFETY_WORDS = {"hurt", "unsafe", "force", "scared", "help"}


class MessageComplexity:
    TRIVIAL = "trivial"
    STANDARD = "standard"
    COMPLEX = "complex"


def classify_complexity(message: str, state: str | None = None, history: list | None = None) -> str:
    """Returns MessageComplexity value."""
    _ = history
    text = (message or "").strip()
    lowered = text.lower()
    state_text = (state or "").strip().lower()

    if state_text in {"blocked", "escalation"}:
        return MessageComplexity.COMPLEX
    if any(word in lowered for word in _COMPLEX_SAFETY_WORDS):
        return MessageComplexity.COMPLEX
    if len(text) > 300 and sum(ch in "!?.," for ch in text) >= 3:
        return MessageComplexity.COMPLEX
    alpha = "".join(ch for ch in text if ch.isalpha())
    if alpha and alpha.isupper() and (text.count("!") + text.count("?") >= 2):
        return MessageComplexity.COMPLEX

    if lowered in _TRIVIAL_WORDS:
        return MessageComplexity.TRIVIAL
    if re.fullmatch(r"\d+(?::\d{2})?", text):
        return MessageComplexity.TRIVIAL
    if len(text) <= 14 and re.fullmatch(r"(?:ok(?:ay)?|yes|no|sure|yep|yeah|nah|nope|hello|hi|thanks?)", lowered):
        return MessageComplexity.TRIVIAL
    if len(text.split()) == 1 and lowered.isalpha():
        return MessageComplexity.TRIVIAL if lowered in _TRIVIAL_WORDS else MessageComplexity.STANDARD

    return MessageComplexity.STANDARD


def get_routed_provider(complexity: str, configured_provider: str) -> tuple[str, str]:
    """Returns (provider, model_override). model_override may be empty string if using provider default."""
    provider = (configured_provider or "claude").strip() or "claude"
    if complexity == MessageComplexity.TRIVIAL:
        return ("gemini", "gemini-2.5-flash")
    if complexity == MessageComplexity.COMPLEX:
        return ("claude", "claude-sonnet-4-6")
    return (provider, "")


class ModelRouter:
    def __init__(self, *, shadow_mode: bool = _DEFAULT_SHADOW_MODE):
        env_value = (os.environ.get("MODEL_ROUTING_SHADOW") or "").strip().lower()
        self.shadow_mode = False if env_value == "false" else bool(shadow_mode)

    def route(
        self,
        message: str,
        state: str | None = None,
        history: list | None = None,
        configured_provider: str = "claude",
    ) -> tuple[str, str]:
        complexity = classify_complexity(message, state=state, history=history)
        would_use = get_routed_provider(complexity, configured_provider)
        actually_used = (configured_provider, "") if self.shadow_mode else would_use
        self.log_routing_decision(complexity, would_use, actually_used)
        return actually_used

    def log_routing_decision(self, complexity: str, would_use: tuple, actually_used: tuple) -> None:
        try:
            logger.info(
                "model routing complexity=%s shadow=%s would_use=%s actual=%s",
                complexity,
                self.shadow_mode,
                would_use,
                actually_used,
            )
        except Exception:
            pass
