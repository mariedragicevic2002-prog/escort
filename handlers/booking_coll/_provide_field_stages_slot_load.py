"""Pipeline stage helpers for provide_field (extract / slot / validate / finish)."""
from __future__ import annotations

from utils.log_sanitize import LOG_SUPPRESSED_FMT

import logging
import re

from config import get_base_url
from core.webform_security import get_webform_url
from templates import field_prompts
from templates.booking_collection_messages import (
    build_slot_reservation_prompt,
    experience_already_set_for_gfe_prompt,
)
from utils.dinner_date import is_dinner_date_booking

from handlers.booking_coll._provide_field_context import CollectingCtx, _EARLY_OUTCALL_KWS
from handlers.booking_coll._shared import (
    _date_for_slot_index,
    _handle_dinner_date_fields_message,
    _match_slot_selection,
)

logger = logging.getLogger("adella_chatbot.handlers.collecting")


def _stage_hybrid_loop_break_shortcut(ctx: CollectingCtx) -> dict | None:
    """
    Hybrid loop-break detector (ambiguity-only) for COLLECTING.

    Only runs when no fields were extracted and message_count suggests a likely loop.
    """
    state_snapshot = ctx.state_manager.get_state(ctx.phone_number) or ctx.state or {}
    if (state_snapshot.get("current_state") or "").strip().upper() != "COLLECTING":
        return None
    try:
        message_count = int(state_snapshot.get("message_count") or 0)
    except (TypeError, ValueError):
        message_count = 0
    if message_count < 2:
        return None

    try:
        from services.hybrid_nlp_detector import HybridNLPDetector

        detector = HybridNLPDetector(ai_service=ctx.ai_service)
        flow_detection = detector.detect_flow_shift(
            message=ctx.message,
            state=state_snapshot,
            history=ctx.raw_context.get("message_history"),
        )
    except Exception as e:
        logger.warning("Hybrid loop-break (COLLECTING) failed: %s", e)
        return None

    label = ""
    if flow_detection.accepted and flow_detection.hint is not None:
        label = (flow_detection.hint.shift_label or "").strip().lower()
    if label == "cancel":
        from handlers.booking_coll._cancel_rates import handle_cancel_booking

        return handle_cancel_booking(ctx.raw_context)
    if label == "modify":
        return {
            "messages": ["No worries — what date and time would you like instead?"],
            "new_state": "COLLECTING",
            "actions": [],
        }

    try:
        deposit_detection = detector.detect_deposit_intent(
            message=ctx.message,
            state=state_snapshot,
            history=ctx.raw_context.get("message_history"),
        )
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        deposit_detection = None

    deposit_label = ""
    if deposit_detection and deposit_detection.accepted and deposit_detection.hint is not None:
        deposit_label = (deposit_detection.hint.intent or "").strip().lower()
    if deposit_label in ("resistance", "question"):
        deposit_required = bool(state_snapshot.get("deposit_required"))
        return {
            "messages": [
                (
                    "A deposit is required for this booking based on the service/policy settings. "
                    "I can explain the amount and next step once your date/time is locked in."
                    if deposit_required
                    else "No stress — deposit rules only apply to specific booking types or policy flags. "
                    "If they apply, I'll explain why before asking for payment."
                )
                + "\n\nFor now, what date and time are you after?"
            ],
            "new_state": None,
            "actions": [],
        }

    try:
        legacy_detection = detector.detect_loop_break(
            message=ctx.message,
            state=state_snapshot,
            history=ctx.raw_context.get("message_history"),
        )
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        legacy_detection = None
    legacy_label = ""
    if legacy_detection and legacy_detection.accepted and legacy_detection.hint is not None:
        legacy_label = (legacy_detection.hint.shift_label or "").strip().lower()
    if not label and legacy_label == "cancel":
        from handlers.booking_coll._cancel_rates import handle_cancel_booking

        return handle_cancel_booking(ctx.raw_context)
    if not label and legacy_label == "modify":
        return {
            "messages": ["No worries — what date and time would you like instead?"],
            "new_state": "COLLECTING",
            "actions": [],
        }
    if not deposit_label and legacy_label == "deposit_resistance":
        deposit_required = bool(state_snapshot.get("deposit_required"))
        return {
            "messages": [
                (
                    "A deposit is required for this booking based on the service/policy settings. "
                    "I can explain the amount and next step once your date/time is locked in."
                    if deposit_required
                    else "No stress — deposit rules only apply to specific booking types or policy flags. "
                    "If they apply, I'll explain why before asking for payment."
                )
                + "\n\nFor now, what date and time are you after?"
            ],
            "new_state": None,
            "actions": [],
        }

    if legacy_label == "frustration":
        try:
            from main_v2.conversation_guards import check_frustration

            frustration_result = check_frustration(
                ctx.message,
                ctx.phone_number,
                state_snapshot,
                ctx.state_manager,
            )
            if frustration_result is not None:
                return frustration_result
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        try:
            wf = get_webform_url(ctx.phone_number)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            wf = f"{get_base_url()}/booking"
        return {
            "messages": [
                "I hear you. To make this smoother, please use the booking webform and I'll pick it up from there:\n"
                f"{wf}"
            ],
            "new_state": None,
            "actions": [],
        }

    return None


# ---------------------------------------------------------------------------
# Stage 6 — slot selection
# ---------------------------------------------------------------------------

def _stage_slot_selection(ctx: CollectingCtx) -> dict | None:
    """
    Stage 6: If the client picks from previously-offered time slots, persist
    the selection and return a slot-reservation prompt.

    Mutates ctx.current_fields in-place when a slot is matched (so downstream
    stages see the updated time/date even when the message also contains
    outcall content and we fall through instead of returning early).
    """
    if ctx.current_fields.get('time'):
        return None  # time already known — skip

    offered_hours = ctx.state.get('offered_slot_hours') or []
    offered_minutes = ctx.state.get('offered_slot_minutes') or []
    offered_dates = ctx.state.get('offered_slot_dates') or []
    offered_date = ctx.state.get('offered_slot_date')

    if not offered_hours or not (offered_dates or offered_date):
        return None

    selected_hour = _match_slot_selection(
        ctx.message,
        offered_hours,
        offered_minutes=offered_minutes or None,
        offered_dates=offered_dates or None,
        offered_date=offered_date,
    )
    if selected_hour is None:
        return None

    try:
        slot_index = list(offered_hours).index(selected_hour)
        selected_minute = int(offered_minutes[slot_index]) if offered_minutes and slot_index < len(offered_minutes) else 0
        if offered_dates and slot_index < len(offered_dates):
            slot_date = offered_dates[slot_index]
        else:
            slot_date = _date_for_slot_index(offered_hours, slot_index, offered_date)
    except (ValueError, IndexError, TypeError):
        selected_minute = 0
        slot_date = offered_date

    ctx.state_manager.update_fields(ctx.phone_number, {
        'time': (selected_hour, selected_minute),
        'date': slot_date,
    })
    ctx.current_fields['time'] = (selected_hour, selected_minute)
    ctx.current_fields['date'] = slot_date

    # Dinner date: never use the generic slot-reservation prompt (duration + GFE/PSE).
    # Dinner is fixed 2h and experience is Dinner Date; fall through so extraction +
    # _handle_dinner_date_fields_message can handle venue + distance + after-dinner ask.
    _bf = ctx.state_manager.get_booking_fields(ctx.phone_number)
    _dinner_merge = {**ctx.current_fields, **(ctx.state or {}), **_bf}
    if is_dinner_date_booking(_dinner_merge):
        return None

    _msg_lower = ctx.message.lower()
    _slot_outcall_kws = (
        'outcall', 'out call', 'my place', 'my hotel', 'my address',
        'my location', 'come to me', 'come to my', 'come over',
        'come see me', 'come and see me', 'see me', 'visit me',
        'staying at', "i'm at", 'im at', 'i am at', 'located at',
        'can you come', 'you come to', 'my address is',
    )
    if any(kw in _msg_lower for kw in _slot_outcall_kws):
        return None  # fall through — let field extraction pick up the address

    h12 = selected_hour % 12 or 12
    ampm = 'am' if selected_hour < 12 else 'pm'
    min_str = f":{selected_minute:02d}" if selected_minute else ""
    time_str = f"{h12}{min_str}{ampm}"
    client_name = ctx.state.get('client_name') or ctx.current_fields.get('client_name') or ''
    _exp_already = experience_already_set_for_gfe_prompt(_dinner_merge)
    _is_outcall_slot = str((_dinner_merge.get("incall_outcall") or "")).lower() == "outcall"
    return {
        "messages": [
            build_slot_reservation_prompt(
                time_str=time_str,
                client_name=client_name,
                experience_already_set=_exp_already,
                is_outcall=_is_outcall_slot,
            )
        ],
        "new_state": None,
        "actions": [],
    }


# ---------------------------------------------------------------------------
# Stage 6b — ordinal slot pick without any offered-slot context (avoid false confirms)
# ---------------------------------------------------------------------------

def _stage_ordinal_pick_without_offered_slots(ctx: CollectingCtx) -> dict | None:
    """Client picks 'the second one' etc. but no numbered slot list exists in state."""
    msg = (ctx.message or "").strip()
    if not msg:
        return None
    if not re.search(
        r"\b(?:i'?ll\s+take\s+)?(?:the\s+)?(?:first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)"
        r"\s+(?:one|slot|time|option|choice)?\b",
        msg,
        re.IGNORECASE,
    ):
        return None
    offered = ctx.state.get("offered_slot_hours") or []
    offered_dates = ctx.state.get("offered_slot_dates") or []
    if offered or offered_dates:
        return None
    return {
        "messages": [
            "I haven't listed numbered times in this thread yet — please send the day and time you want "
            '(for example "Thu 8pm"), or choose one of the times from my earlier message.'
        ],
        "new_state": None,
        "actions": [],
    }


# ---------------------------------------------------------------------------
# Stage 7 — early duration fast path
# ---------------------------------------------------------------------------

def _stage_early_duration_fast_path(ctx: CollectingCtx) -> dict | None:
    """
    Stage 7: If date+time are already collected and the client now provides
    duration (but no outcall intent), persist the update and return the
    incall pre-confirm summary immediately — avoiding the heavier extraction
    loop that would otherwise cause a re-ask.
    """
    _is_outcall_early = (ctx.current_fields.get('incall_outcall') or '').lower() == 'outcall'
    _msg_has_outcall = any(kw in ctx.message.lower() for kw in _EARLY_OUTCALL_KWS)

    if (
        _is_outcall_early
        or _msg_has_outcall
        or not ctx.current_fields.get('time')
        or not ctx.current_fields.get('date')
        or ctx.current_fields.get('duration')
    ):
        return None

    quick_duration = ctx.field_collector._parse_duration(ctx.message)
    if not quick_duration:
        return None

    update_fields = {'duration': quick_duration}

    parsed_exp = ctx.field_collector._parse_experience_type(ctx.message)
    if parsed_exp:
        update_fields['experience_type'] = parsed_exp

    _existing_early = (
        (ctx.state or {}).get("client_name") or ctx.current_fields.get("client_name") or ""
    ).strip()
    if not _existing_early:
        from templates import greetings
        _booking_kw = {
            'gfe', 'pse', 'dgfe', 'yes', 'yep', 'yeah',
            'hr', 'hrs', 'hour', 'hours', 'min', 'mins', 'minute', 'minutes',
            'and', '&', 'for', 'an',
            'how', 'about', 'what', 'when', 'maybe', 'perhaps', 'could', 'can',
            'would', 'like', 'want', 'need', 'book', 'booking', 'at', 'ok', 'okay',
            'actually', 'switch', 'instead', 'scratch', 'sorry', 'rather', 'not',
        }
        _name_parts_early = []
        for _w in ctx.message.strip().split():
            if _w.lower() in _booking_kw or re.match(r'^\d+$', _w):
                break
            if _w.isalpha() and not greetings.is_likely_not_a_name(_w):
                _name_parts_early.append(_w)
            else:
                break
        if _name_parts_early and len(_name_parts_early) <= 2:
            _candidate_name = " ".join(p.capitalize() for p in _name_parts_early)
            if greetings.is_valid_client_name(_candidate_name):
                update_fields['client_name'] = _candidate_name

    ctx.state_manager.update_fields(ctx.phone_number, update_fields)
    all_fields = ctx.state_manager.get_booking_fields(ctx.phone_number)
    client_name = (ctx.state.get('client_name') or all_fields.get('client_name') or '').strip()

    # Mandatory gate: escort-sourced Doubles MMF must ask exploration checklist
    # immediately after duration capture, before any booking summary is sent.
    from handlers.booking_coll._provide_field_stages_finish import (
        _try_mmf_escort_sourced_exploration_gate,
    )

    _mmf_gate = _try_mmf_escort_sourced_exploration_gate(
        phone_number=ctx.phone_number,
        message=ctx.message,
        state=ctx.state,
        updated_fields=all_fields,
        state_manager=ctx.state_manager,
    )
    if _mmf_gate is not None:
        return _mmf_gate

    from templates.booking_reconfirmation import build_incall_preconfirm_summary
    fields_for_summary = {**all_fields, 'phone_number': ctx.phone_number, 'client_name': client_name}

    try:
        webform_url = get_webform_url(ctx.phone_number)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        webform_url = f"{get_base_url()}/booking"

    summary = build_incall_preconfirm_summary(fields_for_summary, webform_url=webform_url)
    if hasattr(ctx.state_manager, "set_awaiting_yes_flags"):
        ctx.state_manager.set_awaiting_yes_flags(ctx.phone_number, is_outcall=False)
    else:
        from utils.timezone import get_current_datetime

        ctx.state_manager.update_fields(
            ctx.phone_number,
            {
                "incall_awaiting_yes": True,
                "outcall_awaiting_yes": False,
                "awaiting_yes_set_at": get_current_datetime().isoformat(),
            },
        )
    return {
        "messages": [summary],
        "new_state": "CHECKING_AVAILABILITY",
        "actions": [],
    }


# ---------------------------------------------------------------------------
# Stages 4+5 — field load + smart defaults
# ---------------------------------------------------------------------------

def _stage_load_fields_and_defaults(ctx: CollectingCtx) -> None:
    """
    Stage 4+5: authoritative field load, state re-fetch, smart defaults.

    Populates ctx.current_fields, ctx.state, and ctx.is_available_now.
    ConversationContext is kept local — it is only used here.
    """
    from core.conversation_context import ConversationContext

    conversation_context = ConversationContext(ctx.db_service) if ctx.db_service else None

    ctx.current_fields = ctx.state_manager.get_booking_fields(ctx.phone_number)
    ctx.state = ctx.state_manager.get_state(ctx.phone_number) or ctx.state
    ctx.is_available_now = bool(ctx.state.get('available_now_requested', False))

    # Dinner: if booking `date` is missing but offered_slot_date matches the intro, hydrate (Stage 11b safety).
    _ld = {**ctx.current_fields, **(ctx.state or {})}
    if is_dinner_date_booking(_ld) and not ctx.current_fields.get("date"):
        osd = (ctx.state or {}).get("offered_slot_date")
        if osd:
            try:
                from datetime import datetime as _dt_seed

                d = _dt_seed.strptime(str(osd)[:10], "%Y-%m-%d").date()
                ctx.current_fields["date"] = d
                ctx.state_manager.update_fields(ctx.phone_number, {"date": d})
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)

    if (
        conversation_context
        and not ctx.current_fields.get('duration')
        and not ctx.current_fields.get('experience_type')
        and not is_dinner_date_booking(ctx.state)
        and not is_dinner_date_booking(ctx.current_fields)
    ):
        smart_defaults = conversation_context.get_smart_defaults(ctx.phone_number)
        if smart_defaults:
            logger.info(f"Applying smart defaults for {ctx.phone_number}: {smart_defaults}")
            for key, value in smart_defaults.items():
                if key not in ctx.current_fields or ctx.current_fields[key] is None:
                    ctx.current_fields[key] = value
                    ctx.state_manager.update_fields(ctx.phone_number, {key: value})


# ---------------------------------------------------------------------------
# Stage 12 — nothing extracted shortcut
# ---------------------------------------------------------------------------

def _stage_nothing_extracted_shortcut(ctx: CollectingCtx) -> dict | None:
    """Stage 12: if nothing was extracted, prompt for missing fields or delegate to availability check."""
    if ctx.extracted:
        return None

    # Confirmation lines like "Harry yes" extract no structured fields but must reach
    # apply_extracted_updates_and_name + mandatory booking stages (not a generic time prompt).
    if re.search(r"\byes\b", (ctx.message or ""), re.IGNORECASE):
        return None

    # MMF escort-sourced mandatory path: when tags are still missing, do not short-circuit
    # to generic reconfirmation. Let later stages parse/persist MMF exploration replies or
    # emit the mandatory prompt/follow-up.
    from booking.mmf_exploration import decode_mmf_exploration_tags, escort_organises_male_for_mmf

    _merged_mmf = {**(ctx.state or {}), **(ctx.current_fields or {})}
    if escort_organises_male_for_mmf(_merged_mmf):
        if not decode_mmf_exploration_tags(_merged_mmf.get("mmf_exploration_tags")):
            return None

    hybrid_loop_break = _stage_hybrid_loop_break_shortcut(ctx)
    if hybrid_loop_break is not None:
        return hybrid_loop_break

    from templates.special_bookings import get_dinner_restaurant_prompt

    missing = ctx.field_collector.get_missing_fields(ctx.current_fields)
    if missing:
        if (
            is_dinner_date_booking(ctx.current_fields)
            and ctx.current_fields.get('date')
            and ctx.current_fields.get('time')
            and missing == ['outcall_address']
            and not (ctx.current_fields.get('dinner_restaurant') or '').strip()
        ):
            return {"messages": [get_dinner_restaurant_prompt()], "new_state": None, "actions": []}
        _merged_miss = {**(ctx.state or {}), **ctx.current_fields}
        _exp_already = experience_already_set_for_gfe_prompt(_merged_miss)
        _is_oc = str(ctx.current_fields.get("incall_outcall") or "").lower() == "outcall"
        prompt = field_prompts.build_missing_fields_message(
            missing,
            context_message=ctx.message,
            experience_already_set=_exp_already,
            is_outcall=_is_oc,
        )
        if prompt:
            return {"messages": [prompt], "new_state": None, "actions": []}
        prompt = field_prompts.get_prompt_for_missing_core_fields(
            missing, experience_already_set=_exp_already, is_outcall=_is_oc
        )
        return {"messages": [prompt], "new_state": None, "actions": []}
    else:
        if (ctx.current_fields.get('incall_outcall') or '').lower() == 'outcall':
            from handlers import availability_check
            ctx.raw_context['state'] = ctx.state_manager.get_state(ctx.phone_number)
            return availability_check.handle_check_availability(ctx.raw_context)
        from templates.booking_reconfirmation import build_booking_reconfirmation
        booking_fields_with_phone = ctx.current_fields.copy()
        booking_fields_with_phone['phone_number'] = ctx.phone_number
        reconfirm_message = build_booking_reconfirmation(booking_fields_with_phone, include_yes_prompt=False)
        return {"messages": [reconfirm_message], "new_state": "CHECKING_AVAILABILITY", "actions": ["check_calendar"]}


# ---------------------------------------------------------------------------
# Stage 13 — build fields_to_validate
# ---------------------------------------------------------------------------

def _persist_dinner_date_time_when_intercept_short_circuits(ctx: CollectingCtx) -> None:
    """
    When _handle_dinner_date_fields_message returns a response, we exit before
    _stage_apply_extracted_updates_and_name — so date/time from this message never hit the DB.
    Next turn then has no time and falls through to generic 'What time works for you?'.
    """
    if not is_dinner_date_booking(ctx.state):
        return
    ftv = ctx.fields_to_validate
    d, t = ftv.get("date"), ftv.get("time")
    if d is None or t is None:
        return
    ctx.state_manager.update_fields(
        ctx.phone_number,
        {"date": d, "time": t},
    )


def _stage_build_fields_to_validate(ctx: CollectingCtx) -> dict | None:
    """Stage 13: build fields_to_validate, fix dinner duration, run dinner-fields intercept."""
    ctx.fields_to_validate = {**ctx.current_fields, **ctx.extracted}
    if is_dinner_date_booking(ctx.state):
        ctx.fields_to_validate["duration"] = 120
        ctx.state_manager.update_fields(ctx.phone_number, {"duration": 120})
    _dinner_block = _handle_dinner_date_fields_message(
        ctx.message, ctx.phone_number, ctx.state, ctx.state_manager, ctx.fields_to_validate
    )
    if _dinner_block is not None:
        _persist_dinner_date_time_when_intercept_short_circuits(ctx)
        return _dinner_block
    return None
