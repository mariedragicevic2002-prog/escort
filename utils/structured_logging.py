"""
Structured Logging Utilities
Provides JSON logging, correlation IDs, and performance metrics.
"""

import json
import logging
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from typing import Any

# Context variable for request ID (thread-safe)
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_performance_metrics: ContextVar[dict[str, float] | None] = ContextVar("performance_metrics", default=None)

# SMS / routing observability (thread-safe; set from webhook + router)
_phone_number: ContextVar[str | None] = ContextVar("phone_number", default=None)
_conversation_state: ContextVar[str | None] = ContextVar("conversation_state", default=None)
_intent: ContextVar[str | None] = ContextVar("intent", default=None)

_logger = logging.getLogger("adella_chatbot.structured_logging")


class StructuredLogger:
    """Structured logger with correlation IDs and JSON output."""
    
    def __init__(self, name: str):
        self.logger = logging.getLogger(name)
        self.name = name
    
    def _get_context(self) -> dict[str, Any]:
        """Get current context (request ID, metrics, etc.)."""
        context = {
            'timestamp': datetime.now(UTC).isoformat(),
            'logger': self.name
        }
        
        # Add request ID if available
        request_id = _request_id.get()
        if request_id:
            context["request_id"] = request_id

        pn = _phone_number.get()
        if pn:
            # Mask to last 4 digits to avoid raw phone numbers in log output
            masked = ("*" * max(0, len(pn) - 4)) + pn[-4:] if len(pn) > 4 else pn
            context["phone_number"] = masked
        st = _conversation_state.get()
        if st:
            context["state"] = st
        it = _intent.get()
        if it:
            context["intent"] = it

        # Add performance metrics if available
        metrics = _performance_metrics.get()
        if metrics:
            context['metrics'] = metrics
        
        return context
    
    def info(self, event: str, **kwargs):
        """Log info level event with structured data."""
        context = self._get_context()
        context.update({
            'level': 'INFO',
            'event': event,
            **kwargs
        })
        self.logger.info(json.dumps(context))
    
    def warning(self, event: str, **kwargs):
        """Log warning level event with structured data."""
        context = self._get_context()
        context.update({
            'level': 'WARNING',
            'event': event,
            **kwargs
        })
        self.logger.warning(json.dumps(context))
    
    def error(self, event: str, **kwargs):
        """Log error level event with structured data."""
        context = self._get_context()
        context.update({
            'level': 'ERROR',
            'event': event,
            **kwargs
        })
        self.logger.error(json.dumps(context))
    
    def debug(self, event: str, **kwargs):
        """Log debug level event with structured data."""
        context = self._get_context()
        context.update({
            'level': 'DEBUG',
            'event': event,
            **kwargs
        })
        self.logger.debug(json.dumps(context))


def get_logger(name: str) -> StructuredLogger:
    """Get structured logger instance."""
    return StructuredLogger(name)


def set_request_id(request_id: str | None = None) -> str:
    """Set request ID for current context. Returns the request ID."""
    if request_id is None:
        request_id = str(uuid.uuid4())[:8]  # Short 8-char ID
    _request_id.set(request_id)
    _sync_sentry_observability()
    return request_id


def get_request_id() -> str | None:
    """Get current request ID."""
    return _request_id.get()


def set_observability_context(
    *,
    phone_number: str | None = None,
    state: str | None = None,
    intent: str | None = None,
) -> None:
    """Merge conversation fields into contextvars (and Sentry scope when configured)."""
    if phone_number is not None:
        _phone_number.set(phone_number)
    if state is not None:
        _conversation_state.set(state)
    if intent is not None:
        _intent.set(intent)
    _sync_sentry_observability()


def clear_observability_context() -> None:
    """Clear request + conversation context (call at end of each HTTP request)."""
    _request_id.set(None)
    _phone_number.set(None)
    _conversation_state.set(None)
    _intent.set(None)
    _performance_metrics.set(None)


def _sync_sentry_observability() -> None:
    try:
        import sentry_sdk

        if sentry_sdk.get_client() is None:
            return
        rid = _request_id.get()
        if rid:
            sentry_sdk.set_tag("request_id", rid)
        pn = _phone_number.get()
        if pn:
            sentry_sdk.set_tag("phone_number", pn)
        st = _conversation_state.get()
        if st:
            sentry_sdk.set_tag("state", st)
        it = _intent.get()
        if it:
            sentry_sdk.set_tag("intent", it)
    except Exception as e:
        _logger.warning("Sentry observability sync failed: %s", e, exc_info=True)


class ObservabilityLogFilter(logging.Filter):
    """Injects rid / phone / state / intent into every LogRecord for plain formatters."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        pn = _phone_number.get() or "-"
        # Mask to last 4 digits in log lines
        record.phone_number = ("*" * max(0, len(pn) - 4)) + pn[-4:] if len(pn) > 4 else pn
        record.state = _conversation_state.get() or "-"
        record.intent = _intent.get() or "-"
        return True


_observability_logging_configured = False


def _is_benign_log_stream_error(exc: BaseException) -> bool:
    """True when the logging stream is gone (client disconnect, closed pipe, Windows console)."""
    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
        return True
    if not isinstance(exc, OSError):
        return False
    msg = str(exc).lower()
    if "write error" in msg or "broken pipe" in msg:
        return True
    err = getattr(exc, "errno", None)
    import errno as _errno

    if err is not None and err in (_errno.EPIPE, _errno.ECONNRESET, getattr(_errno, "ESHUTDOWN", -1)):
        return True
    return False


def _patch_handler_emit_guard(handler: logging.Handler) -> None:
    if getattr(handler, "_adella_emit_guarded", False):
        return
    original = handler.emit

    def guarded_emit(record: logging.LogRecord) -> None:
        try:
            original(record)
        except OSError as e:
            if _is_benign_log_stream_error(e):
                return
            raise

    handler.emit = guarded_emit  # type: ignore[method-assign]
    setattr(handler, "_adella_emit_guarded", True)


def configure_observability_logging() -> None:
    """Attach ObservabilityFilter and bracketed format to root handlers (call after basicConfig)."""
    global _observability_logging_configured
    if _observability_logging_configured:
        return
    fmt = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - "
        "[rid=%(request_id)s phone=%(phone_number)s state=%(state)s intent=%(intent)s] %(message)s"
    )
    flt = ObservabilityLogFilter()
    root = logging.getLogger()
    for h in root.handlers:
        _patch_handler_emit_guard(h)
        h.setFormatter(fmt)
        h.addFilter(flt)
    _observability_logging_configured = True


def record_metric(name: str, value: float):
    """Record a performance metric."""
    metrics = _performance_metrics.get()
    if not metrics:
        metrics = {}
        _performance_metrics.set(metrics)
    metrics[name] = value


def get_metrics() -> dict[str, float]:
    """Get all recorded metrics."""
    return _performance_metrics.get() or {}


def clear_metrics():
    """Clear all metrics."""
    _performance_metrics.set({})


def timed_operation(operation_name: str):
    """Decorator to time an operation and log it."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                duration = time.time() - start_time
                record_metric(f"{operation_name}_duration", duration)
                
                logger = get_logger(func.__module__)
                logger.info(
                    f"{operation_name}_completed",
                    operation=operation_name,
                    duration_ms=round(duration * 1000, 2),
                    success=True
                )
                return result
            except Exception as e:
                duration = time.time() - start_time
                record_metric(f"{operation_name}_duration", duration)
                record_metric(f"{operation_name}_error", 1)
                
                logger = get_logger(func.__module__)
                logger.error(
                    f"{operation_name}_failed",
                    operation=operation_name,
                    duration_ms=round(duration * 1000, 2),
                    error=str(e),
                    error_type=type(e).__name__,
                    success=False
                )
                raise
        return wrapper
    return decorator


def log_state_transition(phone_number: str, old_state: str, new_state: str, **kwargs):
    """Log state transition with structured data."""
    logger = get_logger("adella_chatbot.state_transitions")
    logger.info(
        "state_transition",
        phone_number=phone_number,
        old_state=old_state,
        new_state=new_state,
        **kwargs
    )


def log_booking_event(event_type: str, phone_number: str, **kwargs):
    """Log booking-related event."""
    logger = get_logger("adella_chatbot.booking")
    logger.info(
        f"booking_{event_type}",
        phone_number=phone_number,
        event_type=event_type,
        **kwargs
    )


def log_api_call(service: str, endpoint: str, duration_ms: float, success: bool, **kwargs):
    """Log external API call."""
    logger = get_logger("adella_chatbot.api")
    level = "info" if success else "error"
    getattr(logger, level)(
        "api_call",
        service=service,
        endpoint=endpoint,
        duration_ms=round(duration_ms, 2),
        success=success,
        **kwargs
    )


def log_quality_metric(metric_name: str, **kwargs):
    """Log conversation quality and funnel metrics."""
    logger = get_logger("adella_chatbot.quality")
    logger.info(
        "quality_metric",
        metric_name=metric_name,
        **kwargs,
    )
