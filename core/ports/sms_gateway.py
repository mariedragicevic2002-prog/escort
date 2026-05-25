from typing import Protocol


class SmsGateway(Protocol):
    """Port: send an outbound SMS message."""

    def send_message(self, phone: str, text: str) -> bool: ...
