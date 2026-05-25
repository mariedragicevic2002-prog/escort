"""Webhook health monitor: rolling window of handler outcomes for alert logs."""

from __future__ import annotations

import threading
import time
from collections import deque

from utils.structured_logging import get_logger

structured_logger = get_logger("escort_chatbot.main")

# Tunable via code only (operational settings live in admin_settings; these are internal heuristics).
_WEBHOOK_MONITOR_WINDOW = max(50, 200)
_WEBHOOK_MONITOR_MIN_SAMPLES = max(10, 25)
_WEBHOOK_IGNORED_ALERT_THRESHOLD = min(max(0.20, 0.0), 1.0)
_WEBHOOK_EMPTY_ALERT_THRESHOLD = min(max(0.05, 0.0), 1.0)
_WEBHOOK_MONITOR_ALERT_COOLDOWN = max(30, 300)
_WEBHOOK_MONITOR_EVENTS: deque[dict[str, int | bool]] = deque(maxlen=_WEBHOOK_MONITOR_WINDOW)
_WEBHOOK_MONITOR_LAST_ALERT = {"ignored": 0.0, "empty": 0.0}
# All reads/writes of the deque and last-alert dict must go through this lock.
# PythonAnywhere runs threaded workers; unsynchronised deque mutations under
# contention can yield inconsistent counts and flapping alerts.
_WEBHOOK_MONITOR_LOCK = threading.Lock()


# Alert kinds share a single cooldown-dispatch pattern. Keep the per-kind knobs
# here so adding a new alert class is a one-line entry.
_ALERT_SPECS: dict[str, dict] = {
    "ignored": {
        "threshold": _WEBHOOK_IGNORED_ALERT_THRESHOLD,
        "event": "webhook_monitor_ignored_rate_high",
        "rate_key": "ignored_rate",
        "count_key": "ignored_count",
    },
    "empty": {
        "threshold": _WEBHOOK_EMPTY_ALERT_THRESHOLD,
        "event": "webhook_monitor_empty_outbound_rate_high",
        "rate_key": "empty_outbound_rate",
        "count_key": "empty_count",
    },
}


def _should_fire(kind: str, rate: float, now_ts: float) -> bool:
    """Return True (and mark fired) when ``kind``'s rate exceeds its threshold
    and the cooldown has elapsed. Must be called under _WEBHOOK_MONITOR_LOCK."""
    spec = _ALERT_SPECS[kind]
    if rate < spec["threshold"]:
        return False
    if (now_ts - _WEBHOOK_MONITOR_LAST_ALERT[kind]) < _WEBHOOK_MONITOR_ALERT_COOLDOWN:
        return False
    _WEBHOOK_MONITOR_LAST_ALERT[kind] = now_ts
    return True


def record_webhook_monitor(
    *,
    request_id: str,
    ignored_event: bool = False,
    handler_empty: bool = False,
    messages_sent: int = 0,
    messages_failed: int = 0,
) -> None:
    """Track webhook health and emit alert logs on sustained ignored/empty-outbound rates."""
    import logging

    logger = logging.getLogger("escort_chatbot.main")
    try:
        pending_alerts: list[tuple[str, float, int, int]] = []
        with _WEBHOOK_MONITOR_LOCK:
            _WEBHOOK_MONITOR_EVENTS.append(
                {
                    "ignored": bool(ignored_event),
                    "empty": bool(handler_empty or (messages_sent == 0)),
                    "sent": int(messages_sent),
                    "failed": int(messages_failed),
                }
            )
            total = len(_WEBHOOK_MONITOR_EVENTS)
            if total < _WEBHOOK_MONITOR_MIN_SAMPLES:
                return

            now_ts = time.time()
            counts = {
                "ignored": sum(1 for x in _WEBHOOK_MONITOR_EVENTS if x["ignored"]),
                "empty": sum(1 for x in _WEBHOOK_MONITOR_EVENTS if x["empty"]),
            }
            for kind, count in counts.items():
                rate = count / total
                if _should_fire(kind, rate, now_ts):
                    pending_alerts.append((kind, rate, count, total))

        # Emit alerts outside the lock — structured logging may do I/O.
        for kind, rate, count, sample_size in pending_alerts:
            spec = _ALERT_SPECS[kind]
            structured_logger.warning(
                spec["event"],
                **{spec["rate_key"]: round(rate, 4), spec["count_key"]: count},
                sample_size=sample_size,
                request_id=request_id,
            )
    except Exception as mon_err:
        logger.warning("Webhook monitor update failed: %s", mon_err)
