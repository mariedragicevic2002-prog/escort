"""Queue envelope contracts and durable adapters for ingress/egress processing."""

from app.queue.adapters import (
    DatabaseInboundQueueRepository,
    DatabaseOutboundQueueRepository,
)
from app.queue.inbound import InboundQueueEnvelope, InboundQueueRecord
from app.queue.metadata import QueueMessageMetadata
from app.queue.outbound import OutboundQueueEnvelope, OutboundQueueRecord
from app.queue.providers import (
    DatabaseQueueProvider,
    InboundQueueProvider,
    OutboundQueueProvider,
    QueueProvider,
    resolve_inbound_queue_provider,
    resolve_outbound_queue_provider,
)
from app.queue.repositories import InboundQueueRepository, OutboundQueueRepository
from app.queue.status import (
    QueueDirection,
    QueueStatus,
    can_transition,
    canonical_status,
    outbox_to_queue_status,
    queue_to_outbox_status,
    resolve_retry_or_dead_status,
)

__all__ = [
    "DatabaseInboundQueueRepository",
    "DatabaseOutboundQueueRepository",
    "DatabaseQueueProvider",
    "InboundQueueEnvelope",
    "InboundQueueProvider",
    "InboundQueueRecord",
    "InboundQueueRepository",
    "OutboundQueueEnvelope",
    "OutboundQueueProvider",
    "OutboundQueueRecord",
    "OutboundQueueRepository",
    "QueueProvider",
    "QueueDirection",
    "QueueMessageMetadata",
    "QueueStatus",
    "can_transition",
    "canonical_status",
    "outbox_to_queue_status",
    "queue_to_outbox_status",
    "resolve_inbound_queue_provider",
    "resolve_outbound_queue_provider",
    "resolve_retry_or_dead_status",
]
