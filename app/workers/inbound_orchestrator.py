from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from app.queue.inbound import InboundQueueRecord
from app.runtime.context import InboundSMSMessage


class InboundOrchestrationExecutor(Protocol):
    def execute(self, message: InboundQueueRecord) -> Any:
        ...


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _first_non_empty(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


class RuntimeFacadeInboundOrchestrator:
    """Inbound queue executor that delegates to the runtime orchestration facade."""

    def __init__(self, orchestration_facade: Any) -> None:
        if not hasattr(orchestration_facade, "process_sms"):
            raise ValueError("orchestration_facade must expose process_sms(inbound_message)")
        self._orchestration_facade = orchestration_facade

    def execute(self, message: InboundQueueRecord) -> Any:
        payload = _coerce_mapping(message.payload)
        message_data = _coerce_mapping(payload.get("message_data"))
        request_payload = _coerce_mapping(payload.get("request_payload"))
        headers = _coerce_mapping(payload.get("request_headers"))

        fallback_payload = payload if request_payload == {} else request_payload
        inbound = InboundSMSMessage(
            phone_number=_first_non_empty(
                payload,
                "phone_number",
                "phone",
                "contact",
                "from_number",
                "from",
            ),
            body=_first_non_empty(payload, "body", "message_body", "content", "message", "text"),
            message_data=message_data,
            request_payload=fallback_payload,
            request_id=_first_non_empty(payload, "request_id") or message.message_id,
            request_headers=headers,
            remote_addr=_first_non_empty(payload, "remote_addr", "ip_address"),
        )
        return self._orchestration_facade.process_sms(inbound)
