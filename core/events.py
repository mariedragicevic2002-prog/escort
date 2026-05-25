"""
events.py — Domain event definitions for the Adella conversational AI backend.

These are pure, serialisable data containers.  They carry no behaviour.

Design principles:
  - Frozen dataclasses: immutable after creation.
  - All fields are JSON-serialisable primitives (str, int, float, bool, None, dict).
  - No circular imports: this module must not import from any other project module.
  - Each event has a ``event_type`` class variable used as a discriminator when
    deserialising from a queue / database.

Future:
  These events can be placed on a task queue (ai_task_queue), a Redis stream,
  or a database-backed outbox.  The dispatcher (EventBus) is intentionally
  NOT defined here to keep this module dependency-free.

Usage::

    from core.events import MessageReceived, publish_event

    event = MessageReceived(
        phone_number="+441234567890",
        message_sid="SM123",
        body="Hi, I'd like to book",
    )
    publish_event(event)   # no-op stub until EventBus is wired up
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Base event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DomainEvent:
    """
    Base class for all domain events.

    ``event_id``   — globally unique identifier for this event instance.
    ``occurred_at`` — UTC timestamp of when the event occurred (not queued).
    ``event_type``  — string discriminator for deserialisation.
    """

    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: str = field(
        default_factory=lambda: datetime.datetime.utcnow().isoformat() + "Z"
    )
    event_type: str = field(default="domain_event", init=False)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dict representation."""
        import dataclasses  # noqa: PLC0415

        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Messaging events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MessageReceived(DomainEvent):
    """
    Fired when an inbound SMS (or webhook payload) passes dedup and enters the
    processing pipeline.  Published BEFORE policy checks so all messages are
    observable, even denied ones.

    Supports both the legacy webhook field names and the Clean Architecture
    aliases used by the new application layer.
    """

    phone_number: str = ""
    message_sid: str = ""
    body: str = ""
    gateway_source: str = "webhook"   # "webhook" | "sms_gateway"
    phone: str = ""
    text: str = ""
    intent: str = ""
    state: str = ""
    request_id: str = ""
    event_type: str = field(default="message.received", init=False)

    def __post_init__(self) -> None:
        if self.phone and not self.phone_number:
            object.__setattr__(self, "phone_number", self.phone)
        elif self.phone_number and not self.phone:
            object.__setattr__(self, "phone", self.phone_number)

        if self.text and not self.body:
            object.__setattr__(self, "body", self.text)
        elif self.body and not self.text:
            object.__setattr__(self, "text", self.body)

        if self.request_id and not self.message_sid:
            object.__setattr__(self, "message_sid", self.request_id)
        elif self.message_sid and not self.request_id:
            object.__setattr__(self, "request_id", self.message_sid)


@dataclass(frozen=True)
class MessageDenied(DomainEvent):
    """
    Fired when an inbound message is rejected by the policy gate.
    """

    phone_number: str = ""
    message_sid: str = ""
    deny_reason: str = ""
    http_status: int = 200
    event_type: str = field(default="message.denied", init=False)


@dataclass(frozen=True)
class MessageProcessed(DomainEvent):
    """
    Fired when the full processing pipeline completes (success or recoverable error).
    Carries the outbound message count for observability.
    """

    phone_number: str = ""
    message_sid: str = ""
    matched_fast_path: Optional[str] = None
    outbound_count: int = 0
    escalation_triggered: bool = False
    processing_ms: float = 0.0
    event_type: str = field(default="message.processed", init=False)


# ---------------------------------------------------------------------------
# Booking / scheduling events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BookingRequested(DomainEvent):
    """
    Fired when a client's intent to book is first detected.
    """

    phone_number: str = ""
    requested_date: Optional[str] = None    # ISO date string or free-text
    requested_time: Optional[str] = None
    service_type: Optional[str] = None
    event_type: str = field(default="booking.requested", init=False)


@dataclass(frozen=True)
class BookingConfirmed(DomainEvent):
    """
    Fired when a booking transitions to the 'confirmed' FSM state.
    """

    phone_number: str = ""
    booking_id: Optional[str] = None
    confirmed_date: Optional[str] = None
    confirmed_time: Optional[str] = None
    event_type: str = field(default="booking.confirmed", init=False)


@dataclass(frozen=True)
class BookingCancelled(DomainEvent):
    """
    Fired when a booking is cancelled by either party.
    """

    phone_number: str = ""
    booking_id: Optional[str] = None
    cancelled_by: str = "client"            # "client" | "operator" | "system"
    reason: Optional[str] = None
    event_type: str = field(default="booking.cancelled", init=False)


@dataclass(frozen=True)
class BookingCompleted(DomainEvent):
    """
    Fired when a booking transitions to the 'completed' FSM state.
    """

    phone_number: str = ""
    booking_id: Optional[str] = None
    event_type: str = field(default="booking.completed", init=False)


# ---------------------------------------------------------------------------
# Safety / moderation events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SafetyViolationDetected(DomainEvent):
    """
    Fired when the safety screening layer detects a policy violation.
    Carries sanitised metadata only — never the raw message body.
    """

    phone_number: str = ""
    message_sid: str = ""
    violation_type: str = ""              # e.g. "profanity" | "grooming" | "pii"
    severity: str = "medium"             # "low" | "medium" | "high" | "critical"
    action_taken: str = "blocked"        # "blocked" | "warned" | "escalated"
    event_type: str = field(default="safety.violation_detected", init=False)


@dataclass(frozen=True)
class ProfanitySignalTracked(DomainEvent):
    """
    Fired when a profanity signal is recorded (may not yet breach threshold).
    """

    phone_number: str = ""
    signal_count: int = 0
    threshold: int = 3
    event_type: str = field(default="safety.profanity_signal", init=False)


# ---------------------------------------------------------------------------
# Escalation events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EscalationTriggered(DomainEvent):
    """
    Fired when the escalation engine decides a conversation needs human review.
    """

    phone_number: str = ""
    escalation_reason: str = ""
    current_state: str = ""
    urgency: str = "normal"              # "normal" | "urgent" | "critical"
    event_type: str = field(default="escalation.triggered", init=False)


@dataclass(frozen=True)
class EscalationResolved(DomainEvent):
    """
    Fired when an escalated conversation is resolved by an operator.
    """

    phone_number: str = ""
    resolved_by: str = ""                # operator identifier (not email for PII)
    resolution: str = ""
    event_type: str = field(default="escalation.resolved", init=False)


# ---------------------------------------------------------------------------
# Conversation lifecycle events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConversationStarted(DomainEvent):
    """
    Fired when a new conversation state record is created for a phone number.
    """

    phone_number: str = ""
    flow_version: str = "v2"
    event_type: str = field(default="conversation.started", init=False)


@dataclass(frozen=True)
class ConversationExpired(DomainEvent):
    """
    Fired when a stale conversation is reset (>7 days inactive, no booking in progress).
    """

    phone_number: str = ""
    previous_state: str = ""
    age_days: float = 0.0
    event_type: str = field(default="conversation.expired", init=False)


@dataclass(frozen=True)
class ConversationStateTransitioned(DomainEvent):
    """
    Fired after every successful FSM state transition.
    Provides an immutable audit trail of all state changes.
    """

    phone_number: str = ""
    from_state: str = ""
    to_state: str = ""
    triggering_event: str = ""
    flow_version: str = "v2"
    event_type: str = field(default="conversation.state_transitioned", init=False)


# ---------------------------------------------------------------------------
# Rate-limiting / abuse events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateLimitExceeded(DomainEvent):
    """
    Fired when a phone number exceeds the inbound message rate limit.
    """

    phone_number: str = ""
    limit: int = 0
    window_seconds: int = 0
    event_type: str = field(default="abuse.rate_limit_exceeded", init=False)


@dataclass(frozen=True)
class ClientBlocked(DomainEvent):
    """
    Fired when a message is rejected because the phone number is on the block list.
    """

    phone_number: str = ""
    block_reason: Optional[str] = None
    event_type: str = field(default="abuse.client_blocked", init=False)


# ---------------------------------------------------------------------------
# Replay-attack / security events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayAttackDetected(DomainEvent):
    """
    Fired when a duplicate message_sid is received (replay prevention).
    """

    phone_number: str = ""
    message_sid: str = ""
    original_received_at: Optional[str] = None
    event_type: str = field(default="security.replay_detected", init=False)


@dataclass(frozen=True)
class WebhookSignatureInvalid(DomainEvent):
    """
    Fired when HMAC webhook signature validation fails.
    """

    remote_addr: str = ""
    endpoint: str = ""
    event_type: str = field(default="security.signature_invalid", init=False)


# ---------------------------------------------------------------------------
# Stub event bus (replace with real queue integration)
# ---------------------------------------------------------------------------


def publish_event(event: DomainEvent) -> None:
    """
    Publish a domain event.

    Current implementation: no-op stub that logs the event.
    Replace with ai_task_queue.enqueue(), Redis stream XADD, or similar.

    This function is the single integration point — callers never need to know
    the transport.
    """
    import logging  # noqa: PLC0415

    log = logging.getLogger(__name__)
    log.debug(
        "domain_event event_type=%s event_id=%s",
        event.event_type,
        event.event_id,
    )
