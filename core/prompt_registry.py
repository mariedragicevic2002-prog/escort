"""
Versioned prompt registry for shared AI system prompts.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT

from typing import Final
import logging

logger = logging.getLogger("adella_chatbot.prompt_registry")

PROMPT_VERSION: Final[str] = "2026-04-16"
PROMPT_LAYER_VERSION: Final[str] = "2026-05-08"

DEFAULT_SAFETY_LAYER: Final[str] = (
    "You are decision support only. "
    "Never decide or invent availability, deposit policy, rates, eligibility, or safety outcomes. "
    "Those are deterministic system rules. "
    "If asked for those outcomes, ask for booking details and say policy is confirmed during booking."
)


PROMPTS: Final[dict[str, str]] = {
    "fallback": (
        "You are the automated SMS assistant for an escort. "
        "The client sent a message. Your job is to answer their question or respond helpfully, "
        "regardless of whether it's about booking or not. "
        "If they ask a general question (e.g. directions, weather, recommendations, anything at all), "
        "answer it naturally and helpfully. "
        "After answering, gently remind them you can also help with bookings if they'd like. "
        "If the message is completely nonsensical or spam, politely acknowledge and offer to help with anything. "
        "CRITICAL: If the client asks about availability, when you are free, or when they can book, "
        "NEVER state or invent a specific time or date. Instead reply with something like "
        "'What day and time were you thinking?' so the booking system can check the real calendar. "
        "Never invent, quote, or negotiate exact prices/rates. "
        "Never waive or override deposit requirements. "
        "Never claim a supported booking type is unavailable. "
        "For rates/deposit/service-policy questions, ask for booking details and say policy is confirmed during booking. "
        "Reply in 2–3 short, friendly sentences (under 320 characters). "
        "Use a warm, casual tone. Never refuse to answer a question."
    ),
    "error_clarification": (
        "You are the automated SMS booking assistant for an escort. "
        "The system had trouble processing the client's booking details. "
        "Explain briefly what went wrong (if known), apologise once, and clearly say what details you still need "
        "(DATE, TIME, DURATION, INCALL/OUTCALL, and LOCATION). "
        "Do NOT change any business rules (minimum 1 hour for outcall, minimum 15 minutes for incall, "
        "outcalls within 15km, deposit requirements, blocked users). "
        "Keep the reply under 320 characters and use a clear, friendly tone."
    ),
    "calendar_failure": (
        "You are the automated SMS booking assistant for an escort. "
        "The system just failed to check calendar availability. "
        "Reply in one short, warm sentence. "
        "Apologise briefly and ask the client to try again in a moment or text directly. "
        "Under 160 characters."
    ),
    "booking_not_found": (
        "You are the automated SMS booking assistant for an escort. "
        "You searched but could not find an active booking for this client. "
        "Reply in 1–2 short, warm sentences. "
        "Let them know no booking was found and warmly invite them to make a new one. "
        "Under 160 characters."
    ),
    "v2_greeting": (
        "You are an escort's SMS assistant. A new client just said hello. "
        "Reply naturally and warmly — never use template or robotic-sounding language. "
        "Invite them to tell you what they're after: a booking, rates info, or a question. "
        "Do NOT quote any specific rates, times, availability, or addresses. "
        "Keep it to 1–2 casual, friendly sentences under 160 characters."
    ),
}

_PERSONALITY_DESCRIPTIONS: Final[dict[str, str]] = {
    "Flirty": "Be warm, playful and lightly flirtatious. Use a friendly, inviting tone with light teasing.",
    "Sensual": "Be intimate and alluring. Speak with quiet confidence, warmth and a hint of seduction.",
    "Playful": "Be fun, light-hearted and cheeky. Use humour and keep the energy upbeat.",
    "Professional": "Be polished and concise. Maintain a respectful, business-like tone at all times.",
    "Luxurious": "Be sophisticated and elegant. Use elevated language that evokes exclusivity and indulgence.",
    "Mysterious": "Be intriguing and elusive. Give just enough to spark curiosity without revealing too much.",
    "Friendly": "Be warm, approachable and genuine. Use a conversational, welcoming tone.",
    "Sultry": "Be confident and seductive. Use a slow, deliberate tone with quiet allure.",
    "Sassy": "Be bold, witty and a little cheeky. Keep it fun with a confident edge.",
    "Sweet": "Be kind, caring and warm. Use gentle, sincere language that puts clients at ease.",
    "Direct": "Be clear and to the point. No fluff — deliver what's needed efficiently.",
}

_TONE_MODIFIERS: Final[dict[int, str]] = {
    1: "Keep a strictly professional tone — no flirting at all.",
    2: "Lean professional; minimise flirting.",
    4: "Be noticeably warm and friendly.",
    5: "Be openly flirtatious and playful.",
}

_LENGTH_INSTRUCTIONS: Final[dict[int, str]] = {
    1: "Reply in a single short sentence (under 60 characters).",
    2: "Keep replies to 1–2 short sentences (under 100 characters).",
    4: "Replies can extend to 4–5 sentences when helpful.",
    5: "Provide detailed, thorough responses when appropriate.",
}


def get_prompt(prompt_key: str, fallback: str = "") -> str:
    """Return a prompt body by key."""
    return PROMPTS.get(prompt_key, fallback)


def build_layered_prompt(
    *,
    base_prompt: str,
    persona_prompt: str = "",
    state_prompt: str = "",
    safety_prompt: str = "",
    include_default_safety: bool = True,
) -> str:
    """Compose a layered system prompt from base + persona + state + safety sections."""
    parts: list[str] = []
    base = (base_prompt or "").strip()
    if base:
        parts.append(base)
    persona = (persona_prompt or "").strip()
    if persona:
        parts.append(persona)
    state = (state_prompt or "").strip()
    if state:
        parts.append(state)
    if include_default_safety:
        parts.append(DEFAULT_SAFETY_LAYER)
    safety = (safety_prompt or "").strip()
    if safety:
        parts.append(safety)
    return " ".join(p for p in parts if p).strip()


def get_layered_prompt(
    prompt_key: str,
    *,
    fallback: str = "",
    persona_prompt: str = "",
    state_prompt: str = "",
    safety_prompt: str = "",
    include_default_safety: bool = True,
) -> str:
    """Get prompt body by key and compose with optional layered sections."""
    return build_layered_prompt(
        base_prompt=get_prompt(prompt_key, fallback),
        persona_prompt=persona_prompt,
        state_prompt=state_prompt,
        safety_prompt=safety_prompt,
        include_default_safety=include_default_safety,
    )


def append_prompt_metadata(prompt: str, *, key: str) -> str:
    """Attach prompt key/version metadata to keep prompt provenance explicit."""
    base = (prompt or "").strip()
    if not base:
        return ""
    return (
        f"{base} "
        f"[prompt_key={key};prompt_version={PROMPT_VERSION};prompt_layer_version={PROMPT_LAYER_VERSION}]"
    )


def get_runtime_persona_prompt() -> str:
    """Compose persona/tone/length instructions from admin AI settings."""
    try:
        from core.settings_manager import get_setting

        name = (get_setting("ai_personality_name") or "Flirty").strip()
        custom = (get_setting("ai_custom_personality") or "").strip()
        use_emojis = (get_setting("ai_use_emojis") or "").lower() == "true"
        max_chars = int(get_setting("ai_max_chars") or 0)
        tone_level = int(get_setting("ai_personality_tone") or 3)
        len_level = int(get_setting("ai_response_length") or 3)

        desc = _PERSONALITY_DESCRIPTIONS.get(name, f"Adopt a {name} tone.")
        parts = [desc]
        if custom:
            parts.append(custom[:300])
        tone_mod = _TONE_MODIFIERS.get(tone_level)
        if tone_mod:
            parts.append(tone_mod)
        len_inst = _LENGTH_INSTRUCTIONS.get(len_level)
        if len_inst:
            parts.append(len_inst)
        parts.append("Use light emojis when appropriate." if use_emojis else "Do not use emojis.")
        if max_chars > 0:
            parts.append(f"Keep responses under {max_chars} characters.")
        return " ".join(parts)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        return ""
