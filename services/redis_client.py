"""
Optional Redis client for distributed rate limiting (hosted Redis: Upstash, Redis Cloud, etc.).

Uses ``REDIS_URL`` / ``redis_url`` from config. When Redis is unavailable, callers use in-memory fallbacks.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger("adella_chatbot.redis")

_redis: Any | None = None
_redis_failed: bool = False
_redis_lock = threading.Lock()


def get_redis():
    """
    Return a redis-py client, or None if not configured or connection failed.

    Caches the result for the process lifetime; failed connections are not retried until worker reload.
    """
    global _redis, _redis_failed

    if _redis_failed:
        return None
    if _redis is not None:
        return _redis

    with _redis_lock:
        if _redis_failed:
            return None
        if _redis is not None:
            return _redis

        try:
            from config import get_redis_url
        except Exception as e:
            logger.warning("get_redis_url import: %s", e)
            _redis_failed = True
            return None

        url = get_redis_url()
        if not url:
            return None

        try:
            import redis as redis_lib
        except ImportError:
            logger.warning("redis package not installed — set REDIS_URL only after pip install redis")
            _redis_failed = True
            return None

        try:
            client = redis_lib.from_url(
                url,
                decode_responses=True,
                socket_timeout=5.0,
                socket_connect_timeout=5.0,
                health_check_interval=30,
            )
            client.ping()
            _redis = client
            logger.info("Redis connected for distributed rate limiting")
            return _redis
        except Exception as e:
            logger.warning("Redis unavailable (%s) — using in-memory rate limits for this worker", e)
            _redis_failed = True
            return None


def redis_enabled() -> bool:
    """True if a live Redis client is available."""
    return get_redis() is not None
