"""Pipeline stage helpers for provide_field (extract / slot / validate / finish)."""
from __future__ import annotations

from utils.log_sanitize import LOG_SUPPRESSED_FMT

import logging
import re

from core.booking_substates import (
    DOUBLES_SUPPLY_CONFIRMED,
    DOUBLES_SUPPLY_ESCORT,
    DOUBLES_SUPPLY_GATE,
)
from utils.dinner_date import is_dinner_date_booking

from handlers.booking_coll._provide_field_context import CollectingCtx, _OUTCALL_KWS
from handlers.booking_coll._shared import (
    _check_doubles_supply_response,
    calculate_available_now_booking_datetime,
    check_and_format_outside_hours,
)

logger = logging.getLogger("adella_chatbot.handlers.collecting")


def _state_write_failure_response(ctx: CollectingCtx, *, operation: str) -> dict:
    logger.error(
        "State write failed during %s for %s",
        operation,
        ctx.phone_number,
    )
    from templates.errors import get_system_error_message
    return {"messages": [get_system_error_message(ctx.message)], "new_state": None, "actions": []}


def _safe_update_fields(ctx: CollectingCtx, updates: dict, *, operation: str) -> bool:
    ok = bool(ctx.state_manager.update_fields(ctx.phone_number, updates))
    if not ok:
        logger.error(
            "update_fields returned False during %s for %s; updates=%s",
            operation,
            ctx.phone_number,
            updates,
        )
    return ok


# ---------------------------------------------------------------------------
# Stage 1
# ---------------------------------------------------------------------------

def _stage_first_contact_guard(ctx: CollectingCtx) -> dict | None:
    """Ensure first_contact_sent is set; fix silently if missing (version-conflict recovery)."""
    logger.info(f"[COLLECTING] Entering _handle_provide_field_impl for {ctx.phone_number}")
    logger.info(
        f"[COLLECTING] Current state.first_contact_sent={ctx.state.get('first_contact_sent')}, "
        f"incall_outcall={ctx.state.get('incall_outcall')}"
    )
    logger.info(f"[COLLECTING] Message: {ctx.message[:100]}")
    logger.info(f"[COLLECTING] ALL STATE KEYS: {list(ctx.state.keys())}")

    if not ctx.state.get('first_contact_sent'):
        logger.warning(
            f"[COLLECTING] first_contact_sent is False/missing — marking sent and continuing "
            f"(version conflict recovery). Full state: {ctx.state}"
        )
        if not _safe_update_fields(ctx, {'first_contact_sent': True}, operation="stage_first_contact_guard"):
            return _state_write_failure_response(ctx, operation="stage_first_contact_guard")
        ctx.state['first_contact_sent'] = True
    return None


# ---------------------------------------------------------------------------
# Stage 2
# ---------------------------------------------------------------------------

def _stage_fifth_message_block(ctx: CollectingCtx) -> dict | None:
    """After 5 messages with zero booking fields collected, send the enquiry block and reset to NEW."""
    message_count = ctx.state.get('message_count', 0) + 1
    if not _safe_update_fields(ctx, {'message_count': message_count}, operation="stage_fifth_message_block.increment"):
        return _state_write_failure_response(ctx, operation="stage_fifth_message_block.increment")

    if message_count >= 5:
        current_fields = ctx.state_manager.get_booking_fields(ctx.phone_number)
        has_any_fields = any([
            current_fields.get('date'),
            current_fields.get('time'),
            current_fields.get('duration'),
            current_fields.get('experience_type'),
            current_fields.get('incall_outcall'),
        ])
        if not has_any_fields:
            from templates.enquiry_templates import get_fifth_message_block
            block_message = get_fifth_message_block()
            if not _safe_update_fields(ctx, {'message_count': 0}, operation="stage_fifth_message_block.reset"):
                return _state_write_failure_response(ctx, operation="stage_fifth_message_block.reset")
            return {"messages": [block_message], "new_state": "NEW", "actions": []}
        else:
            # Fields are being collected — reset counter so the block doesn't fire
            # on every subsequent message after the threshold is crossed.
            if not _safe_update_fields(ctx, {'message_count': 0}, operation="stage_fifth_message_block.reset"):
                return _state_write_failure_response(ctx, operation="stage_fifth_message_block.reset")
    return None


# ---------------------------------------------------------------------------
# Stage 3
# ---------------------------------------------------------------------------

_CANCEL_DOUBLES_PATTERNS = (
    "dont want doubles",
    "don't want doubles",
    "dont want a doubles",
    "don't want a doubles",
    "dont want couples",
    "don't want couples",
    "dont want a couples",
    "don't want a couples",
    "just solo",
    "solo booking",
    "just me",
    "just one person",
    "not a doubles",
    "not doubles",
    "not couples",
    "cancel doubles",
    "cancel couples",
    "solo only",
    "only me",
    "by myself",
    "on my own",
    "just regular",
    "normal booking",
)

_DOUBLES_CANCEL_CLEAR_FIELDS = (
    "booking_type",
    "experience_type",
    "doubles_type",
    "escort_supply_source",
    "escort_supply_confirmed",
    "booking_status",
)


def _stage_cancel_doubles(ctx: CollectingCtx) -> dict | None:
    """Detect when a client in a doubles/couples flow says they want a solo booking instead.
    Clears all doubles-specific fields so the bot proceeds as a standard solo booking."""
    _bt = (ctx.state.get('booking_type') or '').strip().lower()
    _exp = (ctx.state.get('experience_type') or '').strip().lower()
    _bt_norm = _bt.replace("-", "_").replace(" ", "_")
    _exp_norm = _exp.replace("-", "_").replace(" ", "_")
    _is_doubles_flow = (
        _bt_norm in ('doubles_mff', 'doubles_mmf', 'couples_mff', 'couples_mmf')
        or _exp_norm in ('doubles_mff', 'doubles_mmf', 'couples_mff', 'couples_mmf')
    )
    if not _is_doubles_flow:
        return None

    msg_lower = ctx.message.lower()
    if not any(pat in msg_lower for pat in _CANCEL_DOUBLES_PATTERNS):
        return None

    clear_updates = {field: None for field in _DOUBLES_CANCEL_CLEAR_FIELDS}
    ctx.state_manager.update_fields(ctx.phone_number, clear_updates)

    _name = (ctx.state.get("client_name") or "").strip()
    _np = f" {_name}" if _name else ""
    return {
        "messages": [
            f"No problem{_np}! I've switched this to a solo booking."
            " What time works for you, and how long would you like to book for?"
        ],
        "new_state": "COLLECTING",
        "actions": [],
    }


def _stage_doubles_gate(ctx: CollectingCtx) -> dict | None:
    """For doubles bookings, confirm who supplies the other provider before proceeding."""
    _status = (ctx.state.get('booking_status') or '').strip().lower()
    _bt = (ctx.state.get('booking_type') or '').strip().lower()
    _exp = (ctx.state.get('experience_type') or '').strip().lower()
    _bt_norm = _bt.replace("-", "_").replace(" ", "_")
    _exp_norm = _exp.replace("-", "_").replace(" ", "_")
    _is_doubles_gate_booking = _bt_norm in ('doubles_mff', 'doubles_mmf') or _exp_norm in (
        'doubles_mff',
        'doubles_mmf',
    )
    _needs_supply_gate = _is_doubles_gate_booking and (
        _status == DOUBLES_SUPPLY_GATE
        or (_status == '' and not ctx.state.get('escort_supply_confirmed'))
    )
    if _needs_supply_gate:
        supply_result = _check_doubles_supply_response(
            ctx.message,
            ctx.phone_number,
            ctx.state,
            ctx.state_manager,
            doubles_supply_gate_follow_up=True,
        )
        if supply_result is not None:
            return supply_result
    return None


# ---------------------------------------------------------------------------
# Stage 10 helper (inline calendar check for available-now specific times)
# ---------------------------------------------------------------------------

def _available_now_inline_calendar_check(ctx: CollectingCtx, extracted: dict, arrival_mins, now) -> dict | None:
    """Stage 10: if the client named a specific clock time in available-now mode, check the calendar
    immediately and return a ✅/❌ reply.  Returns None to fall through to normal field collection."""
    extracted_time = extracted.get("time")
    prior_time = ctx.current_fields.get("time")
    if not (arrival_mins is None and extracted_time is not None and extracted_time != prior_time):
        return None

    _merged = {**(ctx.state or {}), **(ctx.current_fields or {}), **(extracted or {})}
    _bt = str(_merged.get("booking_type") or "").strip().lower()
    _exp = str(_merged.get("experience_type") or "").strip().lower()
    _bt_norm = _bt.replace("-", "_").replace(" ", "_")
    _exp_norm = _exp.replace("-", "_").replace(" ", "_")
    _dtype = str(_merged.get("doubles_type") or "").strip().lower()
    _status = str(_merged.get("booking_status") or "").strip().lower()
    _source = str(_merged.get("escort_supply_source") or "").strip().lower()
    _is_doubles = (
        _bt_norm in ("doubles_mff", "doubles_mmf")
        or _exp_norm in ("doubles_mff", "doubles_mmf", "couples_mff")
        or _dtype in ("mff", "mmf")
    )
    if _is_doubles:
        _supply_confirmed = (
            bool(_merged.get("escort_supply_confirmed"))
            or _status in (DOUBLES_SUPPLY_CONFIRMED, DOUBLES_SUPPLY_ESCORT)
            or _source in ("client", "escort")
        )
        if _status == DOUBLES_SUPPLY_GATE or not _supply_confirmed:
            return {
                "messages": [
                    "Before I can check availability, I need to know — will you be bringing "
                    "the other person yourself, or do you need me to organise them for you?"
                ],
                "new_state": None,
                "actions": [],
            }
    _escort_sources_doubles = _is_doubles and (
        _source == "escort" or _status == DOUBLES_SUPPLY_ESCORT
    )

    try:
        from datetime import datetime as _dt

        from utils.time_parser import infer_time_from_hour as _infer_hour
        from utils.availability_slots import get_next_available_time_slots as _get_slots
        from services.calendar_service import check_conflict as _cc

        req_hour, req_min = extracted_time
        req_date, _ = _infer_hour(req_hour, now)
        req_dt = _dt.combine(req_date, _dt.min.time()).replace(hour=req_hour, minute=req_min)
        if now.tzinfo and req_dt.tzinfo is None:
            try:
                req_dt = now.tzinfo.localize(req_dt)
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                req_dt = req_dt.replace(tzinfo=now.tzinfo)
        if _escort_sources_doubles:
            from handlers.booking_coll.doubles_first_turn_compose import _escort_supply_notice_floor_start

            min_start = _escort_supply_notice_floor_start(now)
        else:
            min_start = None
        too_soon_for_escort_source = bool(min_start and req_dt < min_start)

        h12 = req_hour % 12 or 12
        ampm = "am" if req_hour < 12 else "pm"
        time_str = f"{h12}:{req_min:02d}{ampm}" if req_min else f"{h12}{ampm}"

        check = {
            "date": req_date.strftime("%Y-%m-%d"),
            "time": (req_hour, req_min),
            "duration": ctx.current_fields.get("duration") or extracted.get("duration") or 60,
            "incall_outcall": ctx.current_fields.get("incall_outcall") or extracted.get("incall_outcall") or "outcall",
        }
        conflict_type = "busy" if too_soon_for_escort_source else "unknown"
        if not too_soon_for_escort_source:
            conflict_type, _ = _cc(check)

        if conflict_type in ("none", "graphite"):
            extracted["date"] = req_date
            extracted["time"] = (req_hour, req_min)
            if not _safe_update_fields(
                ctx,
                {"time": (req_hour, req_min), "date": req_date},
                operation="available_now_inline_calendar_check.persist_time_date",
            ):
                return _state_write_failure_response(ctx, operation="available_now_inline_calendar_check.persist_time_date")
            is_oc = (ctx.current_fields.get("incall_outcall") or "").lower() == "outcall"
            _dinner_ctx_inline = {**ctx.current_fields, **(ctx.state or {})}
            _is_dinner_inline = is_dinner_date_booking(_dinner_ctx_inline)
            if is_oc and not (ctx.current_fields.get("outcall_address") or extracted.get("outcall_address")):
                if _is_dinner_inline:
                    from templates.special_bookings import get_dinner_restaurant_prompt
                    confirm = f"✅ Yes I'm available at {time_str}!\n\n{get_dinner_restaurant_prompt()}"
                else:
                    from handlers.booking_coll._shared import _get_outcall_policy_amounts
                    from templates.booking_collection_messages import build_outcall_policy_line

                    _surch, _dep = _get_outcall_policy_amounts()
                    _policy = build_outcall_policy_line(
                        surcharge=_surch, deposit_outcall=_dep, city=""
                    )
                    confirm = (
                        f"✅ Yes I'm available at {time_str}!\n\n"
                        f"{_policy}\n"
                        "What's your address and how long would you like? (min 1 hr)"
                    )
            else:
                confirm = f"✅ Yes I'm available at {time_str}! How long would you like to book?"
            ctx.extracted = extracted
            return {"messages": [confirm], "new_state": None, "actions": []}
        else:
            _slot_kwargs = {"start_from": min_start} if min_start is not None else {}
            alt_slots = _get_slots(now, num_slots=3, check_calendar=True, **_slot_kwargs)
            slots_lines = (
                "\n".join(f"\u2022 {s[1]}" for s in alt_slots)
                if alt_slots
                else "No slots currently available"
            )
            if too_soon_for_escort_source and min_start is not None:
                earliest = min_start.strftime("%I:%M %p").lstrip("0")
                deny = (
                    "When I need to organise the other escort, I require a minimum 4 hours notice. "
                    f"The earliest I can offer is {earliest}.\n\n"
                    f"Here are the next available times:\n{slots_lines}"
                )
            else:
                deny = f"❌ Unfortunately I'm not available at {time_str}.\n\nHere are the next available times:\n{slots_lines}"
            ctx.extracted = extracted
            return {"messages": [deny], "new_state": None, "actions": []}
    except Exception as e:
        logger.warning("Inline availability check failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Stages 8+9+10+11 (bundled)
# ---------------------------------------------------------------------------

def _stage_extract_and_enforce(ctx: CollectingCtx) -> dict | None:
    """Stages 8-11 (bundled).

    Stage 8  — available-now hours guard.
    Stage 9  — field extraction + available-now time/duration calculation.
    Stage 10 — inline calendar check when client gives a specific clock time in available-now mode
               (returns early, bypassing Stage 11 — reason for bundling).
    Stage 11 — incall/outcall intent enforcement.

    Populates ctx.extracted on fall-through.
    """
    from utils.timezone import get_current_datetime

    extracted = ctx.field_collector.extract_fields(ctx.message, ctx.current_fields)

    if ctx.is_available_now:
        now = get_current_datetime()

        # Stage 8: outside-hours guard
        _bf_avail_now = {
            **ctx.current_fields,
            "date": now.date(),
            "time": (now.hour, now.minute),
        }
        within, outside_msg, _, _ = check_and_format_outside_hours(
            _bf_avail_now,
            phone_number=ctx.phone_number,
            state_manager=ctx.state_manager,
            hours_setting_default="",
            suppress_time_specific_opener=True,
        )
        if not within:
            if not _safe_update_fields(
                ctx, {"date": None, "time": None}, operation="stage_extract_and_enforce.clear_outside_hours"
            ):
                return _state_write_failure_response(ctx, operation="stage_extract_and_enforce.clear_outside_hours")
            return {"messages": [outside_msg], "new_state": None, "actions": []}

        # Stage 9: available-now time/duration calculation
        msg_lower = ctx.message.lower()
        arrival_mins = None
        for pattern in [
            r"(?:i'?ll\s+be|i'?ll\s+arrive|arrive|be\s+there|be\s+here)\s+(?:in|at)?\s*(\d+)\s*(?:mins?|minutes?)",
            r"(\d+)\s*(?:mins?|minutes?)\s*(?:until|till|to|before|until\s+i\s+arrive)",
            r"(?:in|at)\s*(\d+)\s*(?:mins?|minutes?)",
        ]:
            m = re.search(pattern, msg_lower)
            if m:
                arrival_mins = int(m.group(1))
                break
        if arrival_mins is None:
            if not any(k in msg_lower for k in ("for", "duration", "book", "booking", "session")):
                m = re.search(r"(\d+)\s*(?:mins?|minutes?)", msg_lower)
                if m and int(m.group(1)) <= 120:
                    arrival_mins = int(m.group(1))

        is_outcall_an = (ctx.current_fields.get("incall_outcall") or "").lower() == "outcall"
        outcall_address = extracted.get("outcall_address") or ctx.current_fields.get("outcall_address")
        has_explicit_time = extracted.get("time") is not None or ctx.current_fields.get("time") is not None

        if arrival_mins is not None or not has_explicit_time:
            booking_time = calculate_available_now_booking_datetime(now, arrival_mins, is_outcall=is_outcall_an, outcall_address=outcall_address)
            extracted["time"] = (booking_time.hour, booking_time.minute)
            if "date" not in extracted and not ctx.current_fields.get("date"):
                extracted["date"] = booking_time.date()
        elif "date" not in extracted and not ctx.current_fields.get("date"):
            extracted["date"] = now.date()

        extracted["arrival_time_minutes"] = arrival_mins if arrival_mins is not None else 0
        if "date" not in extracted and not ctx.current_fields.get("date"):
            extracted["date"] = now.date()

        # Stage 10: inline calendar check for specific clock times (may return early)
        result = _available_now_inline_calendar_check(ctx, extracted, arrival_mins, now)
        if result is not None:
            return result

        # Available-now duration parsing (Stage 10 fall-through)
        duration_mins = None
        for pattern in [
            r"(?:for|book(?:\s+you)?)\s+(?:for\s+)?(?:an\s+)?(\d+)\s*(?:hours?|hrs?)",
            r"(?:an\s+)?hour\b",
            r"\b(?:one\s+)?(\d+)\s*(?:hours?|hrs?)",
            r"(\d+)\s*(?:hours?|hrs?)\s*(?:for|session|booking)",
        ]:
            m = re.search(pattern, msg_lower)
            if m:
                try:
                    duration_mins = int(m.group(1)) * 60 if m.lastindex and m.group(1) else 60
                except (ValueError, TypeError):
                    duration_mins = 60
                break
        if duration_mins is None:
            for pat in [
                r"(?:for|duration|book|session)\s+(?:for\s+)?(\d+)\s*(?:mins?|minutes?)",
                r"(\d+)\s*(?:mins?|minutes?)\s*(?:for|session|booking)",
            ]:
                m = re.search(pat, msg_lower)
                if m:
                    val = int(m.group(1))
                    if val != arrival_mins:
                        duration_mins = val
                        break
        if duration_mins is not None:
            extracted["duration"] = duration_mins
        elif (not has_explicit_time and "duration" not in extracted) or (
            extracted.get("duration") and extracted["duration"] <= 30 and arrival_mins is not None
        ):
            extracted["duration"] = 60

    # Hybrid temporal normalization (advisory): only when date is still unresolved.
    if not extracted.get("date") and not ctx.current_fields.get("date"):
        try:
            from services.hybrid_nlp_detector import HybridNLPDetector
            from utils.timezone import get_current_datetime as _now_dt
            from datetime import timedelta as _td_days

            temporal_hint = HybridNLPDetector(ai_service=ctx.ai_service).detect_temporal_intent(
                message=ctx.message,
                state=ctx.state,
                history=ctx.raw_context.get("message_history"),
            )
            if temporal_hint.accepted and temporal_hint.hint is not None:
                urgency = (temporal_hint.hint.urgency or "").strip().lower()
                now_local = _now_dt()
                if urgency == "today":
                    extracted["date"] = now_local.date()
                elif urgency == "tomorrow":
                    extracted["date"] = (now_local + _td_days(days=1)).date()
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)

    # Stage 11: incall/outcall intent enforcement
    msg_lower_oc = ctx.message.lower()
    if any(kw in msg_lower_oc for kw in _OUTCALL_KWS):
        extracted["incall_outcall"] = "outcall"
    elif not ctx.current_fields.get("incall_outcall") and not extracted.get("incall_outcall"):
        _set_from_hybrid = False
        try:
            from services.hybrid_nlp_detector import HybridNLPDetector

            outcall_hint = HybridNLPDetector(ai_service=ctx.ai_service).detect_outcall_venue(
                message=ctx.message,
                state=ctx.state,
                history=ctx.raw_context.get("message_history"),
            )
            if outcall_hint.accepted and outcall_hint.hint is not None:
                mode = (outcall_hint.hint.location_mode or "").strip().lower()
                if mode in ("incall", "outcall"):
                    extracted["incall_outcall"] = mode
                    _set_from_hybrid = True
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        if not _set_from_hybrid and not extracted.get("incall_outcall"):
            dinner_ctx = {**ctx.current_fields, **(ctx.state or {})}
            extracted["incall_outcall"] = "outcall" if is_dinner_date_booking(dinner_ctx) else "incall"

    # Stage 11b: default date when a time was extracted but no date was given or remembered.
    # Non-dinner: "3pm" → assume today. Dinner: never use "today" here — mis-books vs intro Wed 15th;
    # prefer state.offered_slot_date or already-loaded current_fields.date.
    if extracted.get("time") and not extracted.get("date") and not ctx.current_fields.get("date"):
        _dd_ctx = {**ctx.current_fields, **(ctx.state or {})}
        if is_dinner_date_booking(_dd_ctx):
            osd = (ctx.state or {}).get("offered_slot_date")
            if osd:
                try:
                    from datetime import datetime as _dt_dinner

                    extracted["date"] = _dt_dinner.strptime(str(osd)[:10], "%Y-%m-%d").date()
                except Exception as e:
                    logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        else:
            try:
                from datetime import datetime as _dt_mid

                from handlers.booking_coll._shared import _date_for_slot_index

                _tt = extracted.get("time")
                _h = None
                if isinstance(_tt, (tuple, list)) and len(_tt) >= 1:
                    try:
                        _h = int(_tt[0])
                    except (TypeError, ValueError):
                        _h = None
                _oh = (ctx.state or {}).get("offered_slot_hours") or []
                _odates = (ctx.state or {}).get("offered_slot_dates") or []
                _osd = (ctx.state or {}).get("offered_slot_date")
                if _h == 0 and 0 in _oh and (_odates or _osd):
                    try:
                        _idx = list(_oh).index(0)
                        if _odates and _idx < len(_odates):
                            _ds = str(_odates[_idx])[:10]
                        elif _osd:
                            _ds = str(_date_for_slot_index(_oh, _idx, _osd))[:10]
                        else:
                            _ds = ""
                        if _ds:
                            extracted["date"] = _dt_mid.strptime(_ds, "%Y-%m-%d").date()
                    except (ValueError, TypeError, IndexError) as _e_mid:
                        logger.warning(LOG_SUPPRESSED_FMT, _e_mid, exc_info=False)
                if not extracted.get("date"):
                    from utils.timezone import get_current_datetime

                    extracted["date"] = get_current_datetime().date()
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)

    # Stage 11c: dinner date restaurant extraction.
    # Standard address extraction misses restaurant names like "Le Pas Sage".
    # Pattern extraction may leave ``date`` unset until slots/disambiguation fills it in,
    # so do not gate solely on ``extracted.get("date")``.
    # Do NOT require `not extracted.get("time")`: clients often send time + venue
    # in one message (e.g. "6pm and we go to Le Pas Sage"); time would block venue.
    _dinner_ctx = {**ctx.current_fields, **(ctx.state or {})}
    if (
        is_dinner_date_booking(_dinner_ctx)
        and not ctx.current_fields.get("outcall_address")
        and not extracted.get("outcall_address")
    ):
        from utils.dinner_date import normalize_dinner_venue_name

        msg_raw = ctx.message.strip()
        # Apply the same typo fixes to both strings so indices stay aligned when slicing msg_raw.
        msg_aligned = msg_raw
        for _wrong, _right in (
            ("how abiut", "how about"),
            ("what abiut", "what about"),
            ("thiking", "thinking"),
            ("thinkig", "thinking"),
        ):
            msg_aligned = msg_aligned.replace(_wrong, _right)
        msg_l = msg_aligned.lower()
        import re as _re
        _venue = None
        _go = _re.search(r"\b(?:we\s+)?go\s+to\s+(.+)$", msg_l)
        if _go:
            _cand = _go.group(1).strip().rstrip("?.!")
            if _cand and not _re.search(r"\d", _cand) and len(_cand) >= 3:
                _start = msg_l.find(_cand)
                _venue = msg_aligned[_start : _start + len(_cand)] if _start >= 0 else _cand
        if _venue is None:
            # Patterns: "im thinking X", "how about X", "maybe X", "what about X",
            # "at X", "go to X", "let's go to X", "i was thinking X"
            _venue_patterns = [
                r"(?:i(?:'?m|was)\s+thinking(?:\s+of)?|how about|what about|maybe|let'?s?\s+go\s+to|go\s+to|at\s+the|at)\s+(.+)",
                r"^(.+?)(?:\s+(?:restaurant|café|cafe|bistro|bar|place|hotel))?$",
            ]
            for _pat in _venue_patterns:
                _m = _re.search(_pat, msg_l)
                if _m:
                    _candidate = _m.group(1).strip().rstrip("?.!")
                    if _candidate and not _re.search(r"\d", _candidate) and len(_candidate) >= 3:
                        _start = msg_l.find(_candidate)
                        _venue = msg_aligned[_start : _start + len(_candidate)] if _start >= 0 else _candidate
                        break
        if _venue:
            extracted["outcall_address"] = normalize_dinner_venue_name(_venue)

    # Do not persist a venue from extraction when the SMS is cuisine/favourite-food smalltalk.
    if is_dinner_date_booking(_dinner_ctx):
        from utils.dinner_date import looks_like_dinner_food_preference_chat

        if looks_like_dinner_food_preference_chat(ctx.message):
            extracted.pop("outcall_address", None)

    # After-dinner replies echo "your place" from our prompt; _parse_incall_outcall matches
    # r'\byour place\b' as incall and would overwrite dinner outcall on the next persist.
    _dinner_after_ctx = {**ctx.current_fields, **(ctx.state or {})}
    if (
        is_dinner_date_booking(_dinner_after_ctx)
        and (ctx.current_fields.get("dinner_restaurant") or ctx.state.get("dinner_restaurant") or "").strip()
    ):
        from utils.dinner_date import parse_dinner_after_preference

        if parse_dinner_after_preference(ctx.message) and extracted.get("incall_outcall") == "incall":
            extracted.pop("incall_outcall", None)

    ctx.extracted = extracted
    return None
