from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


class RetryableDrillError(RuntimeError):
    """Signals the scenario runner to retry a bounded step."""


@dataclass(frozen=True)
class ResilienceStateTransition:
    component: str
    from_state: str
    to_state: str
    reason: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "component": self.component,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "reason": self.reason,
        }


@dataclass
class ResilienceDrillContext:
    state: dict[str, str] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    transitions: list[ResilienceStateTransition] = field(default_factory=list)
    step_attempts: dict[str, int] = field(default_factory=dict)

    def transition(self, *, component: str, to_state: str, reason: str = "") -> None:
        normalized_component = str(component or "unknown")
        previous = self.state.get(normalized_component, "unknown")
        self.state[normalized_component] = str(to_state or "unknown")
        self.transitions.append(
            ResilienceStateTransition(
                component=normalized_component,
                from_state=previous,
                to_state=self.state[normalized_component],
                reason=str(reason or ""),
            )
        )


StepHandler = Callable[[ResilienceDrillContext], None]


@dataclass(frozen=True)
class ResilienceDrillStep:
    step_id: str
    handler: StepHandler
    retry_limit: int = 0

    def __post_init__(self) -> None:
        if not str(self.step_id or "").strip():
            raise ValueError("step_id is required")
        object.__setattr__(self, "retry_limit", max(0, int(self.retry_limit)))


@dataclass(frozen=True)
class ResilienceDrillScenario:
    scenario_id: str
    description: str
    steps: tuple[ResilienceDrillStep, ...]
    max_step_executions: int = 25
    test_mode_only: bool = True

    def __post_init__(self) -> None:
        if not str(self.scenario_id or "").strip():
            raise ValueError("scenario_id is required")
        if not self.steps:
            raise ValueError("scenario requires at least one step")
        object.__setattr__(self, "max_step_executions", max(1, int(self.max_step_executions)))
