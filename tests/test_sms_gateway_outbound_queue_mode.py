from __future__ import annotations

import main_v2.sms_gateway as gateway
from refactor.app.outbound import OutboundQueuePublishResult
from refactor.app.outbound.contracts import OutboundDispatchResult
from refactor.app.runtime.response_composer import ComposedResponse


def test_sms_gateway_uses_sync_dispatch_when_queue_mode_disabled(monkeypatch):
    captured: list[str] = []

    class _StubDispatcher:
        def dispatch(self, messages):
            buffered = list(messages)
            captured.extend(message.body for message in buffered)
            return OutboundDispatchResult(attempted=len(buffered), sent=len(buffered), failed=0)

    monkeypatch.setattr(gateway, "resolve_sms_outbound_delivery_mode", lambda: "sync")
    monkeypatch.setattr(gateway, "_build_sms_outbound_dispatcher", lambda **_kwargs: _StubDispatcher())

    result = gateway._dispatch_sms_outbound(
        phone_number="+61412345678",
        request_id="req-sync-1",
        composed_response=ComposedResponse(messages=["first", "second"]),
    )

    assert result.attempted == 2
    assert result.sent == 2
    assert result.failed == 0
    assert captured == ["first", "second"]


def test_sms_gateway_uses_queue_mode_without_sync_dispatch_when_fully_queued(monkeypatch):
    calls = {"sync_dispatches": 0}

    class _StubDispatcher:
        def dispatch(self, messages):
            _ = list(messages)
            calls["sync_dispatches"] += 1
            return OutboundDispatchResult(attempted=0, sent=0, failed=0)

    monkeypatch.setattr(gateway, "resolve_sms_outbound_delivery_mode", lambda: "queue")
    monkeypatch.setattr(gateway, "_build_sms_outbound_dispatcher", lambda **_kwargs: _StubDispatcher())
    monkeypatch.setattr(
        gateway,
        "_publish_sms_outbound_queue",
        lambda **_kwargs: OutboundQueuePublishResult(
            attempted=2,
            queued=2,
            duplicates=0,
            failed=0,
            queued_indices=(0, 1),
        ),
    )

    result = gateway._dispatch_sms_outbound(
        phone_number="+61412345678",
        request_id="req-queue-1",
        composed_response=ComposedResponse(messages=["first", "second"]),
    )

    assert result.attempted == 2
    assert result.sent == 2
    assert result.failed == 0
    assert calls["sync_dispatches"] == 0


def test_sms_gateway_queue_mode_falls_back_to_sync_for_failed_publish_indices(monkeypatch):
    dispatched: list[str] = []

    class _StubDispatcher:
        def dispatch(self, messages):
            buffered = list(messages)
            dispatched.extend(message.body for message in buffered)
            return OutboundDispatchResult(attempted=len(buffered), sent=len(buffered), failed=0)

    monkeypatch.setattr(gateway, "resolve_sms_outbound_delivery_mode", lambda: "queue")
    monkeypatch.setattr(gateway, "resolve_sms_outbound_queue_sync_fallback", lambda: True)
    monkeypatch.setattr(gateway, "_build_sms_outbound_dispatcher", lambda **_kwargs: _StubDispatcher())
    monkeypatch.setattr(
        gateway,
        "_publish_sms_outbound_queue",
        lambda **_kwargs: OutboundQueuePublishResult(
            attempted=2,
            queued=1,
            duplicates=0,
            failed=1,
            queued_indices=(0,),
            failed_indices=(1,),
        ),
    )

    result = gateway._dispatch_sms_outbound(
        phone_number="+61412345678",
        request_id="req-queue-2",
        composed_response=ComposedResponse(messages=["first", "second"]),
    )

    assert result.attempted == 2
    assert result.sent == 2
    assert result.failed == 0
    assert dispatched == ["second"]
