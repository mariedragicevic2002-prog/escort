from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

INGRESS_ENQUEUE_FAILURE_POINT = "ingress_enqueue_failure"
WORKER_CRASH_POINT = "worker_crash"
WORKER_LEASE_EXPIRY_POINT = "worker_lease_expiry"
GUARDRAIL_ROLLBACK_TRIGGER_POINT = "guardrail_rollback_trigger"

_ALL_FAILURE_POINTS = (
    INGRESS_ENQUEUE_FAILURE_POINT,
    WORKER_CRASH_POINT,
    WORKER_LEASE_EXPIRY_POINT,
    GUARDRAIL_ROLLBACK_TRIGGER_POINT,
)


def _normalize_schedule(values: tuple[int, ...]) -> tuple[int, ...]:
    bounded = {max(1, int(value)) for value in values}
    return tuple(sorted(bounded))


@dataclass(frozen=True)
class DeterministicFailurePlan:
    ingress_enqueue_failure: tuple[int, ...] = ()
    worker_crash: tuple[int, ...] = ()
    worker_lease_expiry: tuple[int, ...] = ()
    guardrail_rollback_trigger: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "ingress_enqueue_failure", _normalize_schedule(self.ingress_enqueue_failure))
        object.__setattr__(self, "worker_crash", _normalize_schedule(self.worker_crash))
        object.__setattr__(self, "worker_lease_expiry", _normalize_schedule(self.worker_lease_expiry))
        object.__setattr__(self, "guardrail_rollback_trigger", _normalize_schedule(self.guardrail_rollback_trigger))

    def as_schedule_map(self) -> dict[str, set[int]]:
        return {
            INGRESS_ENQUEUE_FAILURE_POINT: set(self.ingress_enqueue_failure),
            WORKER_CRASH_POINT: set(self.worker_crash),
            WORKER_LEASE_EXPIRY_POINT: set(self.worker_lease_expiry),
            GUARDRAIL_ROLLBACK_TRIGGER_POINT: set(self.guardrail_rollback_trigger),
        }


@dataclass(frozen=True)
class InjectedFailureRecord:
    point: str
    invocation: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "point": self.point,
            "invocation": self.invocation,
            "metadata": {str(key): value for key, value in self.metadata.items()},
        }


class ResilienceDrillFailure(RuntimeError):
    def __init__(self, *, point: str, message: str | None = None) -> None:
        self.point = str(point or "unknown")
        super().__init__(message or f"resilience drill injected failure ({self.point})")


@runtime_checkable
class ResilienceDrillHook(Protocol):
    def before_ingress_enqueue(
        self,
        *,
        channel: str,
        request_id: str,
        dedup_key: str,
    ) -> None:
        ...

    def before_worker_dispatch(
        self,
        *,
        queue_name: str,
        item_id: str,
    ) -> None:
        ...

    def guardrail_action_override(self, *, feature: str) -> str | None:
        ...

    def drain_reported_failures(self) -> tuple[InjectedFailureRecord, ...]:
        ...


class DeterministicFailureInjector:
    """Test-only deterministic failure injector for resilience drills."""

    def __init__(
        self,
        *,
        plan: DeterministicFailurePlan | None = None,
        enabled: bool = False,
        test_mode: bool = False,
        max_injected_failures: int = 20,
        lease_expiry_advancer: Callable[[str, str], None] | None = None,
    ) -> None:
        if enabled and not test_mode:
            raise ValueError("deterministic failure injector requires test_mode=True")
        self._enabled = bool(enabled)
        self._schedule = (plan or DeterministicFailurePlan()).as_schedule_map()
        self._max_injected_failures = max(1, int(max_injected_failures))
        self._lease_expiry_advancer = lease_expiry_advancer
        self._invocations = {point: 0 for point in _ALL_FAILURE_POINTS}
        self._records: list[InjectedFailureRecord] = []

    def before_ingress_enqueue(
        self,
        *,
        channel: str,
        request_id: str,
        dedup_key: str,
    ) -> None:
        injected = self._consume(
            INGRESS_ENQUEUE_FAILURE_POINT,
            metadata={
                "channel": str(channel or ""),
                "request_id": str(request_id or ""),
                "dedup_key": str(dedup_key or ""),
            },
        )
        if injected:
            raise ResilienceDrillFailure(point=INGRESS_ENQUEUE_FAILURE_POINT)

    def before_worker_dispatch(
        self,
        *,
        queue_name: str,
        item_id: str,
    ) -> None:
        lease_injected = self._consume(
            WORKER_LEASE_EXPIRY_POINT,
            metadata={
                "queue_name": str(queue_name or ""),
                "item_id": str(item_id or ""),
            },
        )
        if lease_injected and callable(self._lease_expiry_advancer):
            self._lease_expiry_advancer(str(queue_name or ""), str(item_id or ""))
        crash_injected = self._consume(
            WORKER_CRASH_POINT,
            metadata={
                "queue_name": str(queue_name or ""),
                "item_id": str(item_id or ""),
            },
        )
        if crash_injected:
            raise ResilienceDrillFailure(point=WORKER_CRASH_POINT)

    def guardrail_action_override(self, *, feature: str) -> str | None:
        injected = self._consume(
            GUARDRAIL_ROLLBACK_TRIGGER_POINT,
            metadata={"feature": str(feature or "")},
        )
        if injected:
            return "rollback"
        return None

    def drain_reported_failures(self) -> tuple[InjectedFailureRecord, ...]:
        return tuple(self._records)

    def _consume(self, point: str, *, metadata: dict[str, Any]) -> bool:
        normalized_point = str(point or "")
        if normalized_point not in self._invocations:
            return False
        self._invocations[normalized_point] += 1
        invocation = self._invocations[normalized_point]
        if not self._enabled:
            return False
        if len(self._records) >= self._max_injected_failures:
            return False
        if invocation not in self._schedule.get(normalized_point, set()):
            return False
        self._records.append(
            InjectedFailureRecord(
                point=normalized_point,
                invocation=invocation,
                metadata={str(key): value for key, value in metadata.items()},
            )
        )
        return True
