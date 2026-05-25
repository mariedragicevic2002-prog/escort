"""
Circuit Breaker Pattern Implementation
Provides circuit breaker for external API calls with graceful degradation.
"""

import logging
import threading
import time
from collections.abc import Callable
from enum import Enum
from functools import wraps
from typing import Any, Union

logger = logging.getLogger("adella_chatbot.circuit_breaker")

# Single type or tuple of types for ``except`` matching
ExceptionTypes = Union[type, tuple[type, ...]]


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Circuit is open, failing fast
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreaker:
    """Circuit breaker for external service calls."""
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        expected_exception: ExceptionTypes = Exception,
        name: str = "circuit"
    ):
        """Initialize circuit breaker.
        
        Args:
            failure_threshold: Number of failures before opening circuit
            recovery_timeout: Seconds to wait before attempting recovery
            expected_exception: Exception type that counts as failure
            name: Name for logging
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        self.name = name
        
        self.failure_count = 0
        self.last_failure_time: float | None = None
        self.state = CircuitState.CLOSED
        self.success_count = 0  # For half-open state
        self._lock = threading.Lock()  # Protects all mutable state in multi-threaded WSGI

    def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function with circuit breaker protection.

        Args:
            func: Function to call
            *args: Function arguments
            **kwargs: Function keyword arguments

        Returns:
            Function result

        Raises:
            CircuitBreakerOpenError: If circuit is open
            Exception: If function call fails
        """
        # Check and potentially transition state under lock.
        with self._lock:
            if self.state == CircuitState.OPEN:
                if self.last_failure_time and \
                   time.time() - self.last_failure_time >= self.recovery_timeout:
                    logger.info(f"Circuit breaker {self.name}: Attempting recovery (half-open)")
                    self.state = CircuitState.HALF_OPEN
                    self.success_count = 0
                else:
                    raise CircuitBreakerOpenError(
                        f"Circuit breaker {self.name} is OPEN. "
                        f"Service unavailable. Retry after {self.recovery_timeout}s"
                    )

        # Execute outside the lock so other threads aren't blocked during I/O.
        try:
            result = func(*args, **kwargs)

            # Record success under lock.
            with self._lock:
                if self.state == CircuitState.HALF_OPEN:
                    self.success_count += 1
                    if self.success_count >= 2:  # Require 2 successes to close
                        logger.info(f"Circuit breaker {self.name}: Recovered (closed)")
                        self.state = CircuitState.CLOSED
                        self.failure_count = 0
                        self.success_count = 0
                elif self.state == CircuitState.CLOSED:
                    self.failure_count = 0

            return result

        except self.expected_exception as e:
            # Record failure under lock.
            with self._lock:
                self.failure_count += 1
                self.last_failure_time = time.time()
                should_open = self.failure_count >= self.failure_threshold

            logger.warning(
                f"Circuit breaker {self.name}: Failure {self.failure_count}/{self.failure_threshold}: {e}"
            )

            if should_open:
                with self._lock:
                    self.state = CircuitState.OPEN
                logger.error(f"Circuit breaker {self.name}: Opening circuit (too many failures)")

            raise

    def reset(self):
        """Reset circuit breaker to closed state."""
        with self._lock:
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.success_count = 0
            self.last_failure_time = None
        logger.info(f"Circuit breaker {self.name}: Reset to CLOSED")
    
    def get_state(self) -> CircuitState:
        """Get current circuit state."""
        return self.state
    
    def get_stats(self) -> dict[str, Any]:
        """Get circuit breaker statistics."""
        return {
            'name': self.name,
            'state': self.state.value,
            'failure_count': self.failure_count,
            'failure_threshold': self.failure_threshold,
            'last_failure_time': self.last_failure_time,
            'recovery_timeout': self.recovery_timeout
        }


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open."""
    pass


# Global circuit breakers
_circuit_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(
    name: str,
    failure_threshold: int = 5,
    recovery_timeout: float = 60.0,
    expected_exception: ExceptionTypes = Exception,
) -> CircuitBreaker:
    """Get or create circuit breaker.
    
    Args:
        name: Circuit breaker name
        failure_threshold: Number of failures before opening
        recovery_timeout: Seconds to wait before recovery attempt
        expected_exception: Exception type that counts as failure
        
    Returns:
        CircuitBreaker instance
    """
    if name not in _circuit_breakers:
        _circuit_breakers[name] = CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            expected_exception=expected_exception,
            name=name
        )
    return _circuit_breakers[name]


def circuit_breaker(
    name: str,
    failure_threshold: int = 5,
    recovery_timeout: float = 60.0,
    expected_exception: ExceptionTypes = Exception,
    fallback: Callable | None = None,
):
    """Decorator for circuit breaker pattern.
    
    Args:
        name: Circuit breaker name
        failure_threshold: Number of failures before opening
        recovery_timeout: Seconds to wait before recovery attempt
        expected_exception: Exception type that counts as failure
        fallback: Optional fallback function if circuit is open
        
    Example:
        @circuit_breaker("calendar_api", failure_threshold=5, recovery_timeout=60)
        def check_calendar():
            # Will be protected by circuit breaker
            pass
    """
    def decorator(func: Callable):
        cb = get_circuit_breaker(
            name=name,
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            expected_exception=expected_exception
        )
        
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return cb.call(func, *args, **kwargs)
            except CircuitBreakerOpenError:
                if fallback:
                    logger.info(f"Circuit breaker {name} OPEN - using fallback")
                    return fallback(*args, **kwargs)
                raise
        
        return wrapper
    return decorator
