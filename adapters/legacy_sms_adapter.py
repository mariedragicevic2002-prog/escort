from __future__ import annotations

from collections.abc import Callable


class LegacySMSProcessorAdapter:
    """Adapter for the existing sms_gateway processing function."""

    def __init__(self, processor: Callable[[str, str], list[str]]) -> None:
        self._processor = processor

    def process(self, phone_number: str, message_body: str) -> list[str]:
        return list(self._processor(phone_number, message_body) or [])

