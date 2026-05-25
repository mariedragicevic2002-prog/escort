# ruff: noqa: F401,F403,F405
from handlers.new_conv._shared import *  # noqa: F401,F403
from typing import Any

import logging

from core.booking_substates import DOUBLES_SUPPLY_GATE
from utils.log_sanitize import LOG_SUPPRESSED_FMT

logger = logging.getLogger("adella_chatbot.enquiries")


def handle_doubles_enquiry(context: dict[str, Any]) -> dict[str, Any]:
    """Handle doubles/threesome enquiry with MMF prioritization + threesome clarification."""
    state = context.get("state") or {}
    state_manager = context["state_manager"]
    phone_number = context["phone_number"]
    message_text = (context.get("message") or "").strip()
    from utils.time_parser import is_immediate_request

    if state.get("profanity_detected") and not state.get("deposit_required"):
        from booking.deposit_handler import build_deposit_gate_response

        booking_fields = state_manager.get_booking_fields(phone_number) or {}
        booking_fields.setdefault("incall_outcall", (state.get("incall_outcall") or "incall"))
        deposit_gate = build_deposit_gate_response(
            booking_fields=booking_fields,
            phone_number=phone_number,
            state_manager=state_manager,
            client_name=(state.get("client_name") or None),
            preamble="Before we continue with this booking, a deposit is required.",
            default_reason="profanity",
            default_amount=100,
        )
        if deposit_gate is not None:
            return deposit_gate

    immediate_requested = is_immediate_request(message_text)
    inferred_outcall = _has_outcall_intent(message_text)
    if not inferred_outcall and str(state.get("incall_outcall") or "").strip().lower() != "outcall":
        try:
            from services.hybrid_nlp_detector import HybridNLPDetector

            outcall_hint = HybridNLPDetector(ai_service=context.get("ai_service")).detect_outcall_venue(
                message=message_text,
                state=state,
                history=context.get("message_history"),
            )
            if outcall_hint.accepted and outcall_hint.hint is not None:
                inferred_outcall = (
                    (outcall_hint.hint.location_mode or "").strip().lower() == "outcall"
                )
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)

    if not immediate_requested:
        try:
            from services.hybrid_nlp_detector import HybridNLPDetector

            temporal_hint = HybridNLPDetector(ai_service=context.get("ai_service")).detect_temporal_intent(
                message=message_text,
                state=state,
                history=context.get("message_history"),
            )
            if temporal_hint.accepted and temporal_hint.hint is not None:
                immediate_requested = (
                    (temporal_hint.hint.urgency or "").strip().lower() == "asap"
                )
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)

    if state.get("first_contact_sent"):
        from handlers.new_conv.booking_pivot import clear_incompatible_flow_for_special_booking_pivot

        bt = (state.get("booking_type") or "").strip().lower()
        if bt not in ("doubles_mff", "doubles mmf"):
            clear_incompatible_flow_for_special_booking_pivot(state_manager, phone_number)
            context = dict(context)
            context["state"] = state_manager.get_state(phone_number) or state
            state = context["state"]
        else:
            if immediate_requested:
                state_manager.update_fields(
                    phone_number,
                    {
                        "available_now_requested": True,
                        "incall_outcall": "outcall" if inferred_outcall else "incall",
                    },
                )
                context = dict(context)
                context["state"] = state_manager.get_state(phone_number) or state

            from handlers import booking_collection

            return booking_collection.handle_provide_field(context) or {
                "messages": [],
                "new_state": "COLLECTING",
                "actions": [],
            }

    from config import get_current_incall_location, get_profile_url
    from core.classifier import classify_doubles_signal
    from core.webform_security import get_webform_url
    from handlers.booking_coll._shared import (
        _check_doubles_supply_response,
        infer_doubles_type_hint_from_message,
    )
    from templates.special_bookings import get_threesome_clarification_template

    doubles_signal = classify_doubles_signal(message_text.lower())
    hybrid_supply_fallback: str | None = None
    hybrid_doubles_type_fallback: str | None = None
    if doubles_signal == "ambiguous_threesome":
        try:
            from services.hybrid_nlp_detector import HybridNLPDetector

            hybrid_hint = HybridNLPDetector(ai_service=context.get("ai_service")).detect_doubles(
                message=message_text,
                state=state,
                history=context.get("message_history"),
            )
            if hybrid_hint.accepted and hybrid_hint.hint is not None:
                existing_dtype = (state.get("doubles_type") or "").strip().lower()
                predicted_dtype = (hybrid_hint.hint.doubles_type or "").strip().lower()
                if predicted_dtype in ("mmf", "mff") and (not existing_dtype or existing_dtype == predicted_dtype):
                    hybrid_doubles_type_fallback = predicted_dtype
                    doubles_signal = "mmf_explicit" if predicted_dtype == "mmf" else "mff_explicit"

                existing_supply = (state.get("escort_supply_source") or "").strip().lower()
                predicted_supply = (hybrid_hint.hint.escort_supply_source or "").strip().lower()
                if (
                    predicted_supply in ("client", "escort")
                    and (not existing_supply or existing_supply == predicted_supply)
                ):
                    hybrid_supply_fallback = predicted_supply
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        if not hybrid_doubles_type_fallback:
            lexical_hint = infer_doubles_type_hint_from_message(message_text)
            if lexical_hint in ("mmf", "mff"):
                hybrid_doubles_type_fallback = lexical_hint
                doubles_signal = "mmf_explicit" if lexical_hint == "mmf" else "mff_explicit"
        if hybrid_supply_fallback is None:
            try:
                from services.hybrid_nlp_detector import HybridNLPDetector

                supply_hint = HybridNLPDetector(ai_service=context.get("ai_service")).detect_doubles_supply_clarity(
                    message=message_text,
                    state=state,
                    history=context.get("message_history"),
                )
                if supply_hint.accepted and supply_hint.hint is not None:
                    src = (supply_hint.hint.escort_supply_source or "").strip().lower()
                    if src in ("client", "escort"):
                        hybrid_supply_fallback = src
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
    if hybrid_supply_fallback is None and doubles_signal in ("mmf_explicit", "mff_explicit", "ambiguous_threesome"):
        try:
            from services.hybrid_nlp_detector import HybridNLPDetector

            supply_hint = HybridNLPDetector(ai_service=context.get("ai_service")).detect_doubles_supply_clarity(
                message=message_text,
                state=state,
                history=context.get("message_history"),
            )
            if supply_hint.accepted and supply_hint.hint is not None:
                src = (supply_hint.hint.escort_supply_source or "").strip().lower()
                if src in ("client", "escort"):
                    hybrid_supply_fallback = src
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)

    client_name = state.get("client_name") or greetings.extract_client_name(message_text)
    location = get_current_incall_location() or {}

    webform_url = get_webform_url(phone_number)

    updates: dict[str, Any] = {
        "first_contact_sent": True,
        "escort_supply_confirmed": False,
        "escort_supply_source": None,
        "booking_status": DOUBLES_SUPPLY_GATE,
    }
    if client_name:
        updates["client_name"] = client_name
    if doubles_signal == "mmf_explicit":
        updates["doubles_type"] = "mmf"
        updates["booking_type"] = "Doubles MMF"
        updates["experience_type"] = "Doubles MMF"
    elif doubles_signal == "mff_explicit":
        updates["doubles_type"] = "mff"
        updates["booking_type"] = "doubles_mff"
        updates["experience_type"] = "doubles_mff"
    else:
        existing_dtype = (state.get("doubles_type") or "").strip().lower()
        fallback_dtype = (hybrid_doubles_type_fallback or existing_dtype).strip().lower()
        if fallback_dtype == "mmf":
            updates["doubles_type"] = "mmf"
            updates["booking_type"] = "Doubles MMF"
            updates["experience_type"] = "Doubles MMF"
        elif fallback_dtype == "mff":
            updates["doubles_type"] = "mff"
            updates["booking_type"] = "doubles_mff"
            updates["experience_type"] = "doubles_mff"
    if immediate_requested:
        updates["available_now_requested"] = True
    if inferred_outcall:
        updates["incall_outcall"] = "outcall"
    elif immediate_requested and doubles_signal in ("mmf_explicit", "mff_explicit"):
        updates["incall_outcall"] = "incall"

    state_manager.update_fields(phone_number, updates)
    gate_state = {**state, **updates}

    has_confirmed_doubles_experience = (
        (state.get("booking_type") or "").strip().lower() in ("couples_booking", "doubles_mff", "doubles mmf")
        or (state.get("experience_type") or "").strip().lower() in ("couples_mff", "doubles_mff", "doubles mmf")
        or (state.get("doubles_type") or "").strip().lower() in ("mff", "mmf")
    )
    if doubles_signal == "ambiguous_threesome" and not has_confirmed_doubles_experience:
        clarify_msg = get_threesome_clarification_template(
            client_name=client_name or "",
            webform_url=webform_url,
        )
        return {
            "messages": [clarify_msg],
            "new_state": "COLLECTING",
            "actions": [],
        }

    if doubles_signal in ("mmf_explicit", "mff_explicit"):
        supply_result = _check_doubles_supply_response(
            message_text,
            phone_number,
            gate_state,
            state_manager,
            doubles_supply_gate_follow_up=False,
            fallback_supply_source=hybrid_supply_fallback,
            fallback_doubles_type=hybrid_doubles_type_fallback,
        )
        if supply_result is not None:
            return supply_result
        from handlers import booking_collection

        collecting_context = dict(context)
        collecting_context["state"] = state_manager.get_state(phone_number) or gate_state
        return booking_collection.handle_provide_field(collecting_context) or {
            "messages": ["Perfect — what date and time were you thinking?"],
            "new_state": "COLLECTING",
            "actions": [],
        }

    supply_result = _check_doubles_supply_response(
        message_text,
        phone_number,
        gate_state,
        state_manager,
        doubles_supply_gate_follow_up=False,
        fallback_supply_source=hybrid_supply_fallback,
        fallback_doubles_type=hybrid_doubles_type_fallback,
    )
    if supply_result is not None:
        return supply_result

    from handlers import booking_collection

    collecting_context = dict(context)
    collecting_context["state"] = state_manager.get_state(phone_number) or gate_state
    return booking_collection.handle_provide_field(collecting_context) or {
        "messages": ["Perfect — what date and time were you thinking?"],
        "new_state": "COLLECTING",
        "actions": [],
    }
