"""
policy_gate.py — Ordered inbound policy gate for the processing pipeline.

Checks execute in declaration order; the first deny short-circuits remaining
checks.  All individual checks fail open: an exception returns None (allow),
preserving availability under dependency failures.

Check order:
  1. rate_limit      — cheapest, no DB round-trip
  2. blocked_client  — DB look-up; silent 200 rejection
  3. chatbot_enabled — settings flag
  4. blocked_phrases — configurable phrase list
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Deny decision (immutable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyDeny:
    """
    Immutable deny decision produced by PolicyGate.

    response_body: str response to return/log (may be empty for silent rejects)
    send_sms:      whether the caller should send an SMS to the user
    log_event:     structured-log event tag for observability
    """

    reason: str
    http_status: int
    response_body: str
    send_sms: bool
    log_event: str


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class PolicyGate:
    """
    Runs an ordered sequence of policy checks against the inbound context.

    Instantiate once per request (or share as a stateless singleton since all
    state is read from ctx.services at call time).
    """

    def __init__(self, services) -> None:
        self._services = services

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, ctx) -> Optional[PolicyDeny]:
        """Run all checks in order; return the first deny or None."""
        checks: List[Callable] = [
            self._check_rate_limit,
            self._check_blocked_client,
            self._check_chatbot_enabled,
            self._check_blocked_phrases,
        ]
        for check_fn in checks:
            result = check_fn(ctx)
            if result is not None:
                return result
        return None

    # ------------------------------------------------------------------
    # Individual checks — all return Optional[PolicyDeny], all fail open
    # ------------------------------------------------------------------

    def _check_rate_limit(self, ctx) -> Optional[PolicyDeny]:
        try:
            if ctx.services.rate_limiter.is_rate_limited(ctx.message.from_number):
                return PolicyDeny(
                    reason="rate_limited",
                    http_status=429,
                    response_body="Too many requests",
                    send_sms=False,
                    log_event="rate_limited",
                )
        except Exception:
            logger.exception("rate_limit check raised — failing open")
        return None

    def _check_blocked_client(self, ctx) -> Optional[PolicyDeny]:
        try:
            if ctx.services.db_service.is_blocked(ctx.message.from_number):
                return PolicyDeny(
                    reason="client_blocked",
                    http_status=200,    # silent rejection — no error response
                    response_body="",
                    send_sms=False,
                    log_event="client_blocked",
                )
        except Exception:
            logger.exception("blocked_client check raised — failing open")
        return None

    def _check_chatbot_enabled(self, ctx) -> Optional[PolicyDeny]:
        try:
            enabled = ctx.services.settings_manager.get_setting("chatbot_enabled", True)
            if not enabled:
                return PolicyDeny(
                    reason="chatbot_disabled",
                    http_status=200,
                    response_body="",
                    send_sms=False,
                    log_event="chatbot_disabled",
                )
        except Exception:
            logger.exception("chatbot_enabled check raised — failing open")
        return None

    def _check_blocked_phrases(self, ctx) -> Optional[PolicyDeny]:
        try:
            phrases = ctx.services.settings_manager.get_setting("blocked_phrases", [])
            body_lower = ctx.message.body.lower()
            for phrase in phrases:
                if phrase.lower() in body_lower:
                    return PolicyDeny(
                        reason="blocked_phrase",
                        http_status=200,
                        response_body="",
                        send_sms=False,
                        log_event="blocked_phrase",
                    )
        except Exception:
            logger.exception("blocked_phrases check raised — failing open")
        return None
