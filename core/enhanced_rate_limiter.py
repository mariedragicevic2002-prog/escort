"""
Enhanced Rate Limiter — SMS webhook abuse protection.

Uses Redis when ``REDIS_URL`` / ``redis_url`` is set (Upstash, Redis Cloud, etc.) so limits are
shared across workers and survive process restarts. Falls back to in-memory dicts otherwise.
"""

from __future__ import annotations

from utils.log_sanitize import LOG_SUPPRESSED_FMT

import logging
import re
import threading
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime, timedelta

logger = logging.getLogger("escort_chatbot.rate_limiter")


def _sanitize_phone_key(phone_number: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z+]", "", (phone_number or "").strip())[:80]
    return s or "unknown"


class EnhancedRateLimiterBase(ABC):
    """Shared limits (must match across backends)."""

    MESSAGES_PER_MINUTE = 10
    MESSAGES_PER_HOUR = 50
    MESSAGES_PER_DAY = 200
    COOLDOWN_1ST = timedelta(minutes=5)
    COOLDOWN_2ND = timedelta(minutes=15)
    COOLDOWN_3RD = timedelta(hours=1)

    @abstractmethod
    def check_rate_limit(self, phone_number: str) -> tuple[bool, str | None]:
        pass

    @abstractmethod
    def reset_limits(self, phone_number: str) -> None:
        pass

    @abstractmethod
    def get_stats(self, phone_number: str) -> dict[str, int | bool]:
        pass

    def is_rate_limited(self, phone_number: str) -> bool:
        allowed, _ = self.check_rate_limit(phone_number)
        return not allowed

    def record_message(self, phone_number: str) -> None:
        """Record-only hook (memory backend counts inside check_rate_limit)."""
        pass


class EnhancedRateLimiterMemory(EnhancedRateLimiterBase):
    """In-process rate limiter (single worker; lost on restart).

    Without Redis, counters live only in this process. Multi-worker deployments (e.g. multiple
    uWSGI workers) each enforce limits independently — effective throughput is roughly
    ``workers × configured limits`` unless ``REDIS_URL`` (or equivalent) enables the shared backend.
    """

    def __init__(self) -> None:
        self._message_counts: dict[str, list] = defaultdict(list)
        self._warnings: dict[str, int] = defaultdict(int)
        self._cooldowns: dict[str, datetime] = {}
        self._last_cleanup: datetime = datetime.now()
        self._CLEANUP_INTERVAL = timedelta(hours=1)

    def _cleanup_stale_entries(self) -> None:
        now = datetime.now()
        if now - self._last_cleanup < self._CLEANUP_INTERVAL:
            return
        self._last_cleanup = now
        cutoff = now - timedelta(hours=24)
        stale_phones = [
            phone for phone, timestamps in self._message_counts.items()
            if not timestamps or timestamps[-1] < cutoff
        ]
        for phone in stale_phones:
            self._message_counts.pop(phone, None)
            self._warnings.pop(phone, None)
            self._cooldowns.pop(phone, None)
        if stale_phones:
            logger.debug("Cleaned up rate limiter entries for %s inactive phone numbers", len(stale_phones))

    def check_rate_limit(self, phone_number: str) -> tuple[bool, str | None]:
        now = datetime.now()
        self._cleanup_stale_entries()

        if phone_number in self._cooldowns:
            cooldown_until = self._cooldowns[phone_number]
            if now < cooldown_until:
                remaining = cooldown_until - now
                return False, (
                    f"You're sending messages too quickly. Please wait {int(remaining.total_seconds() / 60)} minutes."
                )
            del self._cooldowns[phone_number]

        cutoff = now - timedelta(hours=24)
        self._message_counts[phone_number] = [
            ts for ts in self._message_counts[phone_number] if ts > cutoff
        ]
        self._message_counts[phone_number].append(now)

        messages_last_minute = sum(
            1 for ts in self._message_counts[phone_number] if ts > now - timedelta(minutes=1)
        )
        messages_last_hour = sum(
            1 for ts in self._message_counts[phone_number] if ts > now - timedelta(hours=1)
        )
        messages_last_day = len(self._message_counts[phone_number])

        if messages_last_minute > self.MESSAGES_PER_MINUTE:
            warning_count = self._warnings[phone_number]
            self._warnings[phone_number] = warning_count + 1
            if warning_count == 0:
                self._cooldowns[phone_number] = now + self.COOLDOWN_1ST
                return False, "You're sending messages too quickly. Please wait 5 minutes."
            if warning_count == 1:
                self._cooldowns[phone_number] = now + self.COOLDOWN_2ND
                return False, "You're sending messages too quickly. Please wait 15 minutes."
            self._cooldowns[phone_number] = now + self.COOLDOWN_3RD
            return False, "You're sending messages too quickly. Please wait 1 hour."

        if messages_last_hour > self.MESSAGES_PER_HOUR:
            return False, "You've exceeded the hourly message limit. Please wait before sending more messages."
        if messages_last_day > self.MESSAGES_PER_DAY:
            return False, "You've exceeded the daily message limit. Please try again tomorrow."
        if messages_last_minute <= self.MESSAGES_PER_MINUTE:
            self._warnings[phone_number] = 0
        return True, None

    def reset_limits(self, phone_number: str) -> None:
        self._message_counts.pop(phone_number, None)
        self._warnings.pop(phone_number, None)
        self._cooldowns.pop(phone_number, None)

    def get_stats(self, phone_number: str) -> dict[str, int | bool]:
        now = datetime.now()
        messages = self._message_counts.get(phone_number, [])
        return {
            "messages_last_minute": sum(1 for ts in messages if ts > now - timedelta(minutes=1)),
            "messages_last_hour": sum(1 for ts in messages if ts > now - timedelta(hours=1)),
            "messages_last_day": len(messages),
            "warnings": self._warnings.get(phone_number, 0),
            "in_cooldown": phone_number in self._cooldowns and now < self._cooldowns[phone_number],
        }


class EnhancedRateLimiterRedis(EnhancedRateLimiterBase):
    """Redis-backed limiter (shared state across workers / restarts)."""

    KEY_PREFIX = "escort:rl:v1"

    def __init__(self, redis_client) -> None:
        self._r = redis_client

    def _keys(self, p: str) -> tuple[str, str, str]:
        base = f"{self.KEY_PREFIX}:{p}"
        return f"{base}:msgs", f"{base}:cd", f"{base}:warn"

    def check_rate_limit(self, phone_number: str) -> tuple[bool, str | None]:
        import time

        p = _sanitize_phone_key(phone_number)
        msgs_key, cd_key, warn_key = self._keys(p)
        now = time.time()

        ttl_cd = self._r.ttl(cd_key)
        if ttl_cd and ttl_cd > 0:
            mins = max(1, ttl_cd // 60)
            return False, (
                f"You're sending messages too quickly. Please wait {mins} minutes."
            )

        member = f"{now:.6f}:{uuid.uuid4().hex[:10]}"
        pipe = self._r.pipeline()
        pipe.zremrangebyscore(msgs_key, 0, now - 86400)
        pipe.zadd(msgs_key, {member: now})
        pipe.zcount(msgs_key, now - 60, now + 1)
        pipe.zcount(msgs_key, now - 3600, now + 1)
        pipe.zcard(msgs_key)
        results = pipe.execute()

        messages_last_minute = int(results[2] or 0)
        messages_last_hour = int(results[3] or 0)
        messages_last_day = int(results[4] or 0)

        if messages_last_minute > self.MESSAGES_PER_MINUTE:
            # Matches memory backend: 1st offence → 5m, 2nd → 15m, 3rd+ → 1h
            strike = int(self._r.incr(warn_key))
            self._r.expire(warn_key, 86400)
            if strike == 1:
                self._r.setex(cd_key, int(self.COOLDOWN_1ST.total_seconds()), "1")
                return False, "You're sending messages too quickly. Please wait 5 minutes."
            if strike == 2:
                self._r.setex(cd_key, int(self.COOLDOWN_2ND.total_seconds()), "1")
                return False, "You're sending messages too quickly. Please wait 15 minutes."
            self._r.setex(cd_key, int(self.COOLDOWN_3RD.total_seconds()), "1")
            return False, "You're sending messages too quickly. Please wait 1 hour."

        if messages_last_hour > self.MESSAGES_PER_HOUR:
            return False, "You've exceeded the hourly message limit. Please wait before sending more messages."
        if messages_last_day > self.MESSAGES_PER_DAY:
            return False, "You've exceeded the daily message limit. Please try again tomorrow."
        if messages_last_minute <= self.MESSAGES_PER_MINUTE:
            self._r.set(warn_key, "0")
            self._r.expire(warn_key, 86400)
        return True, None

    def reset_limits(self, phone_number: str) -> None:
        p = _sanitize_phone_key(phone_number)
        msgs_key, cd_key, warn_key = self._keys(p)
        self._r.delete(msgs_key, cd_key, warn_key)

    def get_stats(self, phone_number: str) -> dict[str, int | bool]:
        import time

        p = _sanitize_phone_key(phone_number)
        msgs_key, cd_key, warn_key = self._keys(p)
        now = time.time()
        pipe = self._r.pipeline()
        pipe.zcount(msgs_key, now - 60, now + 1)
        pipe.zcount(msgs_key, now - 3600, now + 1)
        pipe.zcard(msgs_key)
        pipe.get(warn_key)
        pipe.ttl(cd_key)
        m1, mh, md, wr, ttl_cd = pipe.execute()
        return {
            "messages_last_minute": int(m1 or 0),
            "messages_last_hour": int(mh or 0),
            "messages_last_day": int(md or 0),
            "warnings": int(wr or 0),
            "in_cooldown": bool(ttl_cd and ttl_cd > 0),
        }


_limiter_instance: EnhancedRateLimiterBase | None = None
_limiter_lock = threading.Lock()


def get_rate_limiter() -> EnhancedRateLimiterBase:
    """Return the process-wide rate limiter (Redis when configured, else memory)."""
    global _limiter_instance
    if _limiter_instance is not None:
        return _limiter_instance

    with _limiter_lock:
        if _limiter_instance is not None:
            return _limiter_instance

        try:
            from services.redis_client import get_redis

            r = get_redis()
            if r is not None:
                _limiter_instance = EnhancedRateLimiterRedis(r)
                logger.info("SMS rate limiting: Redis (distributed)")
                return _limiter_instance
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)

        _limiter_instance = EnhancedRateLimiterMemory()
        logger.info("SMS rate limiting: in-memory (set REDIS_URL for distributed limits)")
        return _limiter_instance


# Backwards compatibility for isinstance(..., EnhancedRateLimiter)
EnhancedRateLimiter = EnhancedRateLimiterMemory
