"""
handlers/booking_coll/_provide_field_context.py

CollectingCtx dataclass and outcall keyword tuples for the provide_field pipeline.
"""

import logging
from dataclasses import dataclass, field as dc_field
from typing import Any

logger = logging.getLogger("adella_chatbot.handlers.collecting")

# ---------------------------------------------------------------------------
# Shared keyword sets used by stage functions
# ---------------------------------------------------------------------------

_OUTCALL_KWS = (
    'outcall', 'out call', 'my place', 'my hotel', 'my address', 'my location',
    'my apartment', 'my room', 'my airbnb', 'my unit', 'my suite',
    'come to me', 'come to my', 'come over', 'come see me', 'come and see me', 'see me', 'visit me',
    'staying at', "i'm at", 'im at', 'i am at', 'located at',
    'can you come', 'you come to',
)

_EARLY_OUTCALL_KWS = (
    'outcall', 'out call', 'my place', 'my hotel', 'my address', 'my location',
    'my apartment', 'my room', 'my airbnb', 'my unit', 'my suite',
    'come to me', 'come to my', 'come over', 'come see me', 'come and see me', 'see me', 'visit me',
    'staying at', "i'm at", 'im at', 'i am at', 'located at',
    'can you come', 'you come to',
)


# ---------------------------------------------------------------------------
# Shared context dataclass
# ---------------------------------------------------------------------------

@dataclass
class CollectingCtx:
    """
    Shared mutable context threaded through each stage of _handle_provide_field_impl.

    Immutable fields are set once in from_context().  Mutable fields are
    populated progressively as the chain of stages runs.
    """

    # ── Immutable after construction ─────────────────────────────────────
    phone_number: str
    message: str
    raw_context: dict
    state_manager: Any
    field_collector: Any
    field_validator: Any
    ai_service: Any | None
    db_service: Any | None

    # ── Populated in Stage 1 (patched immediately from context.state) ────
    state: dict = dc_field(default_factory=dict)

    # ── Populated in Stage 5 ────────────────────────────────────────────
    current_fields: dict = dc_field(default_factory=dict)
    is_available_now: bool = False

    # ── Populated in Stage 9 ────────────────────────────────────────────
    extracted: dict = dc_field(default_factory=dict)

    # ── Populated in Stage 13 ───────────────────────────────────────────
    fields_to_validate: dict = dc_field(default_factory=dict)

    # ── Populated in Stage 16 ───────────────────────────────────────────
    valid: bool = False
    errors: list = dc_field(default_factory=list)

    # ── Post-validation (stages 17–23): snapshot after merge + missing list ──
    verified_info: dict | None = None
    updated_fields: dict = dc_field(default_factory=dict)
    missing: list = dc_field(default_factory=list)

    @classmethod
    def from_context(cls, context: dict) -> 'CollectingCtx':
        import config as _config
        from booking.field_collector import FieldCollector
        from booking.field_validator import FieldValidator

        ai_service = context.get('ai_service')
        db_service = context.get('db_service')

        return cls(
            phone_number=context['phone_number'],
            message=context['message'],
            raw_context=context,
            state_manager=context['state_manager'],
            field_collector=FieldCollector(
                _config,
                ai_service=ai_service,
                message_history=context.get('message_history'),
            ),
            field_validator=FieldValidator(_config),
            ai_service=ai_service,
            db_service=db_service,
            state=dict(context.get('state') or {}),
        )
