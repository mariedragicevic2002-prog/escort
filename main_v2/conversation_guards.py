"""Frustration redirect and repeat-message guard (SMS webhook helpers)."""

from __future__ import annotations

from utils.log_sanitize import LOG_SUPPRESSED_FMT

import re as _re_fr
import unicodedata
from typing import Any

from main_v2.helpers import HARD_FRUSTRATION_PHRASES, _build_repeat_guard_message
from main_v2.log import logger


def _message_body_from_row(row: Any) -> str | None:
    """Extract message_body from RealDict, plain dict, or tuple/list row (first column)."""
    if row is None:
        return None
    if isinstance(row, dict):
        v = row.get("message_body")
        return v if v is not None else None
    if isinstance(row, (list, tuple)) and len(row) > 0:
        v = row[0]
        return v if v is not None else None
    return None


def _normalize_for_repeat_check(text: str) -> str:
    """
    Normalize a rendered SMS message for repeat detection.
    Strips punctuation, collapses whitespace, lowercases so minor template
    variations (trailing hints, interpolated names, punctuation) don't reset
    the repeat count and defeat the golden rule.
    """
    # Unicode NFKC normalisation (collapses visually identical chars)
    text = unicodedata.normalize("NFKC", text or "")
    # Strip webform URLs / signed paths so identical templates with different tokens count as repeats.
    text = _re_fr.sub(r"https?://[^\s]+", " ", text)
    text = _re_fr.sub(r"\b[\w.-]+\.(?:com|com\.au|net|org)[^\s]*", " ", text, flags=_re_fr.IGNORECASE)
    # Remove all punctuation / symbols
    text = _re_fr.sub(r"[^\w\s]", "", text)
    # Collapse whitespace
    text = _re_fr.sub(r"\s+", " ", text).strip().lower()
    return text


_GOODBYE_PHRASES = (
    "bye", "goodbye", "good bye", "see you later", "see you soon",
    "see ya", "cya", "talk soon", "speak soon", "ttyl", "take care",
)

def check_frustration(message: str, phone_number: str, state: dict, state_manager) -> dict[str, Any] | None:
    """
    Detect client frustration and return a webform redirect response.
    Returns a result dict if frustrated, None to proceed normally.
    Only fires once per conversation to avoid spamming the message.
    """
    msg = (message or "").lower().strip()

    soft_frustration_phrases = [
        "already told", "already said", "i said", "said that", "keep asking",
        "stop asking", "asked this", "same thing", "going in circles",
    ]
    has_soft_frustration = any(p in msg for p in soft_frustration_phrases)
    has_hard_frustration = any(p in msg for p in HARD_FRUSTRATION_PHRASES)

    has_multi_q = bool(_re_fr.search(r"\?{3,}", message))

    # Yield to goodbye-only handler only when there are no frustration signals — otherwise
    # messages like "this is stupid bye" must still get the frustration redirect.
    if not has_soft_frustration and not has_hard_frustration and not has_multi_q:
        if any(msg == p or msg.endswith(" " + p) for p in _GOODBYE_PHRASES):
            return None
        return None

    has_booking_detail = bool(
        _re_fr.search(
            r"\b("
            r"today|tonight|tomorrow|"
            r"mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?|"
            r"\d{1,2}(?::\d{2})?\s*(?:am|pm)|"
            r"(?:at|around|about)\s+\d{1,2}(?::\d{2})?|"
            r"\d+\s*(?:hr|hrs|hour|hours|min|mins|minute|minutes)"
            r")\b",
            msg,
        )
    )
    if has_soft_frustration and not has_hard_frustration and not has_multi_q and has_booking_detail:
        return None

    if state.get("frustration_reply_sent"):
        return None

    try:
        from core.webform_security import get_webform_url

        wf_url = get_webform_url(phone_number)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        wf_url = "(webform_url)"

    state_manager.update_fields(phone_number, {"frustration_reply_sent": True})

    reply = (
        "Sorry for any confusion \U0001f605 I'm an automated messaging system being trialled. "
        "For a smoother experience please fill in the booking webform: "
        f"{wf_url}"
    )
    return {"messages": [reply], "new_state": None, "actions": []}


def check_repeat_response(
    proposed_message: str, phone_number: str, db_service, state_manager=None
) -> str | None:
    """
    If the bot is about to send the same outbound message for the 3rd time, return repeat-guard template.
    """
    if not proposed_message or not proposed_message.strip():
        return None
    if state_manager:
        try:
            _st = state_manager.get_state(phone_number) if hasattr(state_manager, 'get_state') else None
            if _st:
                _cur = _st.get('current_state')
                if _cur in ('CONFIRMED', 'POST_BOOKING'):
                    return None
        except Exception as _st_err:
            logger.warning("check_repeat_response state check failed for %s: %s", phone_number, _st_err)
    try:
        rows = db_service.execute_query(
            """SELECT message_body FROM message_history
               WHERE phone_number = %s AND direction = 'outbound'
               ORDER BY created_at DESC LIMIT 20""",
            (phone_number,),
            fetch=True,
        )
        recent_outbound: list[str] = []
        for r in rows or []:
            mb = _message_body_from_row(r)
            if mb is not None:
                recent_outbound.append(str(mb))
        _trimmed = proposed_message.strip()
        _normalized = _normalize_for_repeat_check(_trimmed)
        repeat_count = sum(
            1 for m in recent_outbound
            if _normalize_for_repeat_check(m or "") == _normalized
        )
        if repeat_count >= 2:
            logger.info(
                "Repeat detection: same message sent %d times to %s — sending repeat-guard prompt",
                repeat_count,
                phone_number,
            )
            if state_manager:
                try:
                    state_manager.update_fields(phone_number, {"booking_status": "repeat_guard_prompt_sent"})
                except Exception as _se:
                    logger.warning("Could not set repeat_guard_prompt_sent for %s: %s", phone_number, _se)
            return _build_repeat_guard_message(phone_number)
    except Exception as _e:
        logger.warning("_check_repeat_response query failed for %s: %s — failing open to allow delivery", phone_number, _e)
        # Fail open: we cannot determine repeat count without DB access, so let the message through.
        # The outer caller (webhook_main_flow) will substitute the guard template if this function
        # itself raises — but a DB failure is expected/recoverable and must not block all replies.
    return None
