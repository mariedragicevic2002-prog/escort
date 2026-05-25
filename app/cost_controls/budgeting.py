from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Callable

from app.cost_controls.contracts import ProcessingBudgetDecision, ProcessingBudgetSettings


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _sanitize_settings(settings: ProcessingBudgetSettings) -> ProcessingBudgetSettings:
    return ProcessingBudgetSettings(
        max_items_per_worker_pass=max(1, int(settings.max_items_per_worker_pass)),
        max_items_per_interval=max(1, int(settings.max_items_per_interval)),
        interval_seconds=max(1, int(settings.interval_seconds)),
    )


class ProcessingBudgetController:
    def __init__(
        self,
        *,
        settings: ProcessingBudgetSettings | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = _sanitize_settings(settings or ProcessingBudgetSettings())
        self._now = now_provider or _utc_now
        self._window_started_at = self._now()
        self._interval_used = 0

    @property
    def settings(self) -> ProcessingBudgetSettings:
        return self._settings

    def evaluate(self, *, requested_items: int) -> ProcessingBudgetDecision:
        self._roll_window()
        requested = max(0, int(requested_items))
        pass_limit = min(requested, int(self._settings.max_items_per_worker_pass))
        remaining = max(0, int(self._settings.max_items_per_interval) - int(self._interval_used))
        allowed = min(pass_limit, remaining)
        pass_capped = requested > int(self._settings.max_items_per_worker_pass)
        interval_capped = remaining < pass_limit
        if requested <= 0:
            reason = "empty_request"
        elif allowed <= 0 and remaining <= 0:
            reason = "interval_budget_exhausted"
        elif pass_capped and interval_capped:
            reason = "pass_and_interval_cap"
        elif interval_capped:
            reason = "interval_cap_applied"
        elif pass_capped:
            reason = "pass_cap_applied"
        else:
            reason = "within_budget"
        return ProcessingBudgetDecision(
            requested_items=requested,
            allowed_items=max(0, int(allowed)),
            interval_remaining=remaining,
            pass_capped=pass_capped,
            interval_capped=interval_capped,
            reason=reason,
        )

    def record_processed(self, processed_items: int) -> None:
        self._roll_window()
        processed = max(0, int(processed_items))
        if processed <= 0:
            return
        self._interval_used = min(
            int(self._settings.max_items_per_interval),
            int(self._interval_used) + processed,
        )

    def _roll_window(self) -> None:
        now = self._now()
        if now - self._window_started_at >= timedelta(seconds=int(self._settings.interval_seconds)):
            self._window_started_at = now
            self._interval_used = 0
