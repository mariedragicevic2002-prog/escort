"""
handlers/availability_parts/locking.py

Redis/in-process booking lock helpers extracted from main_flow.py.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT

import logging
import os
import threading
import time
import uuid
from typing import Any

import config

logger = logging.getLogger("handlers.availability_check")


_LOCAL_BOOKING_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_BOOKING_LOCKS_GUARD = threading.Lock()


def _booking_lock_key(booking_fields: dict[str, Any], is_outcall: bool) -> str:
    """Stable lock key for final availability check + event creation.

    C3 fix: Lock at mode+date+time granularity (slot-level) so two concurrent
    requests for different time slots on the same day don't block each other,
    while identical slot requests are still serialised correctly.
    """
    date_val = str(booking_fields.get("date") or "")[:10] or "unknown-date"
    t_raw = booking_fields.get("time")
    if isinstance(t_raw, (tuple, list)) and len(t_raw) >= 2:
        hour = int(t_raw[0])
        # Quantise to 30-minute buckets so a 10:00 and 10:15 request collide
        # (both map to 10h00) while a 10:00 and 11:00 request do not.
        bucket = 0 if int(t_raw[1]) < 30 else 30
        t_val = f"{hour:02d}{bucket:02d}"
    else:
        t_val = "xxxx"  # unknown time — fall back to date-only granularity
    mode = "out" if is_outcall else "in"
    return f"booking:{mode}:{date_val}:{t_val}"


def _finalization_booking_identity_key(bf: dict[str, Any] | None) -> str:
    """Normalize date/time/duration for idempotency (skip duplicate calendar creates)."""
    if not bf:
        return ""
    d = str(bf.get("date") or "")[:10]
    t_raw = bf.get("time")
    if isinstance(t_raw, (tuple, list)) and len(t_raw) >= 2:
        t_norm = f"{int(t_raw[0])}:{int(t_raw[1])}"
    else:
        t_norm = str(t_raw or "")
    dur = str(bf.get("duration") or "")
    mode = (bf.get("incall_outcall") or "incall").lower()
    return f"{mode}|{d}|{t_norm}|{dur}"


def _truthy_env(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _require_redis_booking_lock() -> bool:
    """Return True when Redis locking must be available in this process."""
    if os.getenv("REQUIRE_REDIS_BOOKING_LOCK") is not None:
        return _truthy_env("REQUIRE_REDIS_BOOKING_LOCK", default=False)
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    return not bool(getattr(config, "DEBUG", False))


def _acquire_booking_lock(
    lock_key: str,
    ttl_seconds: int = 45,
    max_wait_seconds: float = 10.0,
    poll_interval: float = 0.12,
) -> dict[str, Any] | None:
    """Acquire Redis lock (preferred) or in-process fallback lock.

    When another request holds the same key (e.g. duplicate YES webhook or
    the same day coarse lock), wait briefly and retry so one confirmation can
    finish instead of immediately asking the user to say YES again.
    """
    try:
        from services.redis_client import get_redis

        redis_client = get_redis()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        redis_client = None

    if redis_client is not None:
        deadline = time.monotonic() + max(0.0, float(max_wait_seconds))
        while True:
            token = uuid.uuid4().hex
            try:
                locked = bool(redis_client.set(lock_key, token, nx=True, ex=ttl_seconds))
            except Exception as e:
                logger.warning("Redis lock acquisition failed for %s: %s", lock_key, type(e).__name__)
                locked = False
            if locked:
                return {"source": "redis", "client": redis_client, "key": lock_key, "token": token}
            if time.monotonic() >= deadline:
                return None
            time.sleep(
                min(poll_interval, max(0.0, deadline - time.monotonic()))
            )

    if _require_redis_booking_lock():
        logger.error("Redis booking lock required but unavailable (key=%s)", lock_key)
        return None

    with _LOCAL_BOOKING_LOCKS_GUARD:
        lock = _LOCAL_BOOKING_LOCKS.setdefault(lock_key, threading.Lock())
    if max_wait_seconds and max_wait_seconds > 0:
        acquired = lock.acquire(blocking=True, timeout=float(max_wait_seconds))
    else:
        acquired = lock.acquire(blocking=False)
    if acquired:
        return {"source": "local", "lock": lock, "key": lock_key}
    return None


def _release_booking_lock(lock_handle: dict[str, Any] | None) -> None:
    if not lock_handle:
        return
    if lock_handle.get("source") == "redis":
        client = lock_handle.get("client")
        key = lock_handle.get("key")
        token = lock_handle.get("token")
        if client and key and token:
            try:
                client.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then "
                    "return redis.call('del', KEYS[1]) else return 0 end",
                    1,
                    key,
                    token,
                )
            except Exception as e:
                logger.warning("Redis lock release failed for %s: %s", key, type(e).__name__)
        return

    lock = lock_handle.get("lock")
    if lock is not None and hasattr(lock, "release"):
        try:
            lock.release()
        except RuntimeError:
            pass
