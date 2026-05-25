"""
Router - Dispatch table for state machine routing.
Maps (state, intent) -> handler function.

v2 event-driven path
--------------------
When a DB row has ``flow_version == "v2"`` the router calls ``route_v2()``.
Handlers on that path return ``(event, response_dict)`` instead of a plain
``response_dict``; the router resolves the next state via
``core.state_machine.transition()`` and then persists it with
``StateManager.transition()``.

Backward compatibility
----------------------
All v1 handlers and the existing ``route()`` method are 100% unchanged.
"""

import logging
from collections.abc import Callable
from typing import Any

from templates.errors import get_system_error_message
from templates.router_messages import NO_HANDLER_FOUND
from utils.structured_logging import log_quality_metric, set_observability_context

logger = logging.getLogger("adella_chatbot.router")

_HIERARCHICAL_PHASE_BY_STATE: dict[str, str] = {
    "NEW": "phase_qualification",
    "COLLECTING": "phase_availability",
    "CHECKING_AVAILABILITY": "phase_availability",
    "EXTENDED_ENQUIRY": "phase_screening",
    "MANUAL_REVIEW_PENDING": "phase_screening",
    "DEPOSIT_REQUIRED": "phase_deposit",
    "CONFIRMED": "phase_confirmation",
    "POST_BOOKING": "phase_follow_up",
}
_PHASE_PROTECTED_PREFIXES = ("doubles_supply_", "repeat_guard_")
_PHASE_PROTECTED_EXACT = {"manual_review_pending"}


def _apply_v2_copy_variant(response: dict[str, Any], *, current_state: str, booking_ctx) -> dict[str, Any]:
    """
    Apply a visible v2 wording variant for live webhook traffic so operators can
    distinguish v1/v2 behavior from customer messages.
    """
    metadata = getattr(booking_ctx, "metadata", None) or {}
    if not metadata.get("apply_v2_copy_variant"):
        return response
    if current_state not in {"NEW", "COLLECTING"}:
        return response

    messages = response.get("messages")
    if not isinstance(messages, list) or not messages:
        return response
    first = messages[0]
    if not isinstance(first, str) or not first.strip():
        return response

    rewritten = first
    replacements = (
        (
            "I STRONGLY recommend booking through my webform:",
            "Fastest way to lock this in is my booking form:",
        ),
        (
            "Here are the times I have available",
            "I'm free at these times",
        ),
        (
            "How long would you like to book for, and what's your address?",
            "How long did you want to book for, and what's the hotel name or address?",
        ),
        (
            "How long would you like to book for?",
            "How long did you want to book for?",
        ),
        (
            "What time works for you, and what's your address?",
            "What time suits you, and what's the hotel name or address?",
        ),
        (
            "What time works for you, and how long would you like to book for?",
            "What time suits you and how long did you want to book for?",
        ),
    )
    if rewritten.startswith("Hi"):
        rewritten = f"Hey{rewritten[2:]}"
    for old, new in replacements:
        rewritten = rewritten.replace(old, new)
    rewritten = rewritten.replace(
        "\n\nQuick tip: send date + time + duration in one message for the fastest booking.",
        "",
    )
    rewritten = rewritten.replace(
        "Quick tip: send date + time + duration in one message for the fastest booking.",
        "",
    )
    if rewritten == first:
        return response

    updated = dict(response)
    updated_messages = list(messages)
    updated_messages[0] = rewritten
    updated["messages"] = updated_messages
    return updated


def _inject_hierarchical_booking_phase(
    *,
    updates: dict[str, Any] | None,
    next_state: str,
    booking_ctx,
) -> dict[str, Any] | None:
    """Add a non-breaking hierarchical booking phase marker to booking_status."""
    metadata = getattr(booking_ctx, "metadata", None) or {}
    if not metadata.get("apply_hierarchical_booking_phase"):
        return updates
    existing_status = str((getattr(booking_ctx, "booking_data", {}) or {}).get("booking_status") or "").strip().lower()
    if existing_status and (
        existing_status in _PHASE_PROTECTED_EXACT
        or any(existing_status.startswith(prefix) for prefix in _PHASE_PROTECTED_PREFIXES)
    ):
        return updates
    base = dict(updates or {})
    if "booking_status" in base:
        return base
    phase = _HIERARCHICAL_PHASE_BY_STATE.get(next_state)
    if not phase:
        return base if updates is not None else None
    base["booking_status"] = phase
    return base


class Router:
    """Dispatch table router for state machine."""

    def __init__(self):
        """Initialize router with empty dispatch table."""
        self.dispatch_table: dict[tuple[str, str], Callable] = {}
        # Separate table for v2 event-driven handlers (receive BookingContext, return (event, dict))
        self.v2_dispatch_table: dict[tuple[str, str], Callable] = {}

    def register(self, state: str, intent: str, handler: Callable) -> None:
        """
        Register a handler for a (state, intent) combination.

        Args:
            state: State name (e.g., "COLLECTING")
            intent: Intent name (e.g., "provide_field")
            handler: Handler function
        """
        key = (state, intent)
        if key in self.dispatch_table:
            logger.warning(f"Overwriting handler for {key}")
        self.dispatch_table[key] = handler
        logger.debug(f"Registered handler for {key}")

    def register_v2(self, state: str, intent: str, handler: Callable) -> None:
        """
        Register a v2 event-driven handler for a (state, intent) combination.

        V2 handlers must have the signature:
            handler(booking_ctx: BookingContext) -> tuple[str, dict]

        Args:
            state:   State name (e.g., "COLLECTING")
            intent:  Intent name (e.g., "provide_field")
            handler: V2 handler function
        """
        key = (state, intent)
        if key in self.v2_dispatch_table:
            logger.warning(f"Overwriting v2 handler for {key}")
        self.v2_dispatch_table[key] = handler
        logger.debug(f"Registered v2 handler for {key}")

    def route(self, state: str, intent: str, context: dict[str, Any]) -> dict[str, Any]:
        """
        Route a message to the appropriate handler.

        Args:
            state: Current state
            intent: Classified intent
            context: Context dict with phone_number, message, etc.

        Returns:
            Dict with:
                - messages: List of message strings to send
                - new_state: New state to transition to (optional)
                - actions: List of actions to perform (optional)
        """
        pn = (context or {}).get("phone_number")
        if pn:
            set_observability_context(phone_number=str(pn))
        set_observability_context(state=str(state), intent=str(intent))

        key = (state, intent)

        # Check for exact match
        if key in self.dispatch_table:
            logger.info(f"Routing {key} to handler")
            try:
                return self.dispatch_table[key](context)
            except Exception as e:
                logger.error(f"Handler error for {key}: {e}", exc_info=True)
                msg = get_system_error_message((context or {}).get("message", ""))
                return {
                    "messages": [msg],
                    "new_state": None,
                    "actions": []
                }

        # Check for wildcard state handler (*, intent)
        wildcard_key = ("*", intent)
        if wildcard_key in self.dispatch_table:
            logger.info(f"Routing {key} to wildcard handler {wildcard_key}")
            try:
                return self.dispatch_table[wildcard_key](context)
            except Exception as e:
                logger.error(f"Wildcard handler error for {wildcard_key}: {e}", exc_info=True)
                msg = get_system_error_message((context or {}).get("message", ""))
                return {
                    "messages": [msg],
                    "new_state": None,
                    "actions": []
                }

        # Check for state-level fallback (state, *)
        state_fallback = (state, "*")
        if state_fallback in self.dispatch_table:
            logger.info(f"Routing {key} to state fallback {state_fallback}")
            try:
                return self.dispatch_table[state_fallback](context)
            except Exception as e:
                logger.error(f"State fallback handler error for {state_fallback}: {e}", exc_info=True)
                msg = get_system_error_message((context or {}).get("message", ""))
                return {
                    "messages": [msg],
                    "new_state": None,
                    "actions": []
                }

        # Check for global wildcard fallback (*, *)
        global_fallback = ("*", "*")
        if global_fallback in self.dispatch_table:
            logger.info(f"Routing {key} to global fallback handler")
            try:
                return self.dispatch_table[global_fallback](context)
            except Exception as e:
                logger.error(f"Global fallback handler error for {key}: {e}", exc_info=True)
                msg = get_system_error_message((context or {}).get("message", ""))
                return {
                    "messages": [msg],
                    "new_state": None,
                    "actions": []
                }

        # No handler found
        logger.warning(f"No handler found for {key}")
        return {
            "messages": [NO_HANDLER_FOUND],
            "new_state": None,
            "actions": []
        }

    # ------------------------------------------------------------------
    # v2 event-driven dispatch
    # ------------------------------------------------------------------

    def route_v2(
        self,
        booking_ctx,           # BookingContext from core.conversation_context
        intent: str,
        state_manager,         # StateManager instance
    ) -> dict[str, Any]:
        """
        Event-driven dispatch for flow_version == "v2" conversations.

        Flow
        ----
        1. Resolve handler via the same dispatch table as route().
        2. Call handler(booking_ctx).
           Handler MUST return ``(event: str, response: dict)``.
        3. Call ``state_machine.transition(ctx.state, event)`` → next_state.
        4. Persist next_state via ``state_manager.transition()``.
        5. Mutate ``booking_ctx.state`` to next_state so callers see it.
        6. Return the response dict (with ``new_state`` injected).

        Args:
            booking_ctx:   ``BookingContext`` instance (state lives here).
            intent:        Classified intent string.
            state_manager: ``StateManager`` instance for DB persistence.

        Returns:
            Standard response dict  {messages, new_state, actions}, where
            ``actions`` are opaque tags for observability (not an execution queue).
        """
        from core.state_machine import (
            FsmBridgeError,
            assert_valid_state,
            target_state_to_event,
            transition as sm_transition,
        )

        # Safety guard — crash early in dev, degrade gracefully in prod.
        try:
            assert_valid_state(booking_ctx.state)
        except AssertionError as exc:
            logger.error("route_v2 safety guard: %s", exc)
            booking_ctx.state = "NEW"

        current_state = booking_ctx.state
        user_message = (booking_ctx.metadata or {}).get("message", "")
        set_observability_context(
            phone_number=str(booking_ctx.user_id),
            state=current_state,
            intent=intent,
        )

        # Resolve handler while preserving legacy precedence:
        # exact -> wildcard-state -> state-fallback -> global.
        # For each slot, prefer the v2 implementation when present, otherwise use v1.
        #
        # This avoids a partially migrated v2 state fallback like (NEW, "*")
        # shadowing a more specific v1 wildcard-state route like ("*", "wrong_number_opt_out").
        key = (current_state, intent)
        lookup_order = (
            (key, True),
            (key, False),
            (("*", intent), True),
            (("*", intent), False),
            ((current_state, "*"), True),
            ((current_state, "*"), False),
            (("*", "*"), True),
            (("*", "*"), False),
        )

        handler = None
        is_v2_handler = False
        for candidate_key, from_v2 in lookup_order:
            table = self.v2_dispatch_table if from_v2 else self.dispatch_table
            candidate = table.get(candidate_key)
            if candidate is not None:
                handler = candidate
                is_v2_handler = from_v2
                break

        if handler is None:
            logger.warning("route_v2: no handler found for %s", key)
            return {"messages": [NO_HANDLER_FOUND], "new_state": None, "actions": []}

        try:
            if is_v2_handler:
                result = handler(booking_ctx)
            else:
                # v1 handler in the fallback chain — build legacy context dict.
                legacy_ctx: dict[str, Any] = {
                    "phone_number": booking_ctx.user_id,
                    "state": booking_ctx.booking_data,
                    **booking_ctx.metadata,
                }
                result = handler(legacy_ctx)
        except FsmBridgeError as exc:
            logger.error(
                "route_v2: FsmBridgeError for %s: %s",
                key,
                exc,
                exc_info=True,
                extra={"fsm_bridge_failure": 1, "fsm_state": str(current_state), "intent": str(intent)},
            )
            msg = get_system_error_message((booking_ctx.metadata or {}).get("message", ""))
            return {"messages": [msg], "new_state": None, "actions": []}
        except Exception as exc:
            logger.error("route_v2 handler error for %s: %s", key, exc, exc_info=True)
            msg = get_system_error_message("")
            return {"messages": [msg], "new_state": None, "actions": []}

        # Unwrap (event, response) tuple returned by v2 handlers.
        if isinstance(result, tuple) and len(result) == 2:
            event, response = result
            if not isinstance(event, str):
                logger.error(
                    "route_v2: invalid v2 event type for %s: %s",
                    key,
                    type(event).__name__,
                )
                msg = get_system_error_message(user_message)
                return {"messages": [msg], "new_state": booking_ctx.state, "actions": []}
            if not isinstance(response, dict):
                logger.error(
                    "route_v2: invalid v2 response type for %s: %s",
                    key,
                    type(response).__name__,
                )
                msg = get_system_error_message(user_message)
                return {"messages": [msg], "new_state": booking_ctx.state, "actions": []}
        else:
            # v1 handler returned a plain dict — map absolute target to an FSM event
            # (context-aware; the same new_state string can mean different events).
            logger.info(
                "V1_RETURN_UNDER_V2 state=%s intent=%s handler=%s",
                current_state,
                intent,
                getattr(handler, "__name__", repr(handler)),
                extra={
                    "v1_return_under_v2": 1,
                    "fsm_state": current_state,
                    "intent": intent,
                    "handler_name": getattr(handler, "__name__", None),
                },
            )
            if isinstance(result, dict):
                raw_new_state = result.get("new_state")
                response = result
            else:
                logger.error(
                    "route_v2: invalid legacy handler return type for %s: %s",
                    key,
                    type(result).__name__,
                )
                raw_new_state = None
                msg = get_system_error_message(user_message)
                response = {"messages": [msg], "new_state": None, "actions": []}
            try:
                event = target_state_to_event(current_state, raw_new_state)
            except FsmBridgeError as exc:
                logger.error(
                    "route_v2: FsmBridgeError mapping new_state for %s: %s",
                    key,
                    exc,
                    exc_info=True,
                    extra={"fsm_bridge_failure": 1, "fsm_state": str(current_state), "intent": str(intent)},
                )
                msg = get_system_error_message((booking_ctx.metadata or {}).get("message", ""))
                return {"messages": [msg], "new_state": None, "actions": []}
            if (
                isinstance(raw_new_state, str)
                and raw_new_state
                and raw_new_state != current_state
                and event == "stay"
            ):
                log_quality_metric(
                    "router_v2_bridge_stay",
                    current_state=current_state,
                    raw_new_state=raw_new_state,
                    intent=intent,
                    handler_name=getattr(handler, "__name__", ""),
                )
            logger.debug(
                "route_v2: v1 handler %s returned new_state=%r → event=%r",
                handler.__name__, raw_new_state, event,
            )

        updates = response.pop("updates", None) if isinstance(response, dict) else None
        if updates is not None and not isinstance(updates, dict):
            updates = None
        next_state = sm_transition(current_state, event)
        updates = _inject_hierarchical_booking_phase(
            updates=updates,
            next_state=next_state,
            booking_ctx=booking_ctx,
        )

        db = getattr(state_manager, "db", None)
        has_tx = bool(db and hasattr(db, "transaction"))

        try:
            if has_tx:
                assert db is not None
                with db.transaction() as conn:
                    if updates:
                        updates_ok = state_manager.update_fields(booking_ctx.user_id, updates, conn=conn)
                        if not updates_ok:
                            logger.warning(
                                "route_v2: update_fields returned False for %s — aborting transition",
                                booking_ctx.user_id,
                            )
                            raise RuntimeError(
                                f"route_v2 update_fields failed for {booking_ctx.user_id}"
                            )

                    if next_state != current_state:
                        ok = state_manager.transition(booking_ctx.user_id, next_state, conn=conn)
                        if not ok:
                            logger.warning(
                                "route_v2: StateManager.transition failed for %s (%s→%s) — state NOT persisted",
                                booking_ctx.user_id, current_state, next_state,
                            )
                            raise RuntimeError(
                                f"route_v2 transition failed for {booking_ctx.user_id}: "
                                f"{current_state}->{next_state}"
                            )
                        else:
                            booking_ctx.state = next_state
            else:
                if next_state != current_state:
                    ok = state_manager.transition(booking_ctx.user_id, next_state, updates=updates)
                    if not ok:
                        logger.warning(
                            "route_v2: StateManager.transition failed for %s (%s→%s) — state NOT persisted",
                            booking_ctx.user_id, current_state, next_state,
                        )
                        log_quality_metric(
                            "router_v2_non_tx_transition_failed",
                            phone_number=booking_ctx.user_id,
                            from_state=current_state,
                            to_state=next_state,
                            intent=intent,
                        )
                        msg = get_system_error_message(user_message)
                        return {"messages": [msg], "new_state": booking_ctx.state, "actions": []}
                    else:
                        booking_ctx.state = next_state
                elif updates:
                    updates_ok = state_manager.update_fields(booking_ctx.user_id, updates)
                    if not updates_ok:
                        logger.warning(
                            "route_v2: update_fields returned False for %s — state unchanged",
                            booking_ctx.user_id,
                        )
                        log_quality_metric(
                            "router_v2_non_tx_updates_failed",
                            phone_number=booking_ctx.user_id,
                            state=current_state,
                            intent=intent,
                        )
                        msg = get_system_error_message(user_message)
                        return {"messages": [msg], "new_state": booking_ctx.state, "actions": []}
        except Exception as exc:
            logger.error(
                "route_v2: transactional write failure for %s: %s",
                booking_ctx.user_id, exc, exc_info=True,
            )
            log_quality_metric(
                "router_v2_write_exception",
                phone_number=booking_ctx.user_id,
                state=current_state,
                intent=intent,
                error=type(exc).__name__,
            )
            msg = get_system_error_message(user_message)
            return {"messages": [msg], "new_state": booking_ctx.state, "actions": []}

        if isinstance(response, dict):
            response = _apply_v2_copy_variant(
                response,
                current_state=current_state,
                booking_ctx=booking_ctx,
            )
            response["new_state"] = booking_ctx.state

        return response

    def get_all_routes(self) -> dict[tuple[str, str], str]:
        """
        Get all registered routes.

        Returns:
            Dict mapping (state, intent) to handler function name
        """
        return {
            key: handler.__name__ for key, handler in self.dispatch_table.items()
        }

    def print_routes(self) -> None:
        """Print all registered routes (for debugging)."""
        print("\n=== Router Dispatch Table ===")
        for (state, intent), handler in sorted(self.dispatch_table.items()):
            print(f"  ({state:20s}, {intent:25s}) -> {handler.__name__}")
        print(f"\nTotal routes: {len(self.dispatch_table)}\n")
