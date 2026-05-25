"""
OutboundDispatcher — sends assembled response messages via SMS.

Infrastructure adapter: single responsibility is transport.
No business logic. No state changes.
"""
from __future__ import annotations

import logging
from typing import List

logger = logging.getLogger(__name__)


class OutboundDispatcher:
    """Dispatches outbound SMS messages through the SMS service."""

    def __init__(self, send_fn=None) -> None:
        """
        Args:
            send_fn: Callable(phone, text) -> bool.  Defaults to the
                     production SMS sender resolved at first call.
        """
        self._send_fn = send_fn

    def _resolve_send_fn(self):
        if self._send_fn is None:
            from services.sms_service import send_sms  # type: ignore
            self._send_fn = send_sms
        return self._send_fn

    def dispatch(self, phone: str, messages: List[str]) -> int:
        """
        Send each message to phone.  Returns count of successfully sent messages.
        Never raises — failures are logged.
        """
        if not messages:
            return 0
        send = self._resolve_send_fn()
        sent = 0
        for text in messages:
            try:
                ok = send(phone, text)
                if ok:
                    sent += 1
                else:
                    logger.error(
                        "outbound_dispatcher.send_failed",
                        extra={"phone": phone[:4] + "****", "preview": text[:40]},
                    )
            except Exception:
                logger.exception(
                    "outbound_dispatcher.exception",
                    extra={"phone": phone[:4] + "****"},
                )
        return sent
