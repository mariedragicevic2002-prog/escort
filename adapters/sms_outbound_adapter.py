from __future__ import annotations

from collections.abc import Callable

from app.outbound.contracts import OutboundMessage


class SMSOutboundAdapter:
    channel = "sms"

    def __init__(self, sender: Callable[[str, str], bool]) -> None:
        self._sender = sender

    def send(self, message: OutboundMessage) -> bool:
        return bool(self._sender(message.recipient, message.body))

