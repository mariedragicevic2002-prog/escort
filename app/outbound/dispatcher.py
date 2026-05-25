from __future__ import annotations

from collections.abc import Iterable

from app.outbound.contracts import (
    BeforeSendHook,
    OutboundChannelAdapter,
    OutboundDispatchResult,
    OutboundMessage,
)


class OutboundDispatcher:
    """Channel-agnostic outbound dispatcher with per-message accounting."""

    def __init__(
        self,
        *,
        adapters: Iterable[OutboundChannelAdapter] = (),
        before_send: BeforeSendHook | None = None,
    ) -> None:
        self._adapters: dict[str, OutboundChannelAdapter] = {}
        self._before_send = before_send
        for adapter in adapters:
            self.register_adapter(adapter)

    def register_adapter(self, adapter: OutboundChannelAdapter) -> None:
        key = str(getattr(adapter, "channel", "")).strip().lower()
        if not key:
            raise ValueError("adapter channel is required")
        self._adapters[key] = adapter

    def dispatch(self, messages: Iterable[OutboundMessage]) -> OutboundDispatchResult:
        attempted = 0
        sent = 0
        failed = 0
        for message in messages:
            channel = str(message.channel or "").strip().lower()
            adapter = self._adapters.get(channel)
            attempted += 1
            if self._before_send is not None:
                self._before_send(message)
            if adapter is None:
                failed += 1
                continue
            try:
                delivered = bool(adapter.send(message))
            except Exception:
                delivered = False
            if delivered:
                sent += 1
            else:
                failed += 1
        return OutboundDispatchResult(attempted=attempted, sent=sent, failed=failed)

