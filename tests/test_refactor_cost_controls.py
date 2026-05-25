from __future__ import annotations

from datetime import UTC, datetime, timedelta

from refactor.app.cost_controls import (
    ProcessingBudgetController,
    ProcessingBudgetSettings,
    QueueCostSignals,
    build_cost_control_advisories,
)


class _Now:
    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value


def test_processing_budget_controller_enforces_pass_and_interval_caps() -> None:
    now = _Now(datetime(2026, 1, 1, tzinfo=UTC))
    controller = ProcessingBudgetController(
        settings=ProcessingBudgetSettings(
            max_items_per_worker_pass=2,
            max_items_per_interval=3,
            interval_seconds=30,
        ),
        now_provider=now,
    )

    first = controller.evaluate(requested_items=5)
    controller.record_processed(first.allowed_items)
    second = controller.evaluate(requested_items=5)
    controller.record_processed(second.allowed_items)
    exhausted = controller.evaluate(requested_items=1)

    now.value = now.value + timedelta(seconds=31)
    reset = controller.evaluate(requested_items=5)

    assert first.allowed_items == 2
    assert first.pass_capped is True
    assert first.interval_capped is False
    assert second.allowed_items == 1
    assert second.interval_capped is True
    assert exhausted.allowed_items == 0
    assert exhausted.reason == "interval_budget_exhausted"
    assert reset.allowed_items == 2
    assert reset.reason in {"pass_cap_applied", "within_budget"}


def test_cost_control_advisories_are_deterministic_for_identical_signals() -> None:
    signals = QueueCostSignals(
        queue_depth=15,
        retry_ratio=0.5,
        dead_depth=1,
        oldest_lag_seconds=180.0,
        sample_size=20,
        provider_available=True,
        source="stub",
    )

    first = build_cost_control_advisories(signals=signals).to_public_dict()
    second = build_cost_control_advisories(signals=signals).to_public_dict()

    assert first == second
    assert first["throttle"]["advised_mode"] == "throttle"
    assert first["compaction"]["strategy"] in {"compact_retries", "coalesce_pending"}


def test_cost_control_advisories_fallback_when_signals_unavailable() -> None:
    advisories = build_cost_control_advisories(
        signals=QueueCostSignals(
            queue_depth=0,
            retry_ratio=0.0,
            dead_depth=0,
            oldest_lag_seconds=0.0,
            sample_size=25,
            provider_available=False,
            source="stub",
        )
    ).to_public_dict()

    assert advisories["throttle"]["advised_mode"] == "sync_fallback"
    assert advisories["throttle"]["reason"] == "signals_unavailable"
    assert advisories["compaction"]["should_compact"] is False
    assert advisories["compaction"]["reason"] == "signals_unavailable"
