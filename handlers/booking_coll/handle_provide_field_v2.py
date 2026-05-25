"""
handlers/booking_coll/handle_provide_field_v2.py

V2 event-driven wrapper around the existing 23-stage provide_field pipeline.

MIGRATION GUIDE
---------------
This file demonstrates the v2 handler contract: return ``(event, response)``
instead of a plain ``response`` dict.  The underlying pipeline
(_handle_provide_field_impl) is called exactly as before — no stage logic is
moved or altered.

When to use this handler
~~~~~~~~~~~~~~~~~~~~~~~~
Only invoked when ``booking_ctx.flow_version == "v2"`` (set on the DB row).
All existing v1 wiring in ``main_v2/router_registration.py`` is untouched.

The pipeline still returns a legacy absolute ``new_state``; the event is
``core.state_machine.target_state_to_event(booking_ctx.state, new_state)`` so
the same string (e.g. ``COLLECTING``) is not confused across states.

The router then calls ``state_machine.transition(ctx.state, event)`` to get
the authoritative next_state and persists it via ``StateManager.transition``.
Handlers therefore NEVER call ``state_manager.transition()`` directly.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("adella_chatbot.handlers.booking_coll.v2")


def handle_provide_field_v2(
    booking_ctx,  # BookingContext from core.conversation_context
) -> tuple[str, dict[str, Any]]:
    """
    V2 event-driven handler for COLLECTING / provide_field.

    Args:
        booking_ctx: ``BookingContext`` instance (read-only inside handler).

    Returns:
        ``(event, response_dict)`` tuple.  The caller (``Router.route_v2``)
        resolves the next state and persists it — this handler must NOT touch
        the state machine directly.
    """
    # Build a legacy-compatible context dict from the typed BookingContext so
    # the existing 23-stage pipeline runs without any modification.
    legacy_context: dict[str, Any] = {
        "phone_number": booking_ctx.user_id,
        "message":      booking_ctx.metadata.get("message", ""),
        "state":        booking_ctx.booking_data,
        **booking_ctx.metadata,   # intent, media_urls, etc.
    }

    # Delegate to the existing pipeline — zero duplication.
    from handlers.booking_coll._provide_field import _handle_provide_field_impl
    response = _handle_provide_field_impl(legacy_context)

    # Translate the legacy new_state (absolute target) into a declarative event.
    from core.state_machine import target_state_to_event

    raw_new_state = (response or {}).get("new_state")
    event = target_state_to_event(booking_ctx.state, raw_new_state)

    # Strip new_state from response; the router owns state transitions in v2.
    clean_response: dict[str, Any] = {
        "messages": (response or {}).get("messages", []),
        "actions":  (response or {}).get("actions", []),
        "new_state": None,   # Router will overwrite this
    }

    logger.debug(
        "handle_provide_field_v2: phone=%s state=%s pipeline_new_state=%r event=%r",
        booking_ctx.user_id, booking_ctx.state, raw_new_state, event,
    )

    return event, clean_response
