"""
Redis-backed state cache for client conversation state.

Reduces DB reads per message by caching current state with a short TTL.
Write-through: state updates invalidate and refresh the cache.
Falls back silently to no-cache if Redis is unavailable.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger("adella_chatbot.state_cache")

_CACHE_TTL_SECONDS = 300  # 5 minutes — short enough to not stale on worker restart
_KEY_PREFIX = "state:"


def _make_key(phone_number: str) -> str:
    # Hash the phone number so plaintext client phones are never stored in Redis keys.
    # SHA-256 is deterministic and collision-resistant for this lookup use-case.
    phone_hash = hashlib.sha256(phone_number.strip().encode("utf-8")).hexdigest()[:32]
    return f"{_KEY_PREFIX}{phone_hash}"


def get_cached_state(phone_number: str) -> dict[str, Any] | None:
    """Return cached state dict or None if not cached / Redis unavailable."""
    if not phone_number:
        return None
    try:
        from services.redis_client import get_redis

        r = get_redis()
        if r is None:
            return None
        raw = r.get(_make_key(phone_number))
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as e:
        logger.debug("state cache get failed: %s", e)
        return None


def set_cached_state(phone_number: str, state: dict[str, Any]) -> bool:
    """Cache state dict with TTL. Returns True on success."""
    if not phone_number or not isinstance(state, dict):
        return False
    try:
        from services.redis_client import get_redis

        r = get_redis()
        if r is None:
            return False
        r.setex(_make_key(phone_number), _CACHE_TTL_SECONDS, json.dumps(state, default=str))
        return True
    except Exception as e:
        logger.debug("state cache set failed: %s", e)
        return False


def invalidate_cached_state(phone_number: str) -> bool:
    """Remove cached state for a phone number. Called on state write."""
    if not phone_number:
        return False
    try:
        from services.redis_client import get_redis

        r = get_redis()
        if r is None:
            return False
        r.delete(_make_key(phone_number))
        return True
    except Exception as e:
        logger.debug("state cache invalidate failed: %s", e)
        return False
