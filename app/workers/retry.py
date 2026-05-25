from __future__ import annotations

from dataclasses import dataclass

from app.events.outbox import OutboxEventRecord


@dataclass(frozen=True)
class RetryDecision:
    retry_delay_seconds: int
    would_exceed_budget: bool
    next_attempt_number: int


class ExponentialBackoffRetryPolicy:
    def __init__(
        self,
        *,
        base_delay_seconds: int = 5,
        multiplier: int = 2,
        max_delay_seconds: int = 300,
    ) -> None:
        self._base_delay_seconds = max(1, int(base_delay_seconds))
        self._multiplier = max(1, int(multiplier))
        self._max_delay_seconds = max(1, int(max_delay_seconds))

    def evaluate(self, event: OutboxEventRecord) -> RetryDecision:
        return self.evaluate_counts(
            retry_count=int(event.retry_count),
            max_retries=int(event.max_retries),
        )

    def evaluate_counts(self, *, retry_count: int, max_retries: int) -> RetryDecision:
        """Compute retry decision directly from counts; avoids wrapper dataclass allocation."""
        next_attempt = retry_count + 1
        computed_delay = self._base_delay_seconds * (self._multiplier ** max(0, next_attempt - 1))
        delay = min(self._max_delay_seconds, computed_delay)
        would_exceed_budget = next_attempt >= max_retries
        return RetryDecision(
            retry_delay_seconds=delay,
            would_exceed_budget=would_exceed_budget,
            next_attempt_number=next_attempt,
        )

