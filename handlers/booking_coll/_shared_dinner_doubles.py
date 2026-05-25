"""Doubles supply gate and dinner-date field collection (booking_coll)."""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import logging
import re
from datetime import datetime
from typing import Any

from core.booking_substates import (
    DOUBLES_SUPPLY_CONFIRMED,
    DOUBLES_SUPPLY_ESCORT,
    DOUBLES_SUPPLY_GATE,
)
from utils.dinner_date import is_dinner_date_booking

logger = logging.getLogger("adella_chatbot.handlers.collecting")


_CLIENT_SUPPLIES_PATTERNS = re.compile(
    r"\b("
    r"i.?ll bring|i.?ll be bringing|i will bring|i will be bringing|i.?m bringing|my friend|my mate|my buddy|"
    r"i have someone|i.?ve got someone|client supplies|"
    r"i.?ll be provid\w*|i will be provid\w*|i.?m provid\w*|i.?ll provid\w*|"
    r"(a |my )(friend|mate|buddy) (which |that )?(i.?ll|i will|i am|i.?m) (be )?(bring\w*|provid\w*|supply\w*|organi[sz]\w*|sort)|"
    r"bringing (a |my |the )?(friend|mate|guy|girl|female|male|other person|second person|other one)|"
    r"provid\w* (a |my |the )?(friend|mate|guy|girl|female|male|other person|second person|other one)|"
    r"we.?ll (bring|organi[sz]e|organsie|supply|sort)|"
    r"already (have|got|found|organi[sz]ed|organsied)|"
    r"i.?ll (organi[sz]e|organsie|sort|find|supply|arrange|get)|"
    r"i (have|got) (a |the )?(girl|guy|male|female|friend|mate|escort)"
    r")\b", re.IGNORECASE,
)

# Natural-language variants for "please arrange the other person" (doubles MFF + MMF gate).
# Joined with OR — any hit counts as escort-supplied other person.
_ESCORT_SUPPLY_FRAGMENTS = (
    # Modal + you + verb (typo-tolerant: csn/can, san/can)
    r"(?:can|c[sa]n|could|will|would)\s+(?:you|u)\s+(?:please\s+)?(?:organi[sz]e|organsie|find|supply|sort|arrange|get|source|provide|book)\b",
    r"(?:can|c[sa]n|could|will|would)\s+(?:you|u)\s+(?:please\s+)?(?:bring|get)\s+(?:the\s+|another\s+|an\s+)?(?:other\s+|second\s+)?(?:girl|guy|female|male|person|escort|lady|woman|bloke|chick|man)\b",
    r"(?:can|c[sa]n|could|will|would)\s+(?:you|u)\s+(?:please\s+)?hook\s+up(?:\s+with)?\s+(?:the\s+|another\s+|an\s+)?(?:other\s+|second\s+)?(?:girl|guy|bloke|male|female|person|escort|lady|woman|man|chick|mate|buddy|one)\b",
    r"(?:can|c[sa]n|could|will|would)\s+(?:you|u)\s+(?:please\s+)?suss\s+out\s+(?:the\s+|another\s+|an\s+)?(?:other\s+|second\s+)?(?:girl|guy|bloke|male|female|person|escort|lady|woman|man|chick|mate|buddy|one)\b",
    # "you(to) organise/provide/bring …"
    r"(?:you|u)\s+(?:to\s+)?(?:organi[sz]e|organsie|find|supply|sort|arrange|get|source|provide|bring|hook\s+up|suss\s+out)\b",
    r"need\s+(?:you|u)\s+to|(?:you|u).?ll\s+need\s+to",
    # Verb + object ("organise the other girl/guy", "provide her/him for me", "book another escort")
    r"(?:organi[sz]e|organsie|find|supply|sort|arrange|source|provide|book|get|hook\s+up(?:\s+with)?|suss\s+out)\s+(?:the\s+|another\s+|an\s+|me\s+)?(?:other\s+|second\s+)?(?:girl|guy|female|male|person|escort|lady|woman|mate|buddy|man|bloke|chick|one)\b",
    r"(?:organi[sz]e|organsie|arrange|provide|find|get)\s+(?:her|him|them|he)\b(?:\s+for\s+me)?",
    # Hoping / wishing you'd organise (handles "im was hoping you can…")
    r"(?:i\s*m\s+|i'?m\s+)?(?:was\s+)?hop\w*\s+(?:that\s+)?(?:you|u)\s+(?:can|c[sa]n|could|will|would)\s+(?:please\s+)?(?:organi[sz]e|organsie|arrange|find|provide|get|bring)\b",
    r"(?:i\s*m\s+|i'?m\s+)?(?:was\s+)?hop\w*\s+(?:that\s+)?(?:you|u)\s+(?:could\s+)?(?:organi[sz]e|organsie|arrange|provide)\s+(?:her|him|them|he|the\s+(?:other\s+)?(?:girl|guy|escort|male|mate|man|buddy|bloke|chick|woman))\b",
    r"(?:wish|wanted)\s+(?:you|u)\s+(?:could\s+)?(?:organi[sz]e|organsie|arrange|provide)\b",
    # "Is it ok if you/your bring …" (your = common typo for you'll/you)
    r"(?:is\s+it\s+)?(?:ok|okay|alright|fine)\s+(?:with\s+you\s+)?(?:if\s+)?(?:you|your|u)\s+(?:can\s+|could\s+)?(?:bring|organi[sz]e|organsie|arrange|provide|get)\s+(?:the\s+|another\s+|an\s+)?(?:other\s+|second\s+)?(?:girl|guy|female|male|person|escort|lady|woman|bloke|chick|man|one)\b",
    r"(?:is\s+it\s+)?(?:ok|okay)\s+for\s+(?:you|u)\s+to\s+(?:bring|organi[sz]e|organsie|arrange|provide)\b",
    # I'd prefer / want / need you to …
    r"(?:i.?d\s+)?(?:prefer|like|want|need)\s+(?:you|u)\s+to\s+(?:organi[sz]e|organsie|arrange|find|provide|bring|get|book)\b",
    r"(?:am\s+)?(?:really\s+)?(?:counting|relying)\s+on\s+(?:you|u)\s+to\s+(?:organi[sz]e|organsie|arrange|provide|bring)\b",
    # Informal phrasing
    r"(?:could|can)\s+(?:you|u)\s+(?:(?:sort|line)\s+(?:out\s+|up\s+)?|suss\s+out\s+)(?:another\s+|an\s+|the\s+)?(?:girl|guy|escort|lady|male|female|mate|buddy|man|bloke|chick|woman|person|one)\b",
    r"(?:got|have)\s+(?:any|another)\s+(?:girl|guy|escort|lady|male|mate|buddy)\s+(?:you|u)\s+(?:can|could)\s+(?:bring|organi[sz]e|arrange)|"
    r"(?:you|u)\s+(?:have|got)\s+(?:someone|another (?:girl|guy|escort|male|mate|buddy))\s+(?:in mind|available)|"
    r"(?:pick|choose)\s+(?:someone|another (?:girl|guy|escort|male|mate|buddy))\s+for\s+(?:me|us)",
    # Capability gaps → escort must supply
    r"don.?t have (anyone|one|a |somebody|someone)",
    r"no i don.?t|no one|nobody",
    r"i don.?t (have|know) (anyone|one|somebody|someone|a )",
)

_ESCORT_SUPPLIES_PATTERNS = re.compile(
    "|".join(f"(?:{frag})" for frag in _ESCORT_SUPPLY_FRAGMENTS),
    re.IGNORECASE,
)

def doubles_supply_patterns_touch(message: str) -> bool:
    """True when the client reply picks up bring-vs-organise wording (route through COLLECTING, not photo fast-path)."""
    msg = (message or "").strip()
    if not msg:
        return False
    return bool(_CLIENT_SUPPLIES_PATTERNS.search(msg) or _ESCORT_SUPPLIES_PATTERNS.search(msg))


_MMF_PATTERN = re.compile(
    r"\b("
    r"mmf|"
    r"two guys?|2 guys?|"
    r"(?:me|i)\s+and\s+(?:my\s+|a\s+)?(?:mate|friend)|"
    r"my\s+(?:mate|friend)\s+and\s+(?:i|me)|"
    r"male"
    r")\b",
    re.IGNORECASE,
)
_MFF_PATTERN = re.compile(r"\b(mff|two girls?|2 girls?|another (girl|female|escort)|female)\b", re.IGNORECASE)
_FEMALE_DOUBLES_TERMS = re.compile(
    r"\b(girl|girls|female|females|woman|women|lady|ladies|chick|chicks|her|she)\b",
    re.IGNORECASE,
)
_MALE_DOUBLES_TERMS = re.compile(
    r"\b(mmf|mfm|male|males|guy|guys|bloke|blokes|man|men|dude|dudes|him|he|mate|mates|friend|friends)\b",
    re.IGNORECASE,
)
_GENERIC_SECOND_PERSON_TERMS = re.compile(
    r"\b(other person|second person|other one)\b",
    re.IGNORECASE,
)

_IMPLICIT_ESCORT_MMF = re.compile(
    r"\b(?:"
    r"you\s+and\s+another\s+(?:bloke|guy|man|male|dude|fella)\b|"
    r"(?:book|booking)\s+(?:me\s+)?(?:you|u)\s+and\s+another\s+(?:bloke|guy|man|male)\b|"
    r"\banother\s+(?:bloke|guy|man|male)\s+for\s+(?:an?\s+)?(?:mmf|doubles)\b"
    r")\b",
    re.IGNORECASE,
)
_IMPLICIT_ESCORT_MFF = re.compile(
    r"\b(?:"
    r"you\s+and\s+another\s+(?:girl|woman|female|chick|lady|escort)\b|"
    r"(?:book|booking)\s+(?:me\s+)?(?:you|u)\s+and\s+another\s+(?:girl|woman|female|chick|lady|escort)\b|"
    r"\banother\s+(?:girl|woman|female|chick|lady)\s+for\s+(?:an?\s+)?(?:mff|doubles)\b|"
    r"your\s+(?:friend|girl|colleague|escort|partner)\b|"
    r"what does (?:she|your friend|the other (?:girl|escort|woman|female))\s+look like\b"
    r")\b",
    re.IGNORECASE,
)


def _message_implies_outcall_doubles(message: str) -> bool:
    """Lightweight outcall detector (avoids importing new_conv._shared circularly)."""
    text = (message or "").lower().strip()
    if not text:
        return False
    keys = (
        "outcall",
        "out call",
        "my place",
        "my hotel",
        "my address",
        "my location",
        "my apartment",
        "my room",
        "my airbnb",
        "my unit",
        "my suite",
        "come to me",
        "come to my",
        "come over",
        "come see me",
        "come and see me",
        "visit me",
        "staying at",
        "i'm at ",
        "im at ",
        "i am at ",
        "located at",
        "can you come",
        "you come to",
        "to my hotel",
        "to my place",
    )
    return any(k in text for k in keys)


def _state_implies_outcall_doubles(state: dict | None) -> bool:
    return str((state or {}).get("incall_outcall") or "").strip().lower() == "outcall"


def implicit_escort_supplies_other_person(message: str) -> bool:
    """
    Client booked ``you + another male/female`` for doubles without saying they'll bring someone —
    treat as escort sourcing the second provider.
    """
    m = (message or "").strip()
    if not m:
        return False
    ml = m.lower()
    mmf_ctx = bool(_MMF_PATTERN.search(m)) or "mmf" in ml or "two guy" in ml or "two guys" in ml
    mff_ctx = bool(_MFF_PATTERN.search(m)) or "mff" in ml or "two girl" in ml or "two girls" in ml
    if mmf_ctx and _IMPLICIT_ESCORT_MMF.search(m):
        return True
    if mff_ctx and _IMPLICIT_ESCORT_MFF.search(m):
        return True
    return False


def infer_doubles_type_hint_from_message(message: str) -> str | None:
    """
    Infer MMF/MFF from natural-language cues when explicit `mmf`/`mff` tags are absent.

    For production safety we only emit a hint when one side is clearly implied:
    - female-only terms -> mff
    - male-only terms -> mmf
    - generic "other person/second person/friend" without female terms -> mmf
    """
    msg = (message or "").strip()
    if not msg:
        return None
    female_hit = bool(_FEMALE_DOUBLES_TERMS.search(msg))
    male_hit = bool(_MALE_DOUBLES_TERMS.search(msg))
    generic_second = bool(_GENERIC_SECOND_PERSON_TERMS.search(msg))

    if female_hit and not male_hit:
        return "mff"
    if male_hit and not female_hit:
        return "mmf"
    if generic_second and not female_hit:
        return "mmf"
    return None


def _collecting_response(message: str) -> dict[str, Any]:
    return {"messages": [message], "new_state": "COLLECTING", "actions": []}


def _doubles_is_outcall(message: str, current_state: dict, fallback_state: dict, detector) -> bool:
    return bool(
        detector(message)
        or _state_implies_outcall_doubles(current_state)
        or _state_implies_outcall_doubles(fallback_state)
    )


def _doubles_load_context(phone_number: str, state_manager, fallback_state: dict) -> tuple[dict, str, str, str, str]:
    fresh = state_manager.get_state(phone_number) or fallback_state
    dtype = (fresh.get("doubles_type") or "").strip().lower()
    booking_type = str(fresh.get("booking_type") or "")
    experience_type = str(fresh.get("experience_type") or "")
    client_name = (fresh.get("client_name") or "").strip()
    return fresh, dtype, booking_type, experience_type, client_name


def _build_doubles_slots_message(
    phone_number: str,
    state_manager,
    *,
    start_from: datetime | None,
    intro_line: str,
    include_pair_outcall_travel_notice: bool = False,
) -> str:
    """Build escort-supply doubles slot SMS; never raises (production webhook must not 500)."""
    from config import get_current_incall_location, get_profile_url
    from core.webform_security import get_webform_url
    from templates.special_bookings import build_doubles_escort_supply_slots_message
    from utils.availability_slots import (
        get_next_available_time_slots,
        persist_offered_slots_from_time_slot_pairs,
    )
    from utils.timezone import get_current_datetime

    webform_url = get_webform_url(phone_number)
    location = get_current_incall_location() or {}
    city = (location.get("city") or "").strip()
    hotel_name = (location.get("hotel_name") or "").strip()
    address = (location.get("address") or "").strip()
    profile_url = (get_profile_url() or "").strip()

    try:
        now = get_current_datetime()
        kwargs = {"start_from": start_from} if start_from is not None else {}
        time_slots = get_next_available_time_slots(
            now,
            num_slots=3,
            check_calendar=True,
            **kwargs,
        )
        persist_offered_slots_from_time_slot_pairs(phone_number, state_manager, time_slots)
        return build_doubles_escort_supply_slots_message(
            time_slots=time_slots,
            profile_url=profile_url,
            webform_url=webform_url,
            city=city,
            hotel_name=hotel_name,
            address=address,
            intro_line=intro_line,
            include_pair_outcall_travel_notice=include_pair_outcall_travel_notice,
        )
    except Exception as e:
        logger.error(
            "[DOUBLES] escort-supply slot message failed for %s: %s",
            phone_number,
            e,
            exc_info=True,
        )
        try:
            return build_doubles_escort_supply_slots_message(
                time_slots=[],
                profile_url=profile_url,
                webform_url=webform_url,
                city=city,
                hotel_name=hotel_name,
                address=address,
                intro_line=intro_line,
                include_pair_outcall_travel_notice=include_pair_outcall_travel_notice,
            )
        except Exception as e2:
            logger.error(
                "[DOUBLES] escort-supply slot fallback template failed for %s: %s",
                phone_number,
                e2,
                exc_info=True,
            )
            return (
                f"{intro_line}\n\n"
                "I'm having a brief issue listing times — reply with when you'd like to book "
                f"or use my webform:\n{webform_url}"
            )


def _doubles_safe_profile_url() -> str:
    try:
        from config import get_profile_url

        return (get_profile_url() or "").strip()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return ""


def _doubles_safe_pair_deposit() -> int:
    try:
        from core.rates_from_config import get_deposit_mff_pair

        return int(get_deposit_mff_pair())
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return 200


def _doubles_safe_outcall_surcharge(fresh_state: dict, is_outcall: bool) -> int:
    from core.rates_from_config import get_outcall_travel_surcharge_for_booking

    booking_fields = dict(fresh_state or {})
    if is_outcall:
        booking_fields["incall_outcall"] = "outcall"
    try:
        return int(get_outcall_travel_surcharge_for_booking(booking_fields))
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return 100


def _doubles_build_client_available_now_message(
    phone_number: str,
    state_manager,
    fresh_state: dict,
    client_name: str,
    doubles_type: str,
    is_outcall: bool,
) -> dict[str, Any]:
    from config import get_current_incall_location
    from core.webform_security import get_webform_url
    from templates.special_bookings import build_doubles_available_now_message
    from utils.availability_slots import get_next_available_time_slots
    from utils.timezone import get_current_datetime

    now = get_current_datetime()
    time_slots = get_next_available_time_slots(
        now,
        num_slots=3,
        check_calendar=True,
        persist_slots_for_phone=phone_number,
        persist_slots_state_manager=state_manager,
    )
    location = get_current_incall_location() or {}
    webform_url = get_webform_url(phone_number)
    profile_url = _doubles_safe_profile_url()
    deposit = _doubles_safe_pair_deposit()
    surcharge = _doubles_safe_outcall_surcharge(fresh_state, is_outcall)
    full_msg = build_doubles_available_now_message(
        client_name=client_name or "",
        doubles_type=doubles_type,
        time_slots=time_slots,
        profile_url=profile_url,
        webform_url=webform_url,
        city=(location.get("city") or "").strip(),
        hotel_name=(location.get("hotel_name") or "").strip(),
        address=(location.get("address") or "").strip(),
        is_outcall=is_outcall,
        surcharge=surcharge,
        deposit=deposit,
        intro_style="love",
    )
    return _collecting_response(full_msg)


def _doubles_update_type_from_message(
    msg: str,
    phone_number: str,
    state_manager,
    fallback_doubles_type: str | None,
) -> None:
    if _MMF_PATTERN.search(msg):
        state_manager.update_fields(phone_number, {
            "doubles_type": "mmf",
            "experience_type": "Doubles MMF",
        })
        return
    if _MFF_PATTERN.search(msg):
        state_manager.update_fields(phone_number, {
            "doubles_type": "mff",
            "experience_type": "doubles_mff",
        })
        return

    inferred_type = (infer_doubles_type_hint_from_message(msg) or fallback_doubles_type or "").strip().lower()
    if inferred_type == "mmf":
        state_manager.update_fields(
            phone_number,
            {
                "doubles_type": "mmf",
                "experience_type": "Doubles MMF",
                "booking_type": "Doubles MMF",
            },
        )
        return
    if inferred_type == "mff":
        state_manager.update_fields(
            phone_number,
            {
                "doubles_type": "mff",
                "experience_type": "doubles_mff",
                "booking_type": "doubles_mff",
            },
        )


def _doubles_detect_supply_flags(
    msg: str,
    phone_number: str,
    fallback_supply_source: str | None,
) -> tuple[bool, bool]:
    client_supplies = bool(_CLIENT_SUPPLIES_PATTERNS.search(msg))
    escort_supplies = bool(_ESCORT_SUPPLIES_PATTERNS.search(msg))
    if implicit_escort_supplies_other_person(msg) and not client_supplies:
        escort_supplies = True
    if client_supplies or escort_supplies:
        return client_supplies, escort_supplies

    source = (fallback_supply_source or "").strip().lower()
    if source:
        logger.info(
            "[DOUBLES] %s: ignoring advisory fallback supply source=%s until client confirms",
            phone_number,
            source,
        )
    return client_supplies, escort_supplies


def _doubles_handle_client_supply_new_enquiry(
    msg: str,
    phone_number: str,
    fallback_state: dict,
    state_manager,
) -> dict[str, Any]:
    from core.webform_security import get_webform_url
    from handlers.booking_coll.doubles_first_turn_compose import compose_client_supplied_doubles_first_turn
    from handlers.new_conv._shared import _has_outcall_intent
    from templates import greetings as _greetings

    fresh, dtype, booking_type, experience_type, client_name = _doubles_load_context(
        phone_number, state_manager, fallback_state
    )
    client_name = client_name or _greetings.extract_client_name(msg)
    is_outcall = _doubles_is_outcall(msg, fresh, fallback_state, _has_outcall_intent)
    specific_turn = compose_client_supplied_doubles_first_turn(
        message=msg,
        phone_number=phone_number,
        state_manager=state_manager,
        client_name=client_name or "",
        doubles_type=dtype,
        booking_type=booking_type,
        experience_type=experience_type,
        webform_url=get_webform_url(phone_number),
        is_outcall=is_outcall,
    )
    if specific_turn is not None:
        return _collecting_response(specific_turn)
    return _doubles_build_client_available_now_message(
        phone_number,
        state_manager,
        fresh,
        client_name or "",
        dtype,
        is_outcall,
    )


def _doubles_handle_client_supply_response(
    msg: str,
    phone_number: str,
    fallback_state: dict,
    state_manager,
    doubles_supply_gate_follow_up: bool,
) -> dict[str, Any]:
    state_manager.update_fields(
        phone_number,
        {
            "escort_supply_confirmed": True,
            "escort_supply_source": "client",
            "booking_status": DOUBLES_SUPPLY_CONFIRMED,
        },
    )
    logger.info(f"[DOUBLES] {phone_number}: client will supply the other person")
    if doubles_supply_gate_follow_up:
        return _collecting_response(
            _build_doubles_slots_message(
                phone_number,
                state_manager,
                start_from=None,
                intro_line=(
                    "Thanks for confirming you will be organising the other person/escort. "
                    "Here are the times I have available:"
                ),
                include_pair_outcall_travel_notice=False,
            )
        )
    return _doubles_handle_client_supply_new_enquiry(msg, phone_number, fallback_state, state_manager)


def _doubles_compose_escort_supply_slot_body(
    msg: str,
    phone_number: str,
    fallback_state: dict,
    state_manager,
) -> str:
    from core.webform_security import get_webform_url
    from handlers.booking_coll.doubles_first_turn_compose import (
        compose_escort_sourced_doubles_first_turn,
    )

    fresh, dtype, booking_type, experience_type, client_name = _doubles_load_context(
        phone_number, state_manager, fallback_state
    )
    is_outcall = _doubles_is_outcall(msg, fresh, fallback_state, _message_implies_outcall_doubles)
    return compose_escort_sourced_doubles_first_turn(
        message=msg,
        phone_number=phone_number,
        state_manager=state_manager,
        client_name=client_name,
        doubles_type=dtype,
        booking_type=booking_type,
        experience_type=experience_type,
        webform_url=get_webform_url(phone_number),
        is_outcall=is_outcall,
    )


def _doubles_maybe_prefix_partner_media_reply(msg: str, slot_body: str) -> str:
    try:
        from main_v2.helpers import (
            _asks_about_other_doubles_partner_media_or_identity,
            _build_doubles_other_escort_media_reply,
        )

        if _asks_about_other_doubles_partner_media_or_identity(msg):
            return f"{_build_doubles_other_escort_media_reply()}\n\n{slot_body}"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
    return slot_body


def _doubles_handle_escort_supply_response(
    msg: str,
    phone_number: str,
    fallback_state: dict,
    state_manager,
    doubles_supply_gate_follow_up: bool,
) -> dict[str, Any]:
    from handlers.booking_coll.doubles_first_turn_compose import _escort_supply_notice_floor_start
    from utils.timezone import get_current_datetime

    now = get_current_datetime()
    min_start = _escort_supply_notice_floor_start(now)
    state_manager.update_fields(phone_number, {
        "escort_supply_confirmed": True,
        "escort_supply_source": "escort",
        "booking_status": DOUBLES_SUPPLY_ESCORT,
    })
    logger.info(f"[DOUBLES] {phone_number}: escort will supply the other person (4hr notice)")
    if doubles_supply_gate_follow_up:
        slot_body = _build_doubles_slots_message(
            phone_number,
            state_manager,
            start_from=min_start,
            intro_line="No worries, I can organise the other escort for you. Here are the times I have available:",
            include_pair_outcall_travel_notice=False,
        )
    else:
        slot_body = _doubles_compose_escort_supply_slot_body(msg, phone_number, fallback_state, state_manager)
    slot_body = _doubles_maybe_prefix_partner_media_reply(msg, slot_body)
    return _collecting_response(slot_body)


def _doubles_handle_ambiguous_supply_response(
    msg: str,
    phone_number: str,
    fallback_state: dict,
    state_manager,
    doubles_supply_gate_follow_up: bool,
) -> dict[str, Any]:
    state_manager.update_fields(phone_number, {"booking_status": DOUBLES_SUPPLY_GATE})
    if doubles_supply_gate_follow_up:
        return _collecting_response(
            "Before I can check availability, I need to know — will you be bringing "
            "the other person yourself, or do you need me to organise them for you?"
        )

    from core.webform_security import get_webform_url
    from handlers.booking_coll.doubles_first_turn_compose import (
        compose_ambiguous_doubles_supply_first_turn,
    )

    fresh, dtype, booking_type, experience_type, client_name = _doubles_load_context(
        phone_number, state_manager, fallback_state
    )
    is_outcall = _doubles_is_outcall(msg, fresh, fallback_state, _message_implies_outcall_doubles)
    body = compose_ambiguous_doubles_supply_first_turn(
        message=msg,
        phone_number=phone_number,
        state_manager=state_manager,
        client_name=client_name,
        doubles_type=dtype,
        booking_type=booking_type,
        experience_type=experience_type,
        webform_url=get_webform_url(phone_number),
        is_outcall=is_outcall,
    )
    return _collecting_response(body)


def _check_doubles_supply_response(
    message: str,
    phone_number: str,
    _state: dict,
    state_manager,
    *,
    doubles_supply_gate_follow_up: bool = False,
    fallback_supply_source: str | None = None,
    fallback_doubles_type: str | None = None,
) -> dict | None:
    """Parse the client's reply to the doubles supply question — or infer supply from opening SMS.

    When ``doubles_supply_gate_follow_up`` is False (NEW enquiry flow), client-supplies detection
    on phrases like "me and my mate" must NOT sound like a reply to a question never asked — use
    the enthusiastic MMF/MFF opener + full doubles SMS instead.

    When True (COLLECTING doubles gate), "Thanks for confirming..." remains appropriate.
    """
    msg = message.strip()
    _doubles_update_type_from_message(msg, phone_number, state_manager, fallback_doubles_type)
    client_supplies, escort_supplies = _doubles_detect_supply_flags(
        msg,
        phone_number,
        fallback_supply_source,
    )
    if client_supplies and not escort_supplies:
        return _doubles_handle_client_supply_response(
            msg,
            phone_number,
            _state,
            state_manager,
            doubles_supply_gate_follow_up,
        )
    if escort_supplies and not client_supplies:
        return _doubles_handle_escort_supply_response(
            msg,
            phone_number,
            _state,
            state_manager,
            doubles_supply_gate_follow_up,
        )
    return _doubles_handle_ambiguous_supply_response(
        msg,
        phone_number,
        _state,
        state_manager,
        doubles_supply_gate_follow_up,
    )



def _merged_booking_snapshot(phone_number: str, state_manager) -> dict[str, Any]:
    st = state_manager.get_state(phone_number) or {}
    bf = state_manager.get_booking_fields(phone_number)
    merged: dict[str, Any] = {**bf}
    merged["phone_number"] = phone_number
    for k in (
        "dinner_client_outside_15km",
        "experience_type",
        "booking_type",
        "duration",
        "client_name",
        "dinner_restaurant",
        "dinner_after_preference",
        "dinner_client_address",
    ):
        if k in st:
            merged[k] = st[k]
    return merged


def _dinner_send_booking_confirmation_and_deposit_flags(
    phone_number: str,
    state_manager,
    *,
    client_home_outside_15km: bool = False,
) -> dict[str, Any]:
    from booking.deposit_handler import calculate_deposit_requirement
    from templates.special_bookings import build_dinner_booking_confirmation_message

    state_manager.update_fields(phone_number, {"dinner_client_outside_15km": client_home_outside_15km})
    merged = _merged_booking_snapshot(phone_number, state_manager)
    dep_req, dep_amt, dep_reason = calculate_deposit_requirement(merged, phone_number, state_manager)
    state_manager.update_fields(
        phone_number,
        {
            "deposit_required": dep_req,
            "deposit_amount": dep_amt,
            "deposit_reason": dep_reason,
            "outcall_awaiting_yes": True,
        },
    )
    merged = _merged_booking_snapshot(phone_number, state_manager)
    msg = build_dinner_booking_confirmation_message(
        merged,
        client_home_outside_15km=client_home_outside_15km,
    )
    return {
        "messages": [msg],
        "new_state": "CHECKING_AVAILABILITY",
        "actions": [],
    }


def _dinner_offered_slot_date_str(state: dict) -> str:
    """Normalize offered_slot_date: may be YYYY-MM-DD str or datetime.date from persistence."""
    raw = state.get("offered_slot_date")
    if raw is None:
        return ""
    if hasattr(raw, "strftime"):
        try:
            return raw.strftime("%Y-%m-%d")
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            return str(raw)[:10]
    return str(raw).strip()[:10]


def _dinner_safe_date_str(value: Any) -> str:
    try:
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d")
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return str(value)[:10]
    return str(value)[:10]


def _dinner_extract_requested_date_time(fields_to_validate: dict) -> tuple[Any, tuple[int, int] | None]:
    d = fields_to_validate.get("date")
    t = fields_to_validate.get("time")
    if not d or t is None or not isinstance(t, (list, tuple)) or len(t) < 2:
        return d, None
    try:
        return d, (int(t[0]), int(t[1]))
    except (TypeError, ValueError):
        return d, None


def _dinner_find_offered_slot_for_hour(
    state: dict,
    date_str: str,
    requested_hour: int,
    *,
    exact_minute: int | None = None,
    require_positive_minute: bool = False,
) -> tuple[int, int] | None:
    offered_date = _dinner_offered_slot_date_str(state)
    if offered_date and date_str != offered_date:
        return None

    hours = state.get("offered_slot_hours") or []
    minutes = state.get("offered_slot_minutes") or []
    for i, offered_hour in enumerate(hours):
        try:
            offered_minute = int(minutes[i]) if minutes and i < len(minutes) else 0
            if int(offered_hour) != requested_hour:
                continue
        except (TypeError, ValueError):
            continue
        if exact_minute is not None and offered_minute != exact_minute:
            continue
        if require_positive_minute and offered_minute <= 0:
            continue
        return requested_hour, offered_minute
    return None


def _dinner_requested_time_matches_offered_slot(state: dict, fields_to_validate: dict) -> bool:
    """
    True when date+time match a slot we already offered (client is picking from our list).

    Skips re-sending the full unavailable SMS if the calendar still reports busy for that
    window — we treat our own offered alternatives as authoritative for SMS UX.
    """
    d, parsed_time = _dinner_extract_requested_date_time(fields_to_validate)
    if not d or parsed_time is None:
        return False
    matched = _dinner_find_offered_slot_for_hour(
        state,
        _dinner_safe_date_str(d),
        parsed_time[0],
        exact_minute=parsed_time[1],
    )
    return matched is not None


def _dinner_snap_time_to_offered_if_same_hour(
    state: dict,
    fields_to_validate: dict,
    state_manager,
    phone_number: str,
) -> dict:
    """
    'how about 8' often parses as 8:00pm (20:00) while we offered 8:15/8:30/8:45.
    Snap on-the-hour to the first offered minute in that hour so we don't re-send
    the full unavailable SMS when the client is clearly picking from our list.
    """
    ftv = dict(fields_to_validate)
    d, parsed_time = _dinner_extract_requested_date_time(ftv)
    if not d or parsed_time is None:
        return ftv

    requested_hour, requested_minute = parsed_time
    if requested_minute != 0:
        return ftv

    matched = _dinner_find_offered_slot_for_hour(
        state,
        _dinner_safe_date_str(d),
        requested_hour,
        require_positive_minute=True,
    )
    if matched is None:
        return ftv

    persist_date = _dinner_offered_slot_date_str(state) or _dinner_safe_date_str(d)
    state_manager.update_fields(
        phone_number,
        {
            "time": matched,
            "date": persist_date,
        },
    )
    ftv["time"] = matched
    ftv["date"] = persist_date
    return ftv


def _dinner_build_requested_booking_details(
    state: dict,
    fields_to_validate: dict,
    duration_minutes: int,
) -> dict[str, Any]:
    restaurant = (state.get("dinner_restaurant") or fields_to_validate.get("outcall_address") or "").strip()
    booking_details: dict[str, Any] = {
        "date": fields_to_validate.get("date"),
        "time": fields_to_validate.get("time"),
        "duration": duration_minutes,
        "incall_outcall": "outcall",
    }
    if restaurant:
        booking_details["outcall_address"] = restaurant
    return booking_details


def _dinner_conflict_type_for_requested_time(
    booking_details: dict[str, Any],
    check_conflict,
    check_outcall_conflict_with_travel,
) -> str:
    try:
        if booking_details.get("outcall_address"):
            conflict_type, _ = check_outcall_conflict_with_travel(booking_details)
        else:
            conflict_type, _ = check_conflict(booking_details)
        return conflict_type
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return "unknown"


def _dinner_find_requested_time_alternatives(booking_details: dict[str, Any], find_alternative_slots) -> list:
    try:
        return list(find_alternative_slots(booking_details, max_results=3))
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return []


def _dinner_format_alternative_line(dt, format_slot_display_short, weekday_abbrev_3) -> str:
    try:
        return format_slot_display_short(dt)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
    try:
        if hasattr(dt, "strftime"):
            return f"{weekday_abbrev_3(dt)} {dt.strftime('%d %b %I:%M%p').replace(' 0', ' ')}"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
    return str(dt)


def _dinner_format_requested_time_alternatives(
    slot_dts: list,
    format_slot_display_short,
    weekday_abbrev_3,
) -> list[str]:
    return [
        _dinner_format_alternative_line(dt, format_slot_display_short, weekday_abbrev_3)
        for dt in slot_dts[:3]
    ]


def _dinner_build_requested_time_unavailable_message(
    phone_number: str,
    state: dict,
    fields_to_validate: dict,
    requested_time,
    slot_dts: list,
    build_dinner_date_requested_time_unavailable_full_message,
    format_slot_display_short,
    weekday_abbrev_3,
    get_current_incall_location,
    get_profile_url,
    safe_format_dinner_date_rates_text,
    get_deposit_outcall,
    get_webform_url,
) -> str:
    loc = get_current_incall_location() or {}
    client_name = (state.get("client_name") or fields_to_validate.get("client_name") or "").strip()
    return build_dinner_date_requested_time_unavailable_full_message(
        client_name=client_name,
        slot_display_lines=_dinner_format_requested_time_alternatives(
            slot_dts,
            format_slot_display_short,
            weekday_abbrev_3,
        ),
        rates_text=safe_format_dinner_date_rates_text(),
        profile_url=(get_profile_url() or "").strip(),
        webform_url=get_webform_url(phone_number),
        city=(loc.get("city") or "").strip(),
        requested_time=requested_time,
        deposit=int(get_deposit_outcall()),
    )


def _dinner_offer_source_datetimes(
    phone_number: str,
    state_manager,
    slot_dts: list,
    get_current_datetime,
    get_next_available_time_slots,
) -> list:
    offer_from = slot_dts[:3]
    if offer_from:
        return offer_from

    now = get_current_datetime()
    try:
        time_slots = get_next_available_time_slots(
            now,
            num_slots=3,
            check_calendar=True,
            booking_type="dinner_date",
            persist_slots_for_phone=phone_number,
            persist_slots_state_manager=state_manager,
        )
        return [dt for dt, _ in time_slots[:3]]
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return []


def _dinner_persist_requested_offer_slots(phone_number: str, state_manager, offer_from: list) -> None:
    updates: dict[str, Any] = {
        "offered_slot_hours": [dt.hour for dt in offer_from[:3]],
        "offered_slot_minutes": [dt.minute for dt in offer_from[:3]],
    }
    if offer_from:
        updates["offered_slot_date"] = offer_from[0].strftime("%Y-%m-%d")
    state_manager.update_fields(phone_number, updates)


def _dinner_maybe_reply_if_requested_time_unavailable(
    phone_number: str,
    state_manager,
    fields_to_validate: dict,
    state: dict,
) -> dict[str, Any] | None:
    """
    Dinner COLLECTING: if date+time are known and the calendar blocks that start time,
    send the same full unavailable SMS as the dinner enquiry handler (alternatives + rates + CTA).
    """
    from config import get_current_incall_location, get_profile_url
    from core.rates_from_config import get_deposit_outcall, safe_format_dinner_date_rates_text
    from core.webform_security import get_webform_url
    from services.calendar_service import check_conflict, check_outcall_conflict_with_travel
    from services.calendar_service import find_alternative_slots
    from templates.special_bookings import build_dinner_date_requested_time_unavailable_full_message
    from utils.availability_slots import format_slot_display_short, get_next_available_time_slots, weekday_abbrev_3
    from utils.dinner_date import DINNER_DURATION_MINUTES
    from utils.timezone import get_current_datetime

    ftv = _dinner_snap_time_to_offered_if_same_hour(
        state, fields_to_validate, state_manager, phone_number
    )
    refreshed = state_manager.get_state(phone_number)
    if isinstance(refreshed, dict):
        state = refreshed

    requested_date = ftv.get("date")
    requested_time = ftv.get("time")
    if not requested_date or not requested_time:
        return None
    if _dinner_requested_time_matches_offered_slot(state, ftv):
        return None

    booking_details = _dinner_build_requested_booking_details(state, ftv, DINNER_DURATION_MINUTES)
    conflict_type = _dinner_conflict_type_for_requested_time(
        booking_details,
        check_conflict,
        check_outcall_conflict_with_travel,
    )
    if conflict_type == "none":
        return None

    slot_dts = _dinner_find_requested_time_alternatives(booking_details, find_alternative_slots)
    msg = _dinner_build_requested_time_unavailable_message(
        phone_number,
        state,
        ftv,
        requested_time,
        slot_dts,
        build_dinner_date_requested_time_unavailable_full_message,
        format_slot_display_short,
        weekday_abbrev_3,
        get_current_incall_location,
        get_profile_url,
        safe_format_dinner_date_rates_text,
        get_deposit_outcall,
        get_webform_url,
    )
    offer_from = _dinner_offer_source_datetimes(
        phone_number,
        state_manager,
        slot_dts,
        get_current_datetime,
        get_next_available_time_slots,
    )
    _dinner_persist_requested_offer_slots(phone_number, state_manager, offer_from)
    return _collecting_response(msg)



def _dinner_verify_client_address_and_confirm(
    message: str,
    phone_number: str,
    state_manager,
) -> dict[str, Any]:
    from booking.outcall_verification import (
        MAX_DISTANCE_KM,
        normalize_outcall_address_for_verification,
        verify_hotel_in_cbd,
    )
    from config import get_current_incall_location
    from templates.special_bookings import get_dinner_client_address_prompt
    from utils.dinner_date import extract_client_address_from_message

    addr = extract_client_address_from_message(message)
    if len(addr.strip()) < 10:
        return {
            "messages": [get_dinner_client_address_prompt()],
            "new_state": "COLLECTING",
            "actions": [],
        }

    loc = get_current_incall_location() or {}
    city = (loc.get("city") or "").strip() or None
    verify_in = normalize_outcall_address_for_verification(addr, city) or addr
    is_ok, verify_msg, hotel_info = verify_hotel_in_cbd(verify_in, city)

    dist = hotel_info.get("distance_km") if hotel_info else None
    outside = False
    if is_ok:
        outside = False
    else:
        if dist is not None:
            try:
                outside = float(dist) > MAX_DISTANCE_KM
            except (TypeError, ValueError):
                outside = False
        if not outside:
            return {
                "messages": [verify_msg or "I couldn't verify that address. Please add suburb."],
                "new_state": "COLLECTING",
                "actions": [],
            }

    state_manager.update_fields(
        phone_number,
        {
            "dinner_client_address": addr,
        },
    )
    return _dinner_send_booking_confirmation_and_deposit_flags(
        phone_number,
        state_manager,
        client_home_outside_15km=outside,
    )


def _dinner_has_requested_time(fields_to_validate: dict) -> bool:
    return bool(fields_to_validate.get("date") and fields_to_validate.get("time"))


def _dinner_extract_restaurant_candidate(
    message: str,
    fields_to_validate: dict,
    looks_like_dinner_food_preference_chat,
    looks_like_restaurant_reply,
    normalize_dinner_venue_name,
) -> str | None:
    oa = (fields_to_validate.get("outcall_address") or "").strip()
    raw_for_venue = oa if oa else (message or "").strip()
    plausible = looks_like_restaurant_reply(message) or (
        bool(oa)
        and 3 <= len(oa) <= 100
        and looks_like_restaurant_reply(oa)
        and not looks_like_dinner_food_preference_chat(oa)
    )
    if not raw_for_venue or not plausible:
        return None

    venue = normalize_dinner_venue_name(raw_for_venue)
    if len(venue) < 3:
        return None
    return venue


def _dinner_verify_restaurant_candidate(venue: str) -> tuple[bool, str, dict[str, Any], str | None]:
    try:
        from booking.outcall_verification import (
            normalize_outcall_address_for_verification,
            verify_hotel_in_cbd,
        )
        from config import get_current_incall_location

        loc = get_current_incall_location() or {}
        city = (loc.get("city") or "").strip() or None
        address_to_verify = normalize_outcall_address_for_verification(venue, city) or venue
        is_ok, verify_msg, hotel_info = verify_hotel_in_cbd(address_to_verify, city)
        return is_ok, verify_msg, hotel_info, None
    except Exception as e:
        logger.warning("Dinner restaurant verification error: %s", e)

    try:
        from core.settings_manager import get_setting

        strict = (get_setting("outcall_verification_strict", "false") or "").lower() == "true"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        strict = False
    if strict:
        return False, "", {}, "Verification failed. Please send the restaurant name with suburb (e.g. Le Pas Sage Unley)."
    return True, "", {}, None


def _dinner_store_restaurant_details(
    phone_number: str,
    state_manager,
    venue: str,
    hotel_info: dict[str, Any],
) -> None:
    state_updates: dict[str, Any] = {
        "dinner_restaurant": venue,
        "outcall_address": venue,
    }
    if hotel_info.get("verified_address"):
        state_updates["_verified_address"] = hotel_info["verified_address"]
    if hotel_info.get("distance_km") is not None:
        state_updates["_verified_distance_km"] = hotel_info["distance_km"]
    state_manager.update_fields(phone_number, state_updates)


def _dinner_restaurant_ack_text(venue: str, hotel_info: dict[str, Any]) -> str:
    dist = hotel_info.get("distance_km")
    formatted = hotel_info.get("verified_address") or venue
    if dist is not None:
        try:
            distance_km = float(dist)
            return f"Perfect — {formatted} is about {distance_km:.1f}km from me (within the 15km radius).\n\n"
        except (TypeError, ValueError):
            return f"Perfect — I've noted {venue} for dinner.\n\n"
    return f"Perfect — I've noted {venue} for dinner.\n\n"


def _dinner_after_restaurant_response(
    phone_number: str,
    state_manager,
    fields_to_validate: dict,
    fresh_state: dict,
    has_when: bool,
    ack: str,
    get_dinner_pick_time_prompt,
    get_dinner_after_prompt,
) -> dict[str, Any]:
    if not has_when:
        return _collecting_response(ack + get_dinner_pick_time_prompt(fresh_state))

    busy = _dinner_maybe_reply_if_requested_time_unavailable(
        phone_number,
        state_manager,
        fields_to_validate,
        fresh_state,
    )
    if busy is not None:
        return busy
    return _collecting_response(get_dinner_after_prompt())


def _dinner_handle_restaurant_collection(
    message: str,
    phone_number: str,
    state_manager,
    fields_to_validate: dict,
    has_when: bool,
    looks_like_dinner_food_preference_chat,
    looks_like_restaurant_reply,
    normalize_dinner_venue_name,
    get_dinner_after_prompt,
    get_dinner_food_preference_quick_reply,
    get_dinner_pick_time_prompt,
    get_dinner_restaurant_prompt,
) -> dict[str, Any]:
    if looks_like_dinner_food_preference_chat(message):
        return _collecting_response(
            get_dinner_food_preference_quick_reply()
            + "\n\n"
            + get_dinner_restaurant_prompt()
        )

    venue = _dinner_extract_restaurant_candidate(
        message,
        fields_to_validate,
        looks_like_dinner_food_preference_chat,
        looks_like_restaurant_reply,
        normalize_dinner_venue_name,
    )
    if not venue:
        return _collecting_response(get_dinner_restaurant_prompt())

    is_ok, verify_msg, hotel_info, strict_msg = _dinner_verify_restaurant_candidate(venue)
    if strict_msg:
        return _collecting_response(strict_msg)
    if not is_ok:
        return _collecting_response(verify_msg or "I couldn't verify that venue. Try adding the suburb.")

    _dinner_store_restaurant_details(phone_number, state_manager, venue, hotel_info)
    fresh_state = state_manager.get_state(phone_number) or {}
    ack = _dinner_restaurant_ack_text(venue, hotel_info)
    return _dinner_after_restaurant_response(
        phone_number,
        state_manager,
        fields_to_validate,
        fresh_state,
        has_when,
        ack,
        get_dinner_pick_time_prompt,
        get_dinner_after_prompt,
    )


def _dinner_parse_after_preference(message: str, parse_dinner_after_preference, looks_like_home_address_line) -> str | None:
    pref = parse_dinner_after_preference(message)
    if not pref and looks_like_home_address_line(message):
        return "client_place"
    return pref


def _dinner_finalize_after_preference(
    pref: str,
    message: str,
    phone_number: str,
    state_manager,
    extract_client_address_from_message,
    get_dinner_client_address_prompt,
) -> dict[str, Any] | None:
    if pref == "hotel":
        return _dinner_send_booking_confirmation_and_deposit_flags(
            phone_number,
            state_manager,
            client_home_outside_15km=False,
        )
    if pref != "client_place":
        return None

    addr = extract_client_address_from_message(message)
    if len(addr.strip()) >= 10:
        return _dinner_verify_client_address_and_confirm(message, phone_number, state_manager)
    return _collecting_response(get_dinner_client_address_prompt())


def _dinner_handle_after_collection(
    message: str,
    phone_number: str,
    state: dict,
    state_manager,
    fields_to_validate: dict,
    extract_client_address_from_message,
    looks_like_home_address_line,
    parse_dinner_after_preference,
    get_dinner_after_prompt,
    get_dinner_client_address_prompt,
) -> dict[str, Any] | None:
    pref = _dinner_parse_after_preference(
        message,
        parse_dinner_after_preference,
        looks_like_home_address_line,
    )
    if pref:
        state_manager.update_fields(phone_number, {"dinner_after_preference": pref})
        return _dinner_finalize_after_preference(
            pref,
            message,
            phone_number,
            state_manager,
            extract_client_address_from_message,
            get_dinner_client_address_prompt,
        )

    current_state = state_manager.get_state(phone_number) or state
    busy = _dinner_maybe_reply_if_requested_time_unavailable(
        phone_number,
        state_manager,
        fields_to_validate,
        current_state,
    )
    if busy is not None:
        return busy
    return _collecting_response(get_dinner_after_prompt())


def _handle_dinner_date_fields_message(
    message: str,
    phone_number: str,
    state: dict,
    state_manager,
    fields_to_validate: dict,
) -> dict | None:
    """Collect restaurant, after-dinner plan, and optional client address for dinner dates."""
    from utils.dinner_date import (
        extract_client_address_from_message,
        looks_like_dinner_food_preference_chat,
        looks_like_home_address_line,
        looks_like_restaurant_reply,
        normalize_dinner_venue_name,
        parse_dinner_after_preference,
    )
    from templates.special_bookings import (
        get_dinner_after_prompt,
        get_dinner_client_address_prompt,
        get_dinner_food_preference_quick_reply,
        get_dinner_pick_time_prompt,
        get_dinner_restaurant_prompt,
    )

    if not is_dinner_date_booking(state):
        return None

    restaurant = (state.get("dinner_restaurant") or "").strip()
    after = (state.get("dinner_after_preference") or "").strip()
    client_addr = (state.get("dinner_client_address") or "").strip()
    has_when = _dinner_has_requested_time(fields_to_validate)

    if not restaurant:
        return _dinner_handle_restaurant_collection(
            message,
            phone_number,
            state_manager,
            fields_to_validate,
            has_when,
            looks_like_dinner_food_preference_chat,
            looks_like_restaurant_reply,
            normalize_dinner_venue_name,
            get_dinner_after_prompt,
            get_dinner_food_preference_quick_reply,
            get_dinner_pick_time_prompt,
            get_dinner_restaurant_prompt,
        )
    if not has_when:
        return None
    if not after:
        return _dinner_handle_after_collection(
            message,
            phone_number,
            state,
            state_manager,
            fields_to_validate,
            extract_client_address_from_message,
            looks_like_home_address_line,
            parse_dinner_after_preference,
            get_dinner_after_prompt,
            get_dinner_client_address_prompt,
        )
    if after == "client_place" and not client_addr:
        return _dinner_verify_client_address_and_confirm(message, phone_number, state_manager)
    return None

