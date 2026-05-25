"""Photo keywords, repeat-guard helpers (uses main_v2.runtime for DB/state)."""

from utils.log_sanitize import LOG_SUPPRESSED_FMT

import os
import re
from typing import Any, cast

import config

from . import runtime
from .log import logger

# Shared with conversation_guards (kept here so one file upload can't drop utils/frustration_phrases.py)
HARD_FRUSTRATION_PHRASES = (
    "not working", "doesn't work", "doesnt work", "broken", "useless",
    "ridiculous", "this is stupid", "what the", "wtf", "ffs",
    "are you serious", "seriously?", "omg", "for fuck", "forget it",
    "i give up", "forget this", "this is a joke",
)


def _env_flag(name: str, default: str = "false") -> bool:
    return (os.environ.get(name, default) or default).strip().lower() == "true"


def _is_photo_request(message: str) -> bool:
    """Fast keyword check for photo/pics asks to avoid slow fallback paths."""
    msg = (message or "").strip().lower()
    if not msg:
        return False
    # Use word-boundary check for short keywords that could match substrings (e.g. "pic" inside "pick")
    if re.search(r'\bpics?\b', msg):
        return True
    phrase_keywords = (
        "photo", "photos", "more photo", "more photos",
        "send photos", "got more pics", "more images",
        "picture", "pictures", "send a pic", "send me pics",
        "send pic", "more pic",
    )
    return any(k in msg for k in phrase_keywords)


def _looks_like_photo_followup(message: str) -> bool:
    """Heuristic for short 'send more' follow-ups in an active photo thread."""
    msg = (message or "").strip().lower()
    if not msg:
        return False
    followup_keywords = (
        "some more",
        "send more",
        "send me more",
        "more please",
        "any more",
        "can you send me some more",
    )
    if not any(k in msg for k in followup_keywords):
        return False
    booking_words = ("book", "booking", "time", "times", "available", "availability", "hour", "address")
    return not any(w in msg for w in booking_words)


def _is_photo_followup_request(phone_number: str, message: str) -> bool:
    """Detect ambiguous 'send me some more' requests by checking recent photo context."""
    if not _looks_like_photo_followup(message):
        return False
    rows = []
    sm = runtime.state_manager
    try:
        if sm and hasattr(sm, "get_message_history"):
            hist = cast(list[dict[str, Any]], sm.get_message_history(phone_number, limit=8) or [])
            for item in hist:
                role = (item.get("role") or item.get("direction") or "").strip().lower()
                body = (item.get("content") or item.get("message_body") or "").strip()
                if role and body:
                    rows.append({"direction": "outbound" if role == "outbound" else "inbound", "message_body": body})
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        rows = []
    if not rows:
        try:
            if runtime.db_service:
                rows = runtime.db_service.execute_query(
                    """
                    SELECT direction, message_body
                    FROM message_history
                    WHERE phone_number = %s
                    ORDER BY created_at DESC
                    LIMIT 8
                    """,
                    (phone_number,),
                    fetch=True,
                ) or []
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
            rows = []
    if not rows:
        return False
    recent = rows[:6]
    try:
        from config import get_profile_url
        profile_hint = get_profile_url().replace("https://", "").replace("http://", "")
    except Exception:
        profile_hint = "scarletblue.com.au/escort/escort-allure"
    has_recent_photo_reply = any(
        (r.get("direction") == "outbound")
        and (
            "i share more photos on my profile" in (r.get("message_body") or "").lower()
            or profile_hint in (r.get("message_body") or "").lower()
        )
        for r in recent
    )
    has_recent_photo_ask = any(
        (r.get("direction") == "inbound")
        and _is_photo_request(r.get("message_body") or "")
        for r in recent
    )
    return has_recent_photo_reply and has_recent_photo_ask


def _build_photo_reply() -> str:
    """Return local photo/profile reply without external network calls."""
    try:
        profile_url = (config.get_profile_url() or "").strip()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        profile_url = ""
    if not profile_url:
        profile_url = "https://scarletblue.com.au/escort/escort-allure"
    return f"I share more photos on my profile:\n{profile_url}"


def _is_doubles_escort_sourcing_other_person(state: dict | None) -> bool:
    """True when we're arranging the second provider (MFF or MMF), not when the client brings them.

    Includes ``doubles_supply_gate``: the client may combine supply + pics asks in one SMS before
    we've persisted ``doubles_supply_escort`` (photo fast-path runs before booking_collection).
    The webhook only uses this together with :func:`_asks_about_other_doubles_partner_media_or_identity`.
    """
    if not state:
        return False
    bs = (state.get("booking_status") or "").strip().lower()
    if bs == "doubles_supply_escort":
        return True
    # Awaiting bring-vs-organise answer — combined "suss out other… + pics / who with" still needs UX below.
    if bs == "doubles_supply_gate":
        return True
    if (state.get("escort_supply_source") or "").strip().lower() != "escort":
        return False
    bt = (state.get("booking_type") or "").strip().lower()
    exp = (state.get("experience_type") or "").strip().lower()
    dt = (state.get("doubles_type") or "").strip().lower()
    if bt in ("Doubles MMF", "doubles_mff"):
        return True
    if dt in ("mmf", "mff"):
        return True
    if "Doubles MMF" in exp or "doubles_mff" in exp:
        return True
    return False


def _asks_about_other_doubles_partner_media_or_identity(message: str) -> bool:
    """Client asks for pics/profile/link or identity of the *other* escort (not Escort only)."""
    msg = (message or "").strip().lower()
    if not msg:
        return False
    if re.search(
        r"\bwho\s+(?:are\s+you|is\s+the|r\s+u|'re\s+you)\s+working\s+with\b",
        msg,
    ):
        return True
    if re.search(r"\bwho\s+(?:will\s+you\s+be\s+|are\s+you\s+)?bring(?:ing)?\b", msg):
        return True
    if re.search(
        r"\bwho'?s?\s+the\s+(?:other|second)\s+(?:girl|guy|man|woman|lady|escort|male|female|person|bloke|provider)\b",
        msg,
    ):
        return True
    if re.search(r"\bwhat'?s?\s+(?:her|his|their)\s+(?:name|profile)\b", msg):
        return True
    if re.search(r"\b(?:show|send)\s+(?:me\s+)?(?:him|her)\b", msg) and re.search(
        r"\b(?:other|second|doubles|double|mmf|mff|booking)\b", msg
    ):
        return True
    visual = bool(
        re.search(
            r"\b(?:pic|pics|photo|photos|picture|pictures|profile|link)\b",
            msg,
        )
    )
    if not visual:
        return False
    # Doubles partner cues (MFF + MMF): female/male/guy/girl/escort/etc.
    other_ref = bool(
        re.search(
            r"\b(?:other|second|another)\s+(?:girl|guy|man|woman|lady|escort|male|female|person|provider|"
            r"one|bloke|chick|mate)\b",
            msg,
        )
    )
    other_ref = other_ref or bool(re.search(r"\b(?:other|second)\s+escort'?s?\b", msg))
    other_ref = other_ref or bool(
        re.search(r"\b(?:her|his|their)\s+(?:pic|pics|photo|photos|profile|link)\b", msg)
    )
    other_ref = other_ref or bool(
        re.search(
            r"\b(?:pic|pics|photo|photos|picture|pictures|profile)\s+(?:of|for)\s+(?:the\s+)?"
            r"(?:other|second|another|her|him|them)\b",
            msg,
        )
    )
    other_ref = other_ref or bool(re.search(r"\b(?:your|the)\s+friend\b", msg))
    return other_ref


def _build_doubles_other_escort_media_reply() -> str:
    """When we've offered to source the other provider — client asks for their pics/profile."""
    return (
        "I'm not sure who I'll be working with yet.  "
        "As soon as I find out I'll send through some pics or a link to their profile x"
    )


def _merged_conversation_booking_snapshot(state_manager, phone_number: str) -> dict:
    """Conversation row plus booking_fields (booking wins on overlapping keys)."""
    st = state_manager.get_state(phone_number) or {}
    bf = state_manager.get_booking_fields(phone_number) or {}
    return {**st, **bf}


def _inactive_other_partner_deferral(merged: dict | None) -> bool:
    """When the client supplies the second person — deferral about sourcing 'the other escort' does not apply."""
    if not merged:
        return True
    bs = (merged.get("booking_status") or "").strip().lower()
    src = (merged.get("escort_supply_source") or "").strip().lower()
    if bs == "doubles_supply_confirmed":
        return True
    if src == "client":
        return True
    return False


def _active_escort_sourced_doubles_flow(merged: dict | None, current_state: str) -> bool:
    """Doubles flow where we may source the second provider — eligible for other-partner deferral + pickup prompts."""
    if not merged or _inactive_other_partner_deferral(merged):
        return False
    is_doubles = (
        (merged.get("booking_type") or "").strip().lower() in ("Doubles MMF", "doubles_mff")
        or (merged.get("doubles_type") or "").strip().lower() in ("mmf", "mff")
        or "Doubles MMF" in (merged.get("experience_type") or "").lower()
        or "doubles_mff" in (merged.get("experience_type") or "").lower()
    )
    if not is_doubles:
        return False
    cs = (current_state or "").strip().upper()
    if cs not in ("COLLECTING", "CHECKING_AVAILABILITY", "DEPOSIT_REQUIRED"):
        return False
    bs = (merged.get("booking_status") or "").strip().lower()
    src = (merged.get("escort_supply_source") or "").strip().lower()
    if bs == "doubles_supply_gate":
        return True
    if bs == "doubles_supply_escort" or src == "escort":
        return True
    return False


def _doubles_escort_slot_teaser_lines(
    merged: dict,
    *,
    phone_number: str,
    state_manager,
) -> str:
    """Short bullet list when date/time not chosen yet (respect 4h rule when we're sourcing)."""
    from datetime import timedelta

    from utils.availability_slots import get_next_available_time_slots
    from utils.timezone import get_current_datetime

    now = get_current_datetime()
    min_start = None
    src = (merged.get("escort_supply_source") or "").strip().lower()
    bs = (merged.get("booking_status") or "").strip().lower()
    if bs == "doubles_supply_escort" or src == "escort":
        min_start = now + timedelta(hours=4)
    kwargs = {"start_from": min_start} if min_start else {}
    slots = get_next_available_time_slots(
        now,
        num_slots=3,
        check_calendar=True,
        persist_slots_for_phone=phone_number,
        persist_slots_state_manager=state_manager,
        **kwargs,
    )
    if not slots:
        return "What day and time were you hoping for? I'll check what I have available."
    lines = "\n".join(f"\u2022 {s[1]}" for s in slots)
    return f"Here are some times that might work:\n\n{lines}\n\nLet me know what suits you best."


def _doubles_other_escort_flow_followup(
    state_manager,
    phone_number: str,
    inbound_message: str,
) -> str:
    """Append pickup prompts: missing fields, slot teaser, YES confirm, deposit wait."""
    merged = _merged_conversation_booking_snapshot(state_manager, phone_number)
    cs = (merged.get("current_state") or "NEW").strip().upper()

    if cs == "CHECKING_AVAILABILITY":
        return (
            "When you're ready, reply YES to go ahead with this booking, or tell me what you'd like to change."
        )

    if cs == "DEPOSIT_REQUIRED":
        return (
            "I'm still waiting on your deposit to lock this in — send it through when you're ready, "
            "or text me if you need the upload link again x"
        )

    if cs != "COLLECTING":
        return ""

    bs = (merged.get("booking_status") or "").strip().lower()
    if bs == "doubles_supply_gate":
        return (
            "I'm also still waiting on whether you'll be bringing the other person yourself, "
            "or if you'd like me to organise them for you (minimum 4 hours notice if I'm arranging)."
        )

    try:
        import config as cfg
        from booking.field_collector import FieldCollector
        from templates.field_prompts import build_missing_fields_message
        from utils.dinner_date import is_dinner_date_booking

        fc = FieldCollector(cfg, ai_service=None)
        missing = fc.get_missing_fields(merged)
        _exp_ok = bool((merged.get("experience_type") or "").strip()) or is_dinner_date_booking(merged)
        _is_oc = str((merged.get("incall_outcall") or "")).lower() == "outcall"
        if missing:
            prompt = build_missing_fields_message(
                missing,
                context_message=inbound_message or "",
                experience_already_set=_exp_ok,
                is_outcall=_is_oc,
            )
            if prompt:
                return prompt

        if not merged.get("date") or not merged.get("time"):
            return _doubles_escort_slot_teaser_lines(
                merged,
                phone_number=phone_number,
                state_manager=state_manager,
            )

        if not merged.get("duration"):
            return "How long would you like to book for?"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
    return ""


def build_doubles_other_escort_media_reply_bundle(
    state_manager,
    phone_number: str,
    inbound_message: str,
) -> str:
    """Deferral SMS plus whatever we still need (slots, fields, confirm, deposit)."""
    base = _build_doubles_other_escort_media_reply()
    tail = _doubles_other_escort_flow_followup(state_manager, phone_number, inbound_message)
    if tail:
        return f"{base}\n\n{tail}"
    return base


def _is_webform_request(message: str) -> bool:
    """Fast keyword check for booking webform link requests."""
    msg = (message or "").strip().lower()
    if not msg:
        return False
    webform_keywords = (
        "webform", "web form", "booking form", "booking link", "book online",
        "send the link", "send me the link", "send link", "the link",
        "form link", "online form", "fill in the form", "fill out the form",
    )
    return any(k in msg for k in webform_keywords)


def _build_webform_reply(phone_number: str) -> str:
    """Return a webform link for this client (personalised short URL if possible)."""
    try:
        from core.webform_security import get_webform_url

        url = get_webform_url(phone_number)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        url = f"{config.get_base_url()}/booking"
    return url


def _is_location_request(message: str) -> bool:
    """Fast keyword check for location/address enquiries."""
    msg = (message or "").strip().lower()
    if not msg:
        return False
    location_keywords = (
        "where are you", "your address", "what's your address", "whats your address",
        "your location", "where do you", "where r you", "where you at",
        "what suburb", "what city", "where is it", "where is the",
        "your place", "your hotel", "where you based", "where are u",
        "what area", "where abouts", "whereabouts",
    )
    return any(k in msg for k in location_keywords)


def _build_location_reply() -> str:
    """Return location + hours from config."""
    try:
        from config import get_available_hours, get_cbd_label_for_messages, get_current_incall_location, get_effective_booking_city
        loc = get_current_incall_location() or {}
        city = loc.get("city", "") or get_effective_booking_city()
        hotel = loc.get("display_name") or loc.get("hotel_name") or ""
        address = loc.get("address") or ""
        hours = get_available_hours() or ""
        cbd_fallback = get_cbd_label_for_messages(city)
        hotel_addr = " ".join(p for p in [hotel, address] if p)
        city_already_in_addr = city and city.lower() in hotel_addr.lower()
        if hotel_addr and city and not city_already_in_addr:
            location_str = f"I'm located at {hotel_addr} {city}"
        elif hotel_addr:
            location_str = f"I'm located at {hotel_addr}"
        else:
            location_str = f"I'm currently in {cbd_fallback}"
        parts = [location_str]
        if hours:
            parts.append(f"Available: {hours}")
        return "\n".join(parts)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        try:
            from config import get_cbd_label_for_messages

            _cbd = get_cbd_label_for_messages()
        except Exception:
            _cbd = "my current location"
        return f"I'm currently in {_cbd}. Message me to confirm my exact location."


def _is_screenshot_link_request(message: str) -> bool:
    """Fast keyword check for deposit screenshot/upload link requests."""
    msg = (message or "").strip().lower()
    if not msg:
        return False
    screenshot_keywords = (
        "screenshot link", "upload link", "deposit link", "payid link",
        "send screenshot", "resend screenshot", "send the screenshot",
        "send upload", "resend upload", "the upload link",
        "send deposit link", "resend deposit", "resend the link",
        "send link again", "send me the link again", "link again",
    )
    return any(k in msg for k in screenshot_keywords)


def _build_screenshot_link_reply(phone_number: str, state_manager) -> str:
    """Return deposit upload link, falling back to webform if generation fails."""
    deposit_amount = 100
    try:
        state = state_manager.get_state(phone_number) or {}
        deposit_amount = int(state.get("deposit_amount") or 100)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
    try:
        from templates.utility_templates import get_upload_link_success_message
        return get_upload_link_success_message(phone_number, deposit_amount, force_new=True)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        # Fall back to webform link if upload token generation fails
        return _build_webform_reply(phone_number)


def _is_enquiry_keyword(message: str) -> bool:
    """Detect bare 'ENQUIRY' keyword (client wants to speak directly, not mid-booking)."""
    msg = (message or "").strip().lower()
    # Only trigger on standalone "enquiry" — not "ENQUIRY what's your rate?" (that's a real enquiry)
    return msg == "enquiry"


def _is_enquiry_with_description(message: str) -> bool:
    """Detect ENQUIRY messages that include actual question text."""
    msg = (message or "").strip()
    if not msg:
        return False
    lowered = msg.lower()
    return lowered.startswith("enquiry ") and bool(lowered[8:].strip())


def _build_enquiry_keyword_reply() -> str:
    """Return automated-service notice with escort name from config."""
    try:
        from config import get_escort_name
        name = get_escort_name() or "Adella"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        name = "Adella"
    return (
        f"Due to the number of enquiries I receive you are speaking to an automated service.\n\n"
        f"To speak to {name} directly please text ENQUIRY followed by your question.\n\n"
        f"Example: 'ENQUIRY Can you do doubles bookings?'"
    )


def _is_goodbye(message: str) -> bool:
    """Fast keyword check for farewell messages."""
    msg = (message or "").strip().lower()
    if not msg:
        return False
    # Only match short farewell-only messages — avoid "bye I want to book"
    if len(msg) > 40:
        return False
    # Exclude messages that are clearly availability/booking enquiries
    not_goodbye_hints = (
        "can i", "come see", "come to", "book", "available", "free",
        "when", "what time", "visit", "see you at",
    )
    if any(h in msg for h in not_goodbye_hints):
        return False
    if any(p in msg for p in HARD_FRUSTRATION_PHRASES):
        return False
    goodbye_keywords = (
        "bye", "goodbye", "good bye", "see you later", "see you soon",
        "see ya", "cya", "talk soon", "speak soon", "ttyl",
        "take care", "thanks bye", "thank you bye", "cheers bye", "all good thanks",
    )
    return any(k in msg for k in goodbye_keywords)


def _build_goodbye_reply(phone_number: str) -> str:
    """Return a friendly farewell with booking link."""
    try:
        from core.webform_security import get_webform_url

        url = get_webform_url(phone_number)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        url = f"{config.get_base_url()}/booking"
    return (
        "No worries x If you'd like to make a booking later, use my webform:\n"
        f"{url}"
    )


def _repeat_guard_enabled() -> bool:
    """Always on: when the bot would repeat the same reply, substitute the ENQUIRY + webform template (no admin toggle)."""
    return True


def _build_repeat_guard_message(phone_number: str) -> str:
    """Build escalation message shown when bot repeats itself too often."""
    try:
        from core.webform_security import get_webform_url

        webform_url = get_webform_url(phone_number)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        webform_url = f"{config.get_base_url()}/booking"
    try:
        from config import get_escort_name
        name = get_escort_name() or "Adella"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        name = "Adella"
    return (
        f"Due to the number of enquiries I receive you are speaking to an automated service.\n\n"
        f"If you wish to speak to {name} directly please text ENQUIRY back to this number "
        f"along with a brief description of what you're wanting to ask.\n\n"
        f"If you're wanting to make a booking please use my booking webform:\n"
        f"{webform_url}"
    )


def _build_repeat_guard_final_message(phone_number: str) -> str:
    """Build final cutoff message after repeat-guard prompt is ignored."""
    try:
        from core.webform_security import get_webform_url

        webform_url = get_webform_url(phone_number)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        webform_url = f"{config.get_base_url()}/booking"
    try:
        from config import get_escort_name
        name = get_escort_name() or "Adella"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        name = "Adella"
    return (
        "I'm afraid I'm no longer able to continue this conversation.\n\n"
        f"As previously advised if you wish to speak to {name} directly please text ENQUIRY back to this number "
        "along with a brief description of what you're wanting to ask.\n\n"
        "If you're wanting to make a booking please use my booking webform:\n"
        f"{webform_url}"
    )


def _should_use_repeat_guard(phone_number: str, message: str, current_state: dict | None = None) -> bool:
    """Return True when the same outbound message has already been sent at least twice."""
    if current_state and current_state.get('current_state') in ('CONFIRMED', 'POST_BOOKING'):
        return False
    if not _repeat_guard_enabled():
        return False
    current = (message or "").strip()
    if not current:
        return False
    if "text ENQUIRY back to this number" in current:
        return False
    try:
        def _repeat_signature(text: str) -> str:
            """Group paraphrased prompts into semantic repeat families."""
            t = (text or "").strip().lower()
            if not t:
                return ""
            t = re.sub(r"https?://\S+", "", t)
            t = " ".join(t.split())
            if "what specifically would you like me to send" in t or "more what specifically" in t:
                return "clarify_media_request"
            if "which time works for you" in t:
                return "ask_time"
            if "what time works for you and what's your address" in t or "where are you located" in t:
                return "ask_time_and_address"
            if "what type of experience are you after" in t:
                return "ask_experience"
            if "how long would you like to book for and what type of experience" in t:
                return "ask_duration_and_experience"
            return ""

        if not runtime.db_service:
            return False
        rows = cast(
            list[dict[str, Any]],
            runtime.db_service.execute_query(
                """
                SELECT message_body
                FROM message_history
                WHERE phone_number = %s AND direction = 'outbound'
                ORDER BY created_at DESC
                LIMIT 8
                """,
                (phone_number,),
                fetch=True,
            ) or [],
        )
        repeat_count = sum(1 for row in rows if (row.get("message_body") or "").strip() == current)
        if repeat_count >= 2:
            return True
        current_sig = _repeat_signature(current)
        if not current_sig:
            return False
        family_repeat_count = sum(
            1 for row in rows
            if _repeat_signature((row.get("message_body") or "").strip()) == current_sig
        )
        return family_repeat_count >= 2
    except Exception as e:
        logger.warning("Repeat-guard check failed for %s: %s", phone_number, e)
        return False
