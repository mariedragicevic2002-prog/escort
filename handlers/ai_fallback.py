"""

AI fallback handler – when the client says something the bot doesn't understand,
use AI to reply (e.g. off-topic, wrong number) or fall back to the enquiry prompt.
"""

import logging
import re
from typing import Any

from utils.log_sanitize import LOG_SUPPRESSED_FMT

from core.ai_policy_boundary import AI_DECISION_BOUNDARY_PROMPT, apply_ai_decision_policy_guard
from core.client_profile import profile_to_prompt_snippet
from core.policy_retrieval import get_policy_snapshot, get_rates_summary_snippet
from core.operator_booking_rules import (
    get_runtime_booking_guardrails_prompt,
    get_runtime_booking_regression_prompt,
)
from core.prompt_registry import append_prompt_metadata, get_layered_prompt
from utils.structured_logging import log_quality_metric

logger = logging.getLogger("escort_chatbot.handlers.ai_fallback")

_RATES_QUERY_RE = re.compile(r"\b(rate|rates|price|pricing|cost|how much)\b|\$", re.IGNORECASE)
_DEPOSIT_QUERY_RE = re.compile(r"\b(deposit|cash|payid|payment|transfer)\b", re.IGNORECASE)
_OUTCALL_QUERY_RE = re.compile(r"\b(outcall|travel surcharge|surcharge|within 15km)\b", re.IGNORECASE)
_NOISY_TEXT_RE = re.compile(r"^[^a-zA-Z0-9]{4,}$")

_FALLBACK_STEP_BY_STATE = {
    "NEW": "qualification",
    "COLLECTING": "availability",
    "COLLECTING_BOOKING_FIELDS": "availability",
    "CHECKING_AVAILABILITY": "availability",
    "EXTENDED_ENQUIRY": "screening",
    "MANUAL_REVIEW_PENDING": "screening",
    "DEPOSIT_REQUIRED": "deposit",
    "CONFIRMED": "confirmation",
    "POST_BOOKING": "follow_up",
}
_FALLBACK_STEP_KEYS = {
    "qualification": "ai_fallback_confidence_threshold_qualification",
    "availability": "ai_fallback_confidence_threshold_availability",
    "screening": "ai_fallback_confidence_threshold_screening",
    "deposit": "ai_fallback_confidence_threshold_deposit",
    "confirmation": "ai_fallback_confidence_threshold_confirmation",
    "follow_up": "ai_fallback_confidence_threshold_follow_up",
}


def _build_rates_profile_reply(phone_number: str = "") -> str:
    try:
        from config import get_profile_url

        profile_url = (get_profile_url() or "").strip()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        profile_url = ""

    try:
        from core.webform_security import get_webform_url

        webform_url = (get_webform_url(phone_number) or "").strip()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        webform_url = ""

    reply = (
        "Hi thanks for your enquiry. For a full list of my rates and experiences I offer, "
        "check out my profile below:"
    )
    if profile_url:
        reply += f" {profile_url}"
    reply += " If you would like to make a booking text me back"
    if webform_url:
        reply += f" or instead use my booking webform {webform_url}"
    return reply


def _get_retrieval_first_policy_response(message: str, *, phone_number: str = "") -> str | None:
    """Deterministic policy/rates response before any AI generation."""
    text = (message or "").strip()
    if not text:
        return None

    lower = text.lower()
    if not any(tok in lower for tok in ("rate", "price", "cost", "$", "deposit", "outcall", "surcharge", "payid", "cash", "payment")):
        return None

    policy = get_policy_snapshot()
    if _RATES_QUERY_RE.search(text):
        return _build_rates_profile_reply(phone_number)
    if _DEPOSIT_QUERY_RE.search(text):
        return (
            f"Deposit requirements depend on booking type and status. "
            f"For overnight, weekend, filming, and fly-me-to-you bookings, a ${int(policy.get('outcall_deposit') or 100)} deposit is required before confirmation. "
            "Share your date/time and booking type and I will confirm the exact next step."
        )
    if _OUTCALL_QUERY_RE.search(text):
        return (
            f"Outcalls are within 15km and include a ${int(policy.get('outcall_surcharge') or 100)} travel surcharge. "
            f"Outcall base is ${int(policy.get('outcall_gfe_60') or 800)}/hr. "
            "Share your date/time, duration, and address and I will confirm availability."
        )
    return None


def _apply_policy_guard(
    *,
    message: str,
    reply: str,
    confirmed_context: bool = False,
) -> str:
    return apply_ai_decision_policy_guard(
        message=message,
        reply=reply,
        confirmed_context=confirmed_context,
    )


def _build_client_context_snippet(context: dict[str, Any]) -> str:
    """Return a compact string summarising this client for the AI system prompt."""
    return profile_to_prompt_snippet(context.get("client_profile") or {})


def _build_rates_snippet() -> str:
    """Return a compact rates summary for the AI system prompt."""
    return get_rates_summary_snippet()


def _build_state_prompt_for_templates_first() -> str:
    """Optional runtime state layer based on admin setting ai_templates_first."""
    try:
        from core.settings_manager import get_setting

        templates_first = (get_setting("ai_templates_first") or "").strip().lower() == "true"
        if templates_first:
            return (
                "Template-first mode is enabled. Prefer deterministic booking templates/rules "
                "for policy and booking logistics; use AI for conversational phrasing and clarification."
            )
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
    return ""


def _build_runtime_prompt(prompt_key: str, *, metadata_key: str, extra: str = "") -> str:
    """Build layered prompt at runtime so admin settings changes apply immediately."""
    prompt = get_layered_prompt(
        prompt_key,
        include_default_safety=True,
        state_prompt=_build_state_prompt_for_templates_first(),
    )
    operator_rules = get_runtime_booking_guardrails_prompt()
    if operator_rules:
        prompt = f"{prompt} {operator_rules}".strip()
    regression_rules = get_runtime_booking_regression_prompt()
    if regression_rules:
        prompt = f"{prompt} {regression_rules}".strip()
    if extra:
        prompt = f"{prompt} {extra}".strip()
    return append_prompt_metadata(prompt, key=metadata_key)


def _get_fallback_confidence_threshold() -> float:
    """Runtime threshold for allowing AI fallback responses."""
    try:
        from core.settings_manager import get_setting

        raw = (get_setting("ai_fallback_confidence_threshold") or "").strip()
        if not raw:
            return 0.45
        return max(0.0, min(1.0, float(raw)))
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return 0.45


def _resolve_fallback_step(context: dict[str, Any]) -> str:
    current_state = str((context.get("state") or {}).get("current_state") or "").strip().upper()
    return _FALLBACK_STEP_BY_STATE.get(current_state, "qualification")


def _get_effective_fallback_confidence_threshold(context: dict[str, Any]) -> tuple[float, str]:
    """Return (threshold, step) with per-step override fallback to global threshold."""
    global_threshold = _get_fallback_confidence_threshold()
    step = _resolve_fallback_step(context)
    step_key = _FALLBACK_STEP_KEYS.get(step)
    if not step_key:
        return global_threshold, step
    try:
        from core.settings_manager import get_setting

        raw = (get_setting(step_key) or "").strip()
        if not raw:
            return global_threshold, step
        return max(0.0, min(1.0, float(raw))), step
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return global_threshold, step


def _estimate_fallback_confidence(context: dict[str, Any]) -> float:
    """Heuristic confidence for free-form fallback turns."""
    message = (context.get("message") or "").strip()
    if not message:
        return 0.0
    score = 0.55
    if len(message) < 3:
        score -= 0.35
    elif len(message) < 8:
        score -= 0.15
    if _NOISY_TEXT_RE.match(message):
        score -= 0.30
    if any(ch.isdigit() for ch in message):
        score += 0.08
    if "?" in message:
        score += 0.12
    if any(token in message.lower() for token in ("book", "booking", "time", "available", "rate", "price", "deposit")):
        score += 0.10
    return max(0.0, min(1.0, score))


def _build_fallback_system_prompt(message: str, context: dict[str, Any], name: str) -> str:
    """Build the full system prompt for the AI fallback, including optional context blocks."""
    system = _build_runtime_prompt(
        "fallback",
        metadata_key="fallback",
        extra=f"The business name is {name}.",
    )
    client_snippet = _build_client_context_snippet(context)
    if client_snippet:
        system += f" {client_snippet}"

    semantic_snippets = context.get("semantic_memory_snippets") or []
    if semantic_snippets:
        joined = " | ".join(str(s).strip() for s in semantic_snippets if str(s).strip())
        if joined:
            system += f" Relevant memory snippets: {joined[:400]}"

    rates_snippet = _build_rates_snippet()
    if rates_snippet:
        system += f" Rates info: {rates_snippet}"

    _sched_tokens = ("start", "when", "time", "hour", "available", "open", "busy", "schedule")
    if any(tok in message.lower() for tok in _sched_tokens):
        try:
            from config import get_available_hours
            avail = (get_available_hours() or "").strip()
            if avail:
                system += (
                    f" My available working hours are: {avail}. "
                    "Use this EXACT information if asked about start/end times or availability windows "
                    "— do NOT make up times."
                )
        except Exception:
            pass

    system += f" {AI_DECISION_BOUNDARY_PROMPT}"
    return system


def get_ai_fallback_response(context: dict[str, Any]) -> str | None:
    """
    Call AI to generate a friendly reply for an unclear or off-topic message.
    Returns the reply text, or None if AI is unavailable or errors.
    """
    message = (context.get("message") or "").strip()
    if not message:
        return None
    retrieval_reply = _get_retrieval_first_policy_response(
        message,
        phone_number=(context.get("phone_number") or "").strip(),
    )
    if retrieval_reply:
        log_quality_metric("fallback_retrieval_policy_reply", state=(context.get("state") or {}).get("current_state", "UNKNOWN"))
        return retrieval_reply

    ai_service = context.get("ai_service")
    if not ai_service:
        try:
            from services.ai_service import AIService
            ai_service = AIService()
        except Exception as e:
            logger.warning("AI fallback: no ai_service and AIService() failed: %s", e)
            return None

    try:
        from config import get_escort_name
        name = get_escort_name()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        name = "the escort"

    system = _build_fallback_system_prompt(message, context, name)
    history = context.get("message_history") or None
    try:
        reply = ai_service.chat(
            message,
            system_prompt=system,
            history=history,
            client_profile=context.get("client_profile"),
            include_policy_context=True,
        )
        if reply and isinstance(reply, str) and reply.strip():
            reply = _apply_policy_guard(message=message, reply=reply.strip(), confirmed_context=False)
            log_quality_metric("fallback_ai_reply", state=(context.get("state") or {}).get("current_state", "UNKNOWN"))
            return reply
    except Exception as e:
        logger.warning("AI fallback chat failed: %s", e)
        log_quality_metric("fallback_ai_error", error_type=type(e).__name__)
    return None


def get_ai_error_response(
    message: str,
    errors: list | None = None,
    fields: dict[str, Any] | None = None,
) -> str | None:
    """
    Call AI to generate a short, client-friendly explanation when validation or
    system errors are confusing. Does not override business rules.
    """
    if not message and not errors and not fields:
        return None

    try:
        from services.ai_service import AIService
    except Exception as e:
        logger.warning("AI error fallback: could not import AIService: %s", e)
        return None

    ai_service = AIService(provider=None)

    # Build a compact prompt with context
    parts = []
    if message:
        parts.append(f"Client message: {message}")
    if errors:
        parts.append(f"Validator errors: {errors}")
    if fields:
        parts.append(f"Current booking fields: {fields}")
    prompt = "\n".join(parts)

    try:
        reply = ai_service.chat(
            prompt,
            system_prompt=_build_runtime_prompt(
                "error_clarification",
                metadata_key="error_clarification",
            ),
        )
        if reply and isinstance(reply, str):
            text = reply.strip()
            if not text:
                return None
            if len(text) > 320:
                text = text[:317].rsplit(' ', 1)[0] + "..."
            return text
    except Exception as e:
        logger.warning("AI error clarification failed: %s", e)
        return None


def get_ai_calendar_response(client_message: str = "") -> str | None:
    """
    Call AI to generate a warm, brief response when the calendar check fails.
    Returns the reply text, or None if AI is unavailable or errors.
    """
    try:
        from services.ai_service import AIService
        ai_service = AIService()
    except Exception as e:
        logger.warning("AI calendar response: AIService() failed: %s", e)
        return None

    try:
        from config import get_escort_name
        name = get_escort_name()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        name = "the escort"

    system = _build_runtime_prompt(
        "calendar_failure",
        metadata_key="calendar_failure",
        extra=f"The business name is {name}.",
    )
    prompt = client_message or "I need to check your availability."
    try:
        reply = ai_service.chat(prompt, system_prompt=system)
        if reply and isinstance(reply, str) and reply.strip():
            reply = reply.strip()
            if len(reply) > 160:
                reply = reply[:157].rsplit(' ', 1)[0] + "..."
            return reply
    except Exception as e:
        logger.warning("AI calendar response failed: %s", e)
    return None


def get_ai_booking_not_found_response(client_message: str = "") -> str | None:
    """
    Call AI to generate a warm response when no active booking is found.
    Returns the reply text, or None if AI is unavailable or errors.
    """
    try:
        from services.ai_service import AIService
        ai_service = AIService()
    except Exception as e:
        logger.warning("AI booking-not-found response: AIService() failed: %s", e)
        return None

    try:
        from config import get_escort_name
        name = get_escort_name()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        name = "the escort"

    system = _build_runtime_prompt(
        "booking_not_found",
        metadata_key="booking_not_found",
        extra=f"The business name is {name}.",
    )
    prompt = client_message or "I'm looking for my booking."
    try:
        reply = ai_service.chat(prompt, system_prompt=system)
        if reply and isinstance(reply, str) and reply.strip():
            reply = reply.strip()
            if len(reply) > 320:
                reply = reply[:317].rsplit(' ', 1)[0] + "..."
            return reply
    except Exception as e:
        logger.warning("AI booking-not-found response failed: %s", e)
    return None


def get_ai_confirmed_booking_response(context: dict[str, Any]) -> str | None:
    """
    AI response for messages in CONFIRMED state that aren't handled by keywords.
    The AI is told the client already has a confirmed booking \u2014 it must NEVER ask
    for booking details or suggest they re-book.
    """
    message = (context.get("message") or "").strip()
    if not message:
        return None

    ai_service = context.get("ai_service")
    if not ai_service:
        try:
            from services.ai_service import AIService
            ai_service = AIService()
        except Exception as e:
            logger.warning("AI confirmed response: AIService() failed: %s", e)
            return None

    try:
        from config import get_escort_name
        name = get_escort_name()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        name = "the escort"

    # Build booking context from state
    state = context.get("state") or {}
    booking_date = state.get("date") or "an upcoming date"
    booking_time = state.get("time") or ""
    booking_duration = state.get("duration") or ""
    booking_type = state.get("incall_outcall") or "incall"
    client_name = state.get("client_name") or ""

    booking_summary = f"Date: {booking_date}"
    if booking_time:
        booking_summary += f", Time: {booking_time}"
    if booking_duration:
        booking_summary += f", Duration: {booking_duration} min"
    booking_summary += f", Type: {booking_type}"

    name_part = f" {client_name}" if client_name else ""

    system = append_prompt_metadata((
        f"You are the automated SMS assistant for {name}, an escort. "
        f"The client{name_part} already has a CONFIRMED booking ({booking_summary}). "
        "Their booking is locked in \u2014 do NOT ask them for date, time, duration or any booking details. "
        "Do NOT suggest they rebook or re-submit a booking form. "
        "Respond warmly and naturally to whatever they said. "
        "Never confirm or waive deposit/payment rules. "
        "If they mention cash or deposit, acknowledge and say payment requirements depend on booking status. "
        "If they're asking a question, answer it helpfully. "
        "Keep the reply under 160 characters. Casual, friendly tone."
    ), key="confirmed_state_reply")

    history = context.get("message_history") or None
    try:
        reply = ai_service.chat(message, system_prompt=system, history=history)
        if reply and isinstance(reply, str) and reply.strip():
            reply = _apply_policy_guard(message=message, reply=reply.strip(), confirmed_context=True)
            return reply
    except Exception as e:
        logger.warning("AI confirmed booking response failed: %s", e)
    return None


def handle_fallback_with_ai(context: dict[str, Any]) -> dict[str, Any]:
    """
    Global fallback handler (registered as *, *).
    Templates-first: use the standard enquiry template. If that doesn't produce
    a useful reply (e.g. the message is truly unrecognised), try AI as a last resort.
    """
    retrieval_reply = _get_retrieval_first_policy_response(
        (context.get("message") or "").strip(),
        phone_number=(context.get("phone_number") or "").strip(),
    )
    if retrieval_reply:
        return {
            "messages": [retrieval_reply],
            "new_state": None,
            "actions": ["retrieval_policy_used"],
        }

    fallback_confidence = _estimate_fallback_confidence(context)
    threshold, step = _get_effective_fallback_confidence_threshold(context)
    if fallback_confidence < threshold:
        from templates.enquiry_templates import get_enquiry_prompt_message

        log_quality_metric(
            "fallback_low_confidence_template_path",
            confidence=round(fallback_confidence, 3),
            threshold=round(threshold, 3),
            funnel_step=step,
            state=(context.get("state") or {}).get("current_state", "UNKNOWN"),
        )
        return {
            "messages": [get_enquiry_prompt_message()],
            "new_state": None,
            "actions": ["fallback_template_low_confidence"],
        }

    # Try AI for genuinely unrecognised messages
    reply = get_ai_fallback_response(context)
    if reply:
        return {
            "messages": [reply],
            "new_state": None,
            "actions": ["ai_fallback_used"],
        }

    # AI unavailable \u2014 fall back to template
    from templates.enquiry_templates import get_enquiry_prompt_message
    return {
        "messages": [get_enquiry_prompt_message()],
        "new_state": None,
        "actions": ["fallback_template_used"],
    }
