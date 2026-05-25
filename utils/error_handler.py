"""
Enhanced Error Handling Utilities
Provides graceful degradation, retry logic, and error recovery.
"""

import logging
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from utils.log_sanitize import LOG_SUPPRESSED_FMT

logger = logging.getLogger("adella_chatbot.error_handler")


def retry_with_backoff(
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,)
):
    """Decorator for retrying with exponential backoff.
    
    Args:
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay in seconds
        max_delay: Maximum delay in seconds
        exponential_base: Base for exponential backoff
        exceptions: Tuple of exception types to retry on
        
    Example:
        @retry_with_backoff(max_retries=3, initial_delay=1.0)
        def api_call():
            # Will retry on failure
            pass
    """
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_retries:
                        logger.error(
                            f"{func.__name__} failed after {max_retries} retries: {e}"
                        )
                        raise
                    
                    logger.warning(
                        f"{func.__name__} attempt {attempt + 1}/{max_retries + 1} failed: {e}. "
                        f"Retrying in {delay}s..."
                    )
                    
                    time.sleep(delay)
                    delay = min(delay * exponential_base, max_delay)
            
            return None
        return wrapper
    return decorator


def graceful_degradation(fallback_value: Any = None, fallback_func: Callable | None = None):
    """Decorator for graceful degradation on errors.
    
    Args:
        fallback_value: Value to return on error
        fallback_func: Function to call on error (takes same args as original)
        
    Example:
        @graceful_degradation(fallback_value=[])
        def get_calendar_events():
            # Returns [] on error
            pass
    """
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.warning(f"{func.__name__} failed: {e} - using graceful degradation")
                
                if fallback_func:
                    try:
                        return fallback_func(*args, **kwargs)
                    except Exception as e:
                        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
                
                return fallback_value
        return wrapper
    return decorator


def safe_execute(
    func: Callable,
    *args,
    default: Any = None,
    log_error: bool = True,
    **kwargs
) -> Any:
    """Safely execute a function, returning default on error.
    
    Args:
        func: Function to execute
        *args: Function arguments
        default: Default value to return on error
        log_error: Whether to log errors
        **kwargs: Function keyword arguments
        
    Returns:
        Function result or default
    """
    try:
        return func(*args, **kwargs)
    except Exception as e:
        if log_error:
            logger.error(f"Error executing {func.__name__}: {e}", exc_info=True)
        return default


def handle_api_error(error: Exception, service_name: str, fallback_message: str) -> str:
    """Handle API errors with user-friendly messages.
    
    Args:
        error: Exception that occurred
        service_name: Name of the service
        fallback_message: Message to return if error can't be handled
        
    Returns:
        User-friendly error message
    """
    error_type = type(error).__name__
    error_msg = str(error).lower()
    
    # Network errors
    if 'timeout' in error_msg or 'connection' in error_msg:
        return f"Sorry, I'm having trouble connecting to {service_name}. Please try again in a moment."
    
    # Rate limiting
    if 'rate limit' in error_msg or '429' in error_msg:
        return f"{service_name} is temporarily busy. Please try again in a few minutes."
    
    # Authentication errors
    if 'auth' in error_msg or '401' in error_msg or '403' in error_msg:
        logger.error(f"{service_name} authentication error: {error}")
        return fallback_message
    
    # Generic error
    logger.error(f"{service_name} error ({error_type}): {error}")
    return fallback_message
