"""
main_v2/state_machine_bridge.py

Bridge layer: routes an inbound message through the v1 or v2 path depending
on the ``flow_version`` field of the conversation DB row.

Usage (in application.py webhook handler)
------------------------------------------
Replace the current ``router.route(state, intent, context)`` call with:

    from main_v2.state_machine_bridge import dispatch_message
    result = dispatch_message(
        phone_number=phone_number,
        intent=intent,
        legacy_context=context,     # existing dict — unchanged
        router=router,
        state_manager=state_manager,
    )

The function is a drop-in replacement: it returns the same
``{messages, new_state, actions}`` dict that the router always returned.

V2 activation
-------------
Set ``flow_version = 'v2'`` on a conversation_states row to route that
specific phone number through the event-driven path.  All other rows
continue through the existing v1 path untouched.

Safe rollout order
------------------
1. Deploy this file (no behaviour change — all rows are still v1).
2. Mark a single test number v2 in the DB:
       UPDATE conversation_states SET flow_version = 'v2'
       WHERE phone_number = '+61400000000';
3. Validate in staging / shadow mode.
4. Gradually expand: UPDATE ... WHERE ... (rollout %).
5. Once 100% stable, remove the v1 fallback and the bridge.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("escort_chatbot.state_machine_bridge")

# Legacy v1 handlers return an absolute *target* state.  ``core.router.route_v2``
# maps (current_state, new_state) → FSM event via
# ``core.state_machine.target_state_to_event`` — not a static table here, because
# the same target string (e.g. COLLECTING) can require different events from
# NEW (booking_started) vs CHECKING_AVAILABILITY (availability_failed).

_NON_OUTCALL_INTENTS = frozenset({
    'flirt', 'greeting', 'rude_abusive', 'unsafe_request', 'farewell',
    'wrong_number_opt_out', 'opt_out', 'spam', 'link_click',
})

_OUTCALL_KEYWORDS = (
    'outcall', 'out call', 'out-call',
    'my place', 'my home', 'my hotel', 'my apartment', 'my airbnb', 'my room',
    'my address', 'my location', 'my unit', 'my suite', 'my apt',
    'come to me', 'come to my', 'come over', 'come here',
    'come see me', 'come and see me', 'visit me',
    'you visit', 'you come', 'you travel', 'travel to me',
    'can you come', 'can you travel', 'do you travel', 'do you outcall',
    'staying at', "i'm at", 'im at', 'i am at', 'located at',
)


def _apply_offered_slot_override(
    message: str,
    save_fields: dict,
    state_manager,
    phone_number: str,
    legacy_context: dict[str, Any],
) -> None:
    """Overlay time/date from offered slots if the message selects one, modifying save_fields in-place."""
    try:
        _st = state_manager.get_state(phone_number) or legacy_context.get('state') or {}
        _oh = _st.get('offered_slot_hours') or []
        _om = _st.get('offered_slot_minutes') or []
        _od = _st.get('offered_slot_dates') or []
        _osd = _st.get('offered_slot_date')
        if not (_oh and (_od or _osd)):
            return
        from handlers.booking_coll._shared import _match_slot_selection, _date_for_slot_index
        sel_hour = _match_slot_selection(
            message, _oh, offered_minutes=_om or None,
            offered_dates=_od or None, offered_date=_osd,
        )
        if sel_hour is None:
            return
        idx = list(_oh).index(sel_hour)
        sel_min = int(_om[idx]) if _om and idx < len(_om) else 0
        sel_date = _od[idx] if (_od and idx < len(_od)) else _date_for_slot_index(_oh, idx, _osd)
        save_fields['time'] = (sel_hour, sel_min)
        save_fields['date'] = sel_date
        logger.info("[INCALL_ONLY_GUARD] Slot-matched time %02d:%02d for %s", sel_hour, sel_min, phone_number)
    except Exception as _slot_err:
        logger.debug("[INCALL_ONLY_GUARD] slot-match skipped: %s", _slot_err)


def _extract_and_save_incall_fields(
    legacy_context: dict[str, Any],
    state_manager,
    phone_number: str,
) -> None:
    """Extract booking fields from the message, forcing incall mode, before returning refusal."""
    try:
        import config as _cfg_io
        from booking.field_collector import FieldCollector as _FC_io
        _ai_io = legacy_context.get('ai_service')
        _fc_io = _FC_io(_cfg_io, ai_service=_ai_io)
        _cur_io = state_manager.get_booking_fields(phone_number) or {}
        _extracted_io = _fc_io.extract_fields(legacy_context.get('message') or '', _cur_io) or {}
        _extracted_io.pop('incall_outcall', None)
        _extracted_io.pop('outcall_address', None)
        save_fields = {k: v for k, v in _extracted_io.items() if v is not None and v != ''}
        _apply_offered_slot_override(
            legacy_context.get('message') or '', save_fields, state_manager, phone_number, legacy_context,
        )
        save_fields['incall_outcall'] = 'incall'
        state_manager.update_fields(phone_number, save_fields)
    except Exception as _ext_err:
        logger.warning("[INCALL_ONLY_GUARD] field-extract before refusal failed for %s: %s", phone_number, _ext_err)
        try:
            state_manager.update_fields(phone_number, {'incall_outcall': 'incall'})
        except Exception:
            pass


def _build_incall_only_refusal(state_manager, phone_number: str, legacy_context: dict[str, Any]) -> str:
    """Return the appropriate incall-only refusal message based on message count."""
    try:
        _st_io = state_manager.get_state(phone_number) or legacy_context.get('state') or {}
        _mc_io = int(_st_io.get('message_count') or 0)
    except Exception:
        _mc_io = 0
    if _mc_io <= 1:
        return (
            "Sorry, I only do incall bookings at the moment — I'm not available for outcalls.\n\n"
            "If you're ok with that let me know"
        )
    return (
        "Sorry, I only do incall bookings at the moment — I'm not available for outcalls.\n\n"
        "If you'd like to proceed with an incall booking, let me know and I'll continue from where we were."
    )


def _apply_incall_only_guard(
    intent: str,
    legacy_context: dict[str, Any],
    state_manager,
    phone_number: str,
) -> "dict[str, Any] | None":
    """
    If booking_mode is incall_only and the message contains outcall language,
    extract any booking fields (forcing incall) and return a refusal response.
    Returns None if the guard does not apply.
    """
    try:
        from core.settings_manager import get_setting as _gs_io
        booking_mode = (_gs_io('booking_mode') or 'incall_outcall').strip()
    except Exception:
        booking_mode = 'incall_outcall'

    if booking_mode != 'incall_only':
        return None
    if intent in _NON_OUTCALL_INTENTS:
        return None

    msg_lower = (legacy_context.get('message') or '').lower()
    if not any(kw in msg_lower for kw in _OUTCALL_KEYWORDS):
        return None

    _extract_and_save_incall_fields(legacy_context, state_manager, phone_number)
    refusal = _build_incall_only_refusal(state_manager, phone_number, legacy_context)
    logger.info("[INCALL_ONLY_GUARD] Blocked outcall request for %s (intent=%s)", phone_number, intent)
    return {"messages": [refusal], "new_state": "COLLECTING", "actions": []}


def dispatch_message(
    *,
    phone_number: str,
    intent: str,
    legacy_context: dict[str, Any],
    router,          # core.router.Router
    state_manager,   # core.state_manager.StateManager
) -> dict[str, Any]:
    """
    Central dispatch that selects v1 or v2 routing based on ``flow_version``.

    Args:
        phone_number:    Client's phone number.
        intent:          Classified intent string from the classifier.
        legacy_context:  The existing context dict (state, message, etc.).
        router:          Shared ``Router`` instance.
        state_manager:   Shared ``StateManager`` instance.

    Returns:
        Standard response dict: {messages: list[str], new_state: str|None, actions: list}.
    """
    from handlers.new_conv.booking_pivot import refresh_legacy_context_after_collecting_lane_switch

    legacy_context = refresh_legacy_context_after_collecting_lane_switch(
        intent,
        legacy_context,
        state_manager=state_manager,
        phone_number=phone_number,
    )

    guard_result = _apply_incall_only_guard(intent, legacy_context, state_manager, phone_number)
    if guard_result is not None:
        return guard_result

    db_row = legacy_context.get("state") or {}
    row_flow_version: str = (db_row.get("flow_version") or "v1").strip().lower()
    flow_version: str = _resolve_effective_flow_version(row_flow_version)
    current_state: str = db_row.get("current_state", "NEW")

    if flow_version == "v2":
        return _dispatch_v2(
            phone_number=phone_number,
            intent=intent,
            legacy_context=legacy_context,
            db_row=db_row,
            current_state=current_state,
            router=router,
            state_manager=state_manager,
        )

    # ---- v1 path (100% unchanged) ----------------------------------------
    return router.route(current_state, intent, legacy_context)


def _resolve_effective_flow_version(row_flow_version: str) -> str:
    """
    Resolve the runtime dispatch mode for this request.

    Priority:
    1) FLOW_VERSION_DEFAULT env override ("v1"|"v2") → force globally
    2) admin_settings.flow_version_default ("v1"|"v2") → force globally
    3) admin_settings.flow_version_default == "rollout" + percent >= 100 → force v2
    4) admin_settings.flow_version_default == "rollout" + percent <= 0  → force v1
    5) row flow_version (sticky, set at conversation creation) → rollout/sticky mode
    """
    env_default = (os.environ.get("FLOW_VERSION_DEFAULT") or "").strip().lower()
    if env_default in {"v1", "v2"}:
        return env_default

    db_default = ""
    try:
        from core.settings_manager import get_setting

        db_default = (get_setting("flow_version_default") or "").strip().lower()
    except Exception as exc:
        logger.warning("Could not read flow_version_default from settings: %s", exc)

    if db_default in {"v1", "v2"}:
        return db_default

    # Rollout mode: 100% or 0% overrides the sticky row value.
    if db_default == "rollout":
        try:
            from core.settings_manager import get_setting as _gs
            _raw = (_gs("flow_version_v2_rollout_percent") or "").strip()
            if _raw:
                _pct = int(float(_raw))
                _pct = max(0, min(100, _pct))
                if _pct >= 100:
                    return "v2"
                if _pct <= 0:
                    return "v1"
                # Partial rollout → fall through to sticky row value below
        except Exception as exc:
            logger.warning("Could not read flow_version_v2_rollout_percent: %s", exc)

    if row_flow_version in {"v1", "v2"}:
        return row_flow_version
    return "v2"


# ---------------------------------------------------------------------------
# Internal v2 path
# ---------------------------------------------------------------------------

def _dispatch_v2(
    *,
    phone_number: str,
    intent: str,
    legacy_context: dict[str, Any],
    db_row: dict[str, Any],
    current_state: str,
    router,
    state_manager,
) -> dict[str, Any]:
    """Internal helper: build BookingContext and call router.route_v2()."""
    from core.conversation_context import BookingContext
    from core.state_machine import is_valid_state

    # Safety: validate state before handing to v2 path.
    if not is_valid_state(current_state):
        logger.warning(
            "_dispatch_v2: invalid state %r for %s — falling back to v1",
            current_state, phone_number,
        )
        return router.route(current_state, intent, legacy_context)

    booking_ctx = BookingContext.from_db_row(db_row, flow_version="v2")

    # Populate per-request metadata from the legacy context dict.
    booking_ctx.metadata.update({
        k: v for k, v in legacy_context.items()
        if k not in ("state", "phone_number")
    })
    # Mark webhook-driven v2 routing so Router.route_v2 can apply the
    # customer-facing copy variant without changing internal/tooling callers.
    booking_ctx.metadata["apply_v2_copy_variant"] = True
    # Enable hierarchical booking phase updates for live v2 traffic.
    booking_ctx.metadata["apply_hierarchical_booking_phase"] = True

    return router.route_v2(booking_ctx, intent, state_manager)
