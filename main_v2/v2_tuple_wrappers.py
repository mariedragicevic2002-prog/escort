"""
Wrap v1 (legacy dict) handlers so they return (event, response) for route_v2.

Used for NEW, CHECKING_AVAILABILITY, and DEPOSIT_REQUIRED so all high-traffic
v2 paths use the native tuple contract; ``target_state_to_event`` is still the
translation layer until pipelines emit events directly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("escort_chatbot.v2_tuple_wrappers")

_MIGRATION_STATES = frozenset({"NEW", "CHECKING_AVAILABILITY", "DEPOSIT_REQUIRED"})


def _legacy_to_v2_tuple(legacy_handler: Callable[..., dict[str, Any] | tuple[str, Any]]) -> Callable:
    def _v2_entry(booking_ctx) -> tuple[str, dict[str, Any]]:
        legacy_ctx: dict[str, Any] = {
            "phone_number": booking_ctx.user_id,
            "state": booking_ctx.booking_data,
            **(getattr(booking_ctx, "metadata", None) or {}),
        }
        result = legacy_handler(legacy_ctx)
        if isinstance(result, tuple) and len(result) == 2:
            return result[0], result[1] if isinstance(result[1], dict) else {
                "messages": [], "new_state": None, "actions": []
            }
        from core.state_machine import target_state_to_event

        raw = (result or {}).get("new_state") if isinstance(result, dict) else None
        event = target_state_to_event(booking_ctx.state, raw)
        clean: dict[str, Any] = dict(result) if isinstance(result, dict) else {
            "messages": [],
            "actions": [],
        }
        for k in list(clean.keys()):
            if k == "new_state":
                del clean[k]
        clean["new_state"] = None
        if "messages" not in clean:
            clean["messages"] = []
        if "actions" not in clean:
            clean["actions"] = []
        return event, clean

    _v2_entry.__name__ = f"v2wrap_{getattr(legacy_handler, '__name__', 'handler')}"
    _v2_entry.__qualname__ = f"v2wrap_{getattr(legacy_handler, '__qualname__', 'handler')}"
    return _v2_entry


def legacy_handler_to_v2_tuple(legacy_handler: Callable[..., dict[str, Any] | tuple[str, Any]]) -> Callable:
    """Public alias so router_registration can wrap ad hoc v1 handlers for route_v2."""
    return _legacy_to_v2_tuple(legacy_handler)


def register_v2_tuple_wrappers_for_migration_states(router) -> None:
    """
    For each (state, intent) in the v1 table where state is NEW, CHECKING, or
    DEPOSIT_REQUIRED, register a v2 entry that returns (event, response).
    """
    n = 0
    for (st, it), h in list(router.dispatch_table.items()):
        if st in _MIGRATION_STATES:
            router.register_v2(st, it, _legacy_to_v2_tuple(h))
            n += 1
    logger.info("register_v2_tuple_wrappers: registered %d v2 tuple handlers for %s", n, _MIGRATION_STATES)
