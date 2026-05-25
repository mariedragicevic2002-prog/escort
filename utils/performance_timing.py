"""
Performance Timing Utilities
Track and log performance metrics for operations.
"""

import functools
import time
from collections.abc import Callable
from typing import Any

from utils.structured_logging import get_logger, record_metric


class PerformanceTimer:
    """Context manager for timing operations."""
    
    def __init__(self, operation_name: str, logger_name: str = None):
        self.operation_name = operation_name
        self.logger = get_logger(logger_name or "adella_chatbot.performance")
        self.start_time = None
        self.end_time = None
    
    def __enter__(self):
        self.start_time = time.time()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end_time = time.time()
        duration = self.end_time - self.start_time
        duration_ms = duration * 1000
        
        # Record metric
        record_metric(f"{self.operation_name}_duration", duration)
        
        # Log performance
        if exc_type is None:
            self.logger.info(
                f"{self.operation_name}_completed",
                operation=self.operation_name,
                duration_ms=round(duration_ms, 2),
                success=True
            )
        else:
            record_metric(f"{self.operation_name}_error", 1)
            self.logger.error(
                f"{self.operation_name}_failed",
                operation=self.operation_name,
                duration_ms=round(duration_ms, 2),
                error=str(exc_val),
                error_type=exc_type.__name__,
                success=False
            )
        
        return False  # Don't suppress exceptions


def time_function(operation_name: str = None):
    """Decorator to time a function execution."""
    def decorator(func: Callable) -> Callable:
        op_name = operation_name or f"{func.__module__}.{func.__name__}"
        
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            with PerformanceTimer(op_name):
                return func(*args, **kwargs)
        return wrapper
    return decorator


def get_performance_summary() -> dict:
    """Get summary of all recorded performance metrics."""
    from utils.structured_logging import get_metrics
    metrics = get_metrics()
    
    # Group by operation type
    summary = {}
    for key, value in metrics.items():
        if key.endswith('_duration'):
            operation = key.replace('_duration', '')
            if operation not in summary:
                summary[operation] = {}
            summary[operation]['duration_ms'] = round(value * 1000, 2)
        elif key.endswith('_error'):
            operation = key.replace('_error', '')
            if operation not in summary:
                summary[operation] = {}
            summary[operation]['errors'] = int(value)
    
    return summary
