"""
Central timeouts, tenacity retries, and helpers for external APIs (Claude, Gemini, Google Calendar).

Env overrides (seconds, unless noted):
  AI_HTTP_TIMEOUT_SECONDS (default 45)
  GEMINI_HTTP_TIMEOUT_SECONDS (default 45)
  CALENDAR_HTTP_TIMEOUT_SECONDS (default 30)
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger("adella_chatbot.api_resilience")


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.environ.get(name, str(default)) or str(default)).strip())
    except (TypeError, ValueError):
        return default


AI_HTTP_TIMEOUT_SECONDS = max(5.0, _env_float("AI_HTTP_TIMEOUT_SECONDS", 25.0))
GEMINI_HTTP_TIMEOUT_SECONDS = max(5.0, _env_float("GEMINI_HTTP_TIMEOUT_SECONDS", 25.0))
CALENDAR_HTTP_TIMEOUT_SECONDS = max(5.0, _env_float("CALENDAR_HTTP_TIMEOUT_SECONDS", 30.0))
HTTPSMS_HTTP_TIMEOUT_SECONDS = max(5.0, _env_float("HTTPSMS_HTTP_TIMEOUT_SECONDS", 15.0))


def chat_fallback_template_message() -> str:
    """Last-resort reply when Claude and Gemini both fail or are unavailable."""
    return (
        "I'm having a brief connection issue with our AI. "
        "Please send your message again in a moment, or text your booking details "
        "(date, time, duration) and I'll help."
    )


def _retry_anthropic(exc: BaseException) -> bool:
    try:
        import anthropic

        if isinstance(
            exc,
            (
                anthropic.APITimeoutError,
                anthropic.APIConnectionError,
                anthropic.InternalServerError,
            ),
        ):
            return True
        if hasattr(anthropic, "RateLimitError") and isinstance(exc, anthropic.RateLimitError):
            return True
    except Exception as e:
        logger.warning("retry classifier (anthropic types): %s", e, exc_info=True)
    return False


def _retry_gemini(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    try:
        import httpx

        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.WriteError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            if exc.response.status_code in (408, 429, 500, 502, 503, 504):
                return True
    except Exception as e:
        logger.warning("retry classifier (httpx for gemini): %s", e, exc_info=True)
    try:
        from google.api_core import exceptions as gexc

        if isinstance(exc, (gexc.ServiceUnavailable, gexc.DeadlineExceeded, gexc.TooManyRequests)):
            return True
        if isinstance(exc, gexc.InternalServerError):
            return True
    except Exception as e:
        logger.warning("retry classifier (google.api_core for gemini): %s", e, exc_info=True)
    err = str(exc).lower()
    return any(
        x in err
        for x in (
            "timeout",
            "503",
            "429",
            "unavailable",
            "deadline",
            "connection",
            "temporarily",
            "resource exhausted",
            "try again",
        )
    )


def _retry_calendar_http(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError, BrokenPipeError)):
        return True
    try:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            code = getattr(exc.resp, "status", None) if getattr(exc, "resp", None) else None
            if code in (408, 429, 500, 502, 503, 504):
                return True
    except Exception as e:
        logger.warning("retry classifier (calendar HttpError): %s", e, exc_info=True)
    return False


retrying_anthropic_chat: Retrying = Retrying(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=20),
    retry=retry_if_exception(_retry_anthropic),
    reraise=True,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)

retrying_gemini_chat: Retrying = Retrying(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=20),
    retry=retry_if_exception(_retry_gemini),
    reraise=True,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)

retrying_calendar_execute: Retrying = Retrying(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=12),
    retry=retry_if_exception(_retry_calendar_http),
    reraise=True,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)


def call_with_retry_anthropic(fn: Callable[[], object]) -> object:
    return retrying_anthropic_chat(fn)


def call_with_retry_gemini(fn: Callable[[], object]) -> object:
    return retrying_gemini_chat(fn)


def call_with_retry_calendar_execute(fn: Callable[[], object]) -> object:
    """Wrap Google Calendar ``request.execute()`` (no-arg callable)."""
    return retrying_calendar_execute(fn)


def _retry_httpsms(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    try:
        import requests

        if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
            return True
        if isinstance(exc, requests.exceptions.HTTPError):
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code in (429, 500, 502, 503, 504):
                return True
    except Exception:
        pass
    return False


_retrying_httpsms: Retrying = Retrying(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=1.0, max=10),
    retry=retry_if_exception(_retry_httpsms),
    reraise=True,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)


def call_with_retry_httpsms(fn: Callable[[], object]) -> object:
    """Wrap httpSMS send with retry (transient network errors + 5xx)."""
    return _retrying_httpsms(fn)
