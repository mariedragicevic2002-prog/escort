"""
inbound_context.py — Typed contracts for the inbound processing pipeline.

Defines the immutable input and mutable processing-context dataclasses that
flow through every pipeline stage.

Design:
  - InboundMessage is frozen: stages cannot corrupt the original input.
  - ProcessingContext is mutable and gradually enriched by each stage.
  - ProcessingResult is the typed output returned to the caller.
  - No business logic; pure data containers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Immutable input
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InboundMessage:
    """
    Immutable representation of a single inbound SMS/webhook message.

    Constructed once at the ingress edge; never mutated by pipeline stages.
    """

    from_number: str        # E.164 sender number, e.g. "+447700900000"
    body: str               # Raw message text
    message_sid: str        # Provider message ID (idempotency key)
    raw_payload: Dict       # Full unmodified provider payload


# ---------------------------------------------------------------------------
# Mutable processing context — enriched incrementally by pipeline stages
# ---------------------------------------------------------------------------


@dataclass
class ProcessingContext:
    """
    Processing context that flows through the pipeline.

    Required at construction:
      - message:  the inbound payload
      - services: the injected service bundle

    Populated by later stages:
      - state:                 loaded by state-bootstrap stage
      - history:               loaded by history stage
      - classification:        set by classifier stage
      - pending_state_transition: set by dispatch/handler stage
    """

    message: InboundMessage
    services: Any

    # Populated by pipeline stages
    state: Optional[Dict] = field(default=None)
    history: List = field(default_factory=list)
    classification: Optional[Dict] = field(default=None)
    pending_state_transition: Optional[str] = None


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ProcessingResult:
    """
    Typed result returned from MessageProcessor.process().

    deny:               set when the pipeline was rejected before processing.
    context:            the enriched context after a full pipeline run.
    outbound_messages:  list of messages to send to the user.
    matched_fast_path:  name of the fast-path handler that matched, if any.
    """

    deny: Optional[Any] = None          # Optional[PolicyDeny] — avoids circular import
    context: Optional[ProcessingContext] = None
    outbound_messages: List = field(default_factory=list)
    matched_fast_path: Optional[str] = None
