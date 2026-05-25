from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol


@dataclass(frozen=True)
class OutboundMessage:
    channel: str
    recipient: str
    body: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutboundDispatchResult:
    attempted: int = 0
    sent: int = 0
    failed: int = 0


class OutboundChannelAdapter(Protocol):
    channel: str

    def send(self, message: OutboundMessage) -> bool:
        ...


BeforeSendHook = Callable[[OutboundMessage], None]

