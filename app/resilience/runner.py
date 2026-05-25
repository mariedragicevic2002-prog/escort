from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, cast

from app.resilience.scenarios import (
    ResilienceDrillContext,
    ResilienceDrillScenario,
    RetryableDrillError,
)

AssertionPredicate = Callable[["ResilienceDrillReport"], bool]


@dataclass(frozen=True)
class DrillAssertion:
    assertion_id: str
    predicate: AssertionPredicate
    failure_message: str


@dataclass(frozen=True)
class DrillAssertionResult:
    assertion_id: str
    passed: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "assertion_id": self.assertion_id,
            "passed": self.passed,
            "message": self.message,
        }


@dataclass(frozen=True)
class ResilienceDrillReport:
    scenario_id: str
    succeeded: bool
    bounded_execution: bool
    executed_attempts: int
    transitions: tuple[Any, ...]
    step_attempts: dict[str, int]
    assertion_results: tuple[DrillAssertionResult, ...]
    errors: tuple[str, ...]
    artifacts: dict[str, Any]
    injected_failures: tuple[dict[str, Any], ...]

    def to_artifact(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "succeeded": self.succeeded,
            "bounded_execution": self.bounded_execution,
            "executed_attempts": self.executed_attempts,
            "step_attempts": dict(sorted(self.step_attempts.items())),
            "transitions": [transition.to_dict() for transition in self.transitions],
            "assertions": [result.to_dict() for result in self.assertion_results],
            "errors": list(self.errors),
            "artifacts": dict(self.artifacts),
            "injected_failures": list(self.injected_failures),
        }


@dataclass
class ResilienceDrillRunner:
    test_mode: bool = False
    max_step_executions: int = 50
    _last_report: ResilienceDrillReport | None = field(default=None, init=False, repr=False)

    def run(
        self,
        scenario: ResilienceDrillScenario,
        *,
        context: ResilienceDrillContext | None = None,
        assertions: tuple[DrillAssertion, ...] = (),
        drill_hook: Any | None = None,
    ) -> ResilienceDrillReport:
        if scenario.test_mode_only and not self.test_mode:
            raise ValueError("resilience drills require test_mode=True")

        active_context = context or ResilienceDrillContext()
        execution_budget = min(max(1, int(self.max_step_executions)), max(1, int(scenario.max_step_executions)))
        executed_attempts = 0
        bounded_execution = True
        errors: list[str] = []

        stop_execution = False
        for step in scenario.steps:
            if stop_execution:
                break
            retries = 0
            while True:
                if executed_attempts >= execution_budget:
                    bounded_execution = False
                    errors.append("execution_budget_exhausted")
                    stop_execution = True
                    break
                executed_attempts += 1
                active_context.step_attempts[step.step_id] = active_context.step_attempts.get(step.step_id, 0) + 1
                try:
                    step.handler(active_context)
                    break
                except RetryableDrillError as exc:
                    if retries >= step.retry_limit:
                        errors.append(f"{step.step_id}:retry_exhausted:{exc}")
                        stop_execution = True
                        break
                    retries += 1
                except Exception as exc:  # pragma: no cover - exercised in drill usage
                    errors.append(f"{step.step_id}:{type(exc).__name__}:{exc}")
                    stop_execution = True
                    break

        report_stub = ResilienceDrillReport(
            scenario_id=scenario.scenario_id,
            succeeded=False,
            bounded_execution=bounded_execution,
            executed_attempts=executed_attempts,
            transitions=tuple(active_context.transitions),
            step_attempts=dict(active_context.step_attempts),
            assertion_results=(),
            errors=tuple(errors),
            artifacts=dict(active_context.artifacts),
            injected_failures=self._read_injected_failures(drill_hook),
        )
        assertion_results = self._evaluate_assertions(report_stub, assertions)
        succeeded = not errors and all(result.passed for result in assertion_results) and bounded_execution
        report = ResilienceDrillReport(
            scenario_id=scenario.scenario_id,
            succeeded=succeeded,
            bounded_execution=bounded_execution,
            executed_attempts=executed_attempts,
            transitions=tuple(active_context.transitions),
            step_attempts=dict(active_context.step_attempts),
            assertion_results=assertion_results,
            errors=tuple(errors),
            artifacts=dict(active_context.artifacts),
            injected_failures=self._read_injected_failures(drill_hook),
        )
        self._last_report = report
        return report

    @staticmethod
    def _evaluate_assertions(
        report: ResilienceDrillReport,
        assertions: tuple[DrillAssertion, ...],
    ) -> tuple[DrillAssertionResult, ...]:
        results: list[DrillAssertionResult] = []
        for assertion in assertions:
            try:
                passed = bool(assertion.predicate(report))
            except Exception as exc:  # pragma: no cover - defensive
                passed = False
                message = f"{assertion.failure_message} ({type(exc).__name__})"
            else:
                message = "passed" if passed else assertion.failure_message
            results.append(
                DrillAssertionResult(
                    assertion_id=assertion.assertion_id,
                    passed=passed,
                    message=message,
                )
            )
        return tuple(results)

    @staticmethod
    def _read_injected_failures(drill_hook: Any | None) -> tuple[dict[str, Any], ...]:
        reader = getattr(drill_hook, "drain_reported_failures", None)
        if not callable(reader):
            return ()
        try:
            records = reader()
        except Exception:
            return ()
        payload: list[dict[str, Any]] = []
        for record in cast(Any, records) or ():
            serializer = getattr(record, "to_dict", None)
            if callable(serializer):
                payload.append(cast(dict[str, Any], serializer()))
                continue
            if isinstance(record, dict):
                payload.append({str(key): value for key, value in record.items()})
        return tuple(payload)
