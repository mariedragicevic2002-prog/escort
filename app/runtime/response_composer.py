from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ComposedResponse:
    messages: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)


def compose_response(raw_output: Any) -> ComposedResponse:
    messages: list[str] = []
    actions: list[dict[str, Any]] = []

    def _append_message(value: Any) -> None:
        text = str(value or "").strip()
        if text:
            messages.append(text)

    def _append_action(value: Any) -> None:
        if isinstance(value, Mapping):
            normalized = {str(key): payload for key, payload in dict(value).items()}
            if normalized:
                actions.append(normalized)
            return
        name = str(value or "").strip()
        if name:
            actions.append({"name": name})

    def _consume(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            _append_message(value)
            return
        if isinstance(value, Mapping):
            payload = dict(value)
            for key in ("messages", "message", "body", "text"):
                if key in payload:
                    _consume(payload.get(key))
            if "action" in payload:
                _consume_actions(payload.get("action"))
            if "actions" in payload:
                _consume_actions(payload.get("actions"))
            return
        if hasattr(value, "messages"):
            _consume(getattr(value, "messages"))
            _consume_actions(getattr(value, "actions", None))
            return
        if isinstance(value, tuple) and len(value) == 2:
            _consume(value[0])
            _consume_actions(value[1])
            return
        if isinstance(value, Sequence):
            for item in value:
                _consume(item)

    def _consume_actions(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str) or not isinstance(value, Sequence):
            _append_action(value)
            return
        for item in value:
            _append_action(item)

    _consume(raw_output)
    return ComposedResponse(messages=messages, actions=actions)

