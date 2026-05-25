from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.runtime.context import OrchestrationContext


@dataclass(frozen=True)
class IntentResolution:
    intent: str
    source: str


class IntentResolver(Protocol):
    def resolve(self, context: OrchestrationContext) -> IntentResolution | None:
        ...


class IntentHandler(Protocol):
    def __call__(self, context: OrchestrationContext) -> list[str]:
        ...


class FastPathHandler(Protocol):
    name: str

    def matches(self, context: OrchestrationContext) -> bool:
        ...

    def handle(self, context: OrchestrationContext) -> list[str]:
        ...


class LegacyFallbackHandler(Protocol):
    def __call__(self, context: OrchestrationContext) -> list[str]:
        ...
