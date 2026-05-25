from typing import Any, Protocol


class EventPublisher(Protocol):
    """Port: publish a domain event."""

    def publish(self, event: Any) -> None: ...
