"""
Rate limiting for admin login attempts.
Prevents brute-force attacks by limiting failed login attempts.

When ``REDIS_URL`` / ``redis_url`` is set (Upstash, Redis Cloud, etc.), counters and lockouts
are stored in Redis so they apply across all web workers. Otherwise uses in-process dicts.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from functools import wraps
from threading import Lock

from flask import jsonify, request

from utils.net import get_client_ip as _resolve_client_ip

logger = logging.getLogger("escort_chatbot.admin.rate_limiter")

# Login attempt tracking (in-memory fallback)
_login_attempts = defaultdict(list)
_login_lock = Lock()

# Configuration
LOGIN_ATTEMPT_WINDOW = 300  # 5 minutes
MAX_LOGIN_ATTEMPTS = 5      # Max 5 attempts per 5 minutes
LOCKOUT_DURATION = 900      # 15 minutes lockout after exceeding limit

# Track lockouts (in-memory fallback)
_lockouts = {}
_lockout_lock = Lock()

KEY_LOGIN = "escort:admin:login:v1"
KEY_LOCK = "escort:admin:lock:v1"


def _redis():
    try:
        from services.redis_client import get_redis

        return get_redis()
    except Exception as e:
        logger.warning("admin rate_limiter redis: %s", e)
        return None


def _cleanup_old_attempts(attempts_list, window_seconds):
    """Remove attempts older than the window."""
    current_time = time.time()
    cutoff_time = current_time - window_seconds
    return [attempt_time for attempt_time in attempts_list if attempt_time > cutoff_time]


def _redis_configured() -> bool:
    """Return True if a Redis URL is configured (env or admin settings)."""
    try:
        from config import get_redis_url
        return bool(get_redis_url())
    except Exception:
        pass
    import os
    return bool(os.environ.get("REDIS_URL", "").strip())


def _redis_required_but_unavailable() -> bool:
    """Return True when Redis is configured but currently unreachable.

    In this state the in-memory fallback must NOT be used for admin login
    because multiple workers each have independent memory — the per-worker
    attempt counters would allow unlimited brute-force across workers.
    """
    if not _redis_configured():
        return False
    return _redis() is None


def is_ip_locked_out(ip):
    """Check if an IP is currently locked out."""
    r = _redis()
    if r:
        try:
            return bool(r.exists(f"{KEY_LOCK}:{ip}"))
        except Exception as e:
            logger.warning("Redis lockout check failed: %s", e)

    # Redis is configured but unavailable — fail-closed; the caller should
    # block logins. Return True so the login gate denies the request.
    if _redis_required_but_unavailable():
        logger.warning(
            "Redis unavailable — admin login blocked (fail-closed) for IP %s", ip
        )
        return True

    with _lockout_lock:
        if ip in _lockouts:
            lockout_time = _lockouts[ip]
            if time.time() < lockout_time:
                return True
            del _lockouts[ip]
    return False


def get_lockout_remaining(ip):
    """Get remaining lockout time in seconds."""
    r = _redis()
    if r:
        try:
            ttl = r.ttl(f"{KEY_LOCK}:{ip}")
            if ttl is not None and ttl > 0:
                return int(ttl)
        except Exception as e:
            logger.warning("Redis lockout ttl failed: %s", e)

    with _lockout_lock:
        if ip in _lockouts:
            remaining = _lockouts[ip] - time.time()
            return max(0, int(remaining))
    return 0


def record_failed_login(ip):
    """Record a failed login attempt and return if should be locked out."""
    current_time = time.time()
    r = _redis()
    if r:
        try:
            zkey = f"{KEY_LOGIN}:{ip}"
            lk = f"{KEY_LOCK}:{ip}"
            member = f"{current_time:.6f}:{uuid.uuid4().hex[:8]}"
            pipe = r.pipeline()
            pipe.zremrangebyscore(zkey, 0, current_time - LOGIN_ATTEMPT_WINDOW)
            pipe.zadd(zkey, {member: current_time})
            pipe.expire(zkey, LOGIN_ATTEMPT_WINDOW + 60)
            pipe.zcard(zkey)
            n = pipe.execute()[-1]
            if int(n or 0) >= MAX_LOGIN_ATTEMPTS:
                r.setex(lk, LOCKOUT_DURATION, "1")
                logger.warning(
                    "IP %s locked out after %s failed login attempts (Redis)",
                    ip,
                    MAX_LOGIN_ATTEMPTS,
                )
                return True
            return False
        except Exception as e:
            logger.warning("Redis record_failed_login failed: %s", e)

    # Redis configured but down — fail-closed (already handled by is_ip_locked_out).
    if _redis_required_but_unavailable():
        return True

    with _login_lock:
        _login_attempts[ip] = _cleanup_old_attempts(_login_attempts[ip], LOGIN_ATTEMPT_WINDOW)
        _login_attempts[ip].append(current_time)
        if len(_login_attempts[ip]) >= MAX_LOGIN_ATTEMPTS:
            with _lockout_lock:
                _lockouts[ip] = current_time + LOCKOUT_DURATION
            logger.warning("IP %s locked out after %s failed login attempts", ip, MAX_LOGIN_ATTEMPTS)
            return True
    return False



def clear_login_attempts(ip):
    """Clear login attempts for an IP (after successful login)."""
    r = _redis()
    if r:
        try:
            r.delete(f"{KEY_LOGIN}:{ip}", f"{KEY_LOCK}:{ip}")
            return
        except Exception as e:
            logger.warning("Redis clear_login_attempts: %s", e)

    with _login_lock:
        _login_attempts.pop(ip, None)
    with _lockout_lock:
        _lockouts.pop(ip, None)


def rate_limit_login(f):
    """Decorator to rate limit login attempts."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        ip = _resolve_client_ip(request)

        # Fail-closed: if Redis is configured but unreachable, block all admin
        # logins to prevent brute force exploiting the per-worker memory fallback.
        if _redis_required_but_unavailable():
            logger.warning(
                "Admin login blocked (Redis unavailable) for IP %s", ip
            )
            return jsonify(
                {"error": "Authentication service temporarily unavailable. Please try again shortly."}
            ), 503

        if is_ip_locked_out(ip):
            remaining = get_lockout_remaining(ip)
            minutes = remaining // 60
            logger.warning("Locked out IP %s attempted login (%s minutes remaining)", ip, minutes)
            return jsonify(
                {"error": f"Too many failed attempts. Locked out for {minutes} more minutes."}
            ), 429

        return f(*args, **kwargs)

    return decorated_function


def get_rate_limit_stats():
    """Get current rate limit statistics (for monitoring). In-memory stats only if not using Redis."""
    r = _redis()
    if r:
        try:
            return {
                "backend": "redis",
                "window_seconds": LOGIN_ATTEMPT_WINDOW,
                "max_attempts": MAX_LOGIN_ATTEMPTS,
                "lockout_seconds": LOCKOUT_DURATION,
            }
        except Exception as e:
            logger.warning("get_rate_limit_stats redis: %s", e)

    with _login_lock:
        stats = {
            "backend": "memory",
            "active_trackers": len(_login_attempts),
            "attempts_by_ip": {},
        }
        for ip, attempts in _login_attempts.items():
            cleaned = _cleanup_old_attempts(attempts, LOGIN_ATTEMPT_WINDOW)
            if cleaned:
                stats["attempts_by_ip"][ip] = {
                    "attempts_last_5min": len(cleaned),
                    "limit": MAX_LOGIN_ATTEMPTS,
                }

    with _lockout_lock:
        stats["locked_out_ips"] = {}
        current_time = time.time()
        for ip, lockout_time in _lockouts.items():
            if lockout_time > current_time:
                stats["locked_out_ips"][ip] = {
                    "remaining_seconds": int(lockout_time - current_time),
                }

    return stats


class AdminRateLimiter:
    """Rate limiter for admin operations (in-memory; used by tests / secondary checks)."""

    def __init__(self):
        self.attempts = defaultdict(list)
        self.lock = Lock()

    def is_rate_limited(self, identifier: str) -> bool:
        with self.lock:
            attempts = self.attempts[identifier]
            attempts = _cleanup_old_attempts(attempts, LOGIN_ATTEMPT_WINDOW)
            self.attempts[identifier] = attempts
            return len(attempts) >= MAX_LOGIN_ATTEMPTS

    def is_blocked(self, identifier: str) -> bool:
        return self.is_rate_limited(identifier)

    def record_attempt(self, identifier: str) -> None:
        with self.lock:
            self.attempts[identifier].append(time.time())

    def record_failed_attempt(self, identifier: str) -> None:
        self.record_attempt(identifier)
