"""
Per-phone-number sliding window rate limiter.

Infrastructure layer — in-process, thread-safe.
Upgradeable to Redis-backed without changing the interface.

Default: max 20 messages per phone number per 60-second window.
Configure via environment:
  RATE_LIMIT_MAX_REQUESTS  (default: 20)
  RATE_LIMIT_WINDOW_SECONDS (default: 60)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


def _read_positive_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default))
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        logger.warning("rate_limit.invalid_env", extra={"name": name, "value": raw_value})
        return default


_MAX_REQUESTS = _read_positive_int("RATE_LIMIT_MAX_REQUESTS", 20)
_WINDOW_SECONDS = _read_positive_int("RATE_LIMIT_WINDOW_SECONDS", 60)

_lock = threading.Lock()
_windows: dict[str, deque[float]] = defaultdict(deque)


class RateLimitExceeded(Exception):
    """Raised when a phone number exceeds the allowed request rate."""


def _mask_phone(phone: str) -> str:
    cleaned = (phone or "").strip()
    if len(cleaned) <= 4:
        return "****"
    return cleaned[:4] + "****"


def _prune_window(window: deque[float], cutoff: float) -> None:
    while window and window[0] < cutoff:
        window.popleft()


def check_rate_limit(phone: str) -> None:
    """
    Check whether the given phone number is within rate limits.
    Raises RateLimitExceeded if the limit is breached.
    Records the current request if allowed.
    """
    now = time.monotonic()
    cutoff = now - _WINDOW_SECONDS
    phone_key = (phone or "").strip()

    with _lock:
        window = _windows[phone_key]
        _prune_window(window, cutoff)

        if len(window) >= _MAX_REQUESTS:
            logger.warning(
                "rate_limit.exceeded",
                extra={
                    "phone": _mask_phone(phone_key),
                    "requests_in_window": len(window),
                    "window_seconds": _WINDOW_SECONDS,
                },
            )
            raise RateLimitExceeded(
                f"Rate limit exceeded for phone (max {_MAX_REQUESTS} per {_WINDOW_SECONDS}s)"
            )

        window.append(now)


def get_request_count(phone: str) -> int:
    """Return the current request count for a phone number within the window."""
    now = time.monotonic()
    cutoff = now - _WINDOW_SECONDS
    phone_key = (phone or "").strip()
    with _lock:
        window = _windows[phone_key]
        _prune_window(window, cutoff)
        count = len(window)
        if count == 0:
            _windows.pop(phone_key, None)
        return count


__all__ = ["RateLimitExceeded", "check_rate_limit", "get_request_count"]
