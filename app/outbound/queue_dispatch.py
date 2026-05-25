from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from typing import Any, Mapping, Protocol
import uuid

from app.outbound.contracts import OutboundMessage
from app.queue import (
    OutboundQueueEnvelope,
    QueueMessageMetadata,
)
from app.queue.providers import OutboundQueueProvider

OUTBOUND_SMS_EVENT_TYPE = "sms.outbound.send"
OUTBOUND_SMS_AGGREGATE_TYPE = "conversation_state"

class SettingsGetter(Protocol):
    def __call__(self, key: str, default: Any = None) -> Any: ...


def _to_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _to_mode(value: Any, *, default: str = "sync") -> str:
    mode = str(value or "").strip().lower()
    if mode in {"sync", "queue"}:
        return mode
    return default


def _default_settings_getter() -> SettingsGetter:
    from core.settings_manager import get_setting  # noqa: PLC0415

    return get_setting


def _read_setting(
    *,
    env: Mapping[str, str],
    key: str,
    env_key: str,
    default: Any,
    setting_getter: SettingsGetter | None,
) -> Any:
    if env_key in env:
        return env.get(env_key)
    getter = setting_getter
    if getter is None:
        try:
            getter = _default_settings_getter()
        except Exception:
            return default
    try:
        return getter(key, default)
    except TypeError:
        return getter(key)
    except Exception:
        return default


def resolve_sms_outbound_delivery_mode(
    *,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
) -> str:
    source_env = env or os.environ
    mode_raw = _read_setting(
        env=source_env,
        key="refactor_sms_outbound_delivery_mode",
        env_key="REFACTOR_SMS_OUTBOUND_DELIVERY_MODE",
        default="sync",
        setting_getter=setting_getter,
    )
    return _to_mode(mode_raw, default="sync")


def resolve_sms_outbound_queue_sync_fallback(
    *,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
) -> bool:
    source_env = env or os.environ
    fallback_raw = _read_setting(
        env=source_env,
        key="refactor_sms_outbound_queue_sync_fallback",
        env_key="REFACTOR_SMS_OUTBOUND_QUEUE_SYNC_FALLBACK",
        default=True,
        setting_getter=setting_getter,
    )
    return _to_bool(fallback_raw, default=True)


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_map(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _message_fingerprint(message: OutboundMessage) -> str:
    normalized = " ".join(str(message.body or "").split())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def build_sms_outbound_dedup_key(
    *,
    message: OutboundMessage,
    aggregate_id: str,
    request_id: str | None,
    correlation_id: str | None,
    message_index: int,
) -> str:
    metadata = _safe_map(message.metadata)
    explicit_key = _safe_text(metadata.get("dedup_key"))
    if explicit_key:
        return explicit_key
    stable_request = _safe_text(request_id) or _safe_text(correlation_id) or _safe_text(aggregate_id)
    return (
        f"sms_outbound:{stable_request}:{_safe_text(message.channel).lower()}:"
        f"{_safe_text(message.recipient)}:{message_index}:{_message_fingerprint(message)}"
    )


def build_sms_outbound_message_id(*, dedup_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"refactor.outbound:{dedup_key}"))


@dataclass(frozen=True)
class OutboundQueuePublishResult:
    attempted: int = 0
    queued: int = 0
    duplicates: int = 0
    failed: int = 0
    queued_indices: tuple[int, ...] = ()
    duplicate_indices: tuple[int, ...] = ()
    failed_indices: tuple[int, ...] = ()


class OutboundQueuePublisher:
    def __init__(
        self,
        *,
        queue_repository: OutboundQueueProvider,
        message_type: str = OUTBOUND_SMS_EVENT_TYPE,
        aggregate_type: str = OUTBOUND_SMS_AGGREGATE_TYPE,
        max_attempts: int = 5,
    ) -> None:
        self._queue_repository = queue_repository
        self._message_type = _safe_text(message_type) or OUTBOUND_SMS_EVENT_TYPE
        self._aggregate_type = _safe_text(aggregate_type) or OUTBOUND_SMS_AGGREGATE_TYPE
        self._max_attempts = max(1, int(max_attempts))

    def publish_messages(
        self,
        *,
        aggregate_id: str,
        messages: list[OutboundMessage],
        request_id: str | None = None,
        correlation_id: str | None = None,
        conn: Any | None = None,
    ) -> OutboundQueuePublishResult:
        queued_indices: list[int] = []
        duplicate_indices: list[int] = []
        failed_indices: list[int] = []
        queued = 0
        duplicates = 0
        failed = 0
        stable_aggregate_id = _safe_text(aggregate_id)
        stable_request_id = _safe_text(request_id) or None
        stable_correlation_id = _safe_text(correlation_id) or stable_request_id

        for index, message in enumerate(messages):
            dedup_key = build_sms_outbound_dedup_key(
                message=message,
                aggregate_id=stable_aggregate_id,
                request_id=stable_request_id,
                correlation_id=stable_correlation_id,
                message_index=index,
            )
            message_id = build_sms_outbound_message_id(dedup_key=dedup_key)
            metadata_map = _safe_map(message.metadata)
            envelope = OutboundQueueEnvelope(
                message_id=message_id,
                message_type=self._message_type,
                aggregate_type=self._aggregate_type,
                aggregate_id=stable_aggregate_id or _safe_text(message.recipient),
                payload={
                    "channel": _safe_text(message.channel).lower(),
                    "recipient": _safe_text(message.recipient),
                    "body": str(message.body or ""),
                    "metadata": metadata_map,
                    "message_index": index,
                },
                metadata=QueueMessageMetadata(
                    dedup_key=dedup_key,
                    correlation_id=stable_correlation_id,
                    request_id=stable_request_id,
                    attributes={
                        "channel": _safe_text(message.channel).lower(),
                        "recipient": _safe_text(message.recipient),
                        "message_index": index,
                    },
                ),
                max_attempts=self._max_attempts,
            )
            try:
                inserted = self._queue_repository.enqueue(envelope, conn=conn)
            except Exception:
                failed += 1
                failed_indices.append(index)
                continue
            if inserted:
                queued += 1
                queued_indices.append(index)
            else:
                duplicates += 1
                duplicate_indices.append(index)

        return OutboundQueuePublishResult(
            attempted=len(messages),
            queued=queued,
            duplicates=duplicates,
            failed=failed,
            queued_indices=tuple(queued_indices),
            duplicate_indices=tuple(duplicate_indices),
            failed_indices=tuple(failed_indices),
        )
