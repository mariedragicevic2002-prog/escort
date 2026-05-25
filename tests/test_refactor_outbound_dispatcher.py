from __future__ import annotations

from adapters.sms_outbound_adapter import SMSOutboundAdapter
from app.outbound import OutboundDispatcher, OutboundMessage


def test_outbound_dispatcher_accounts_partial_failures() -> None:
    send_attempts: list[tuple[str, str]] = []
    before_send_bodies: list[str] = []

    def _sender(phone: str, body: str) -> bool:
        send_attempts.append((phone, body))
        return body == "ok"

    dispatcher = OutboundDispatcher(
        adapters=[SMSOutboundAdapter(_sender)],
        before_send=lambda message: before_send_bodies.append(message.body),
    )
    result = dispatcher.dispatch(
        [
            OutboundMessage(channel="sms", recipient="+61400000001", body="ok"),
            OutboundMessage(channel="sms", recipient="+61400000001", body="fail"),
            OutboundMessage(channel="email", recipient="ops@example.com", body="unsupported"),
        ]
    )

    assert result.attempted == 3
    assert result.sent == 1
    assert result.failed == 2
    assert send_attempts == [
        ("+61400000001", "ok"),
        ("+61400000001", "fail"),
    ]
    assert before_send_bodies == ["ok", "fail", "unsupported"]

