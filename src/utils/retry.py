"""
Retry utilities for handling transient failures.

This module provides decorators and utilities for implementing
retry logic with exponential backoff for external service calls.
"""

import logging
import random
import time
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def with_retry(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
    on_retry: Callable[[Exception, int], None] = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for retry with exponential backoff.

    Retries a function call when specified exceptions occur,
    with configurable delay and exponential backoff.

    Args:
        max_attempts: Maximum number of attempts (default: 3)
        delay: Initial delay between retries in seconds (default: 1.0)
        backoff: Multiplier for delay after each retry (default: 2.0)
        exceptions: Tuple of exception types to catch and retry (default: (Exception,))
        on_retry: Optional callback function called on each retry with (exception, attempt)

    Returns:
        Decorated function with retry logic

    Example:
        @with_retry(max_attempts=3, delay=1.0, exceptions=(requests.RequestException,))
        def fetch_data(url):
            return requests.get(url)
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exception = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e

                    if attempt < max_attempts:
                        # Add jitter to prevent thundering herd on concurrent retries
                        sleep_time = delay * (backoff ** (attempt - 1)) * random.uniform(0.5, 1.5)
                        logger.warning(
                            f"Attempt {attempt}/{max_attempts} failed for {func.__name__}: {e}. "
                            f"Retrying in {sleep_time:.1f}s"
                        )

                        if on_retry:
                            on_retry(e, attempt)

                        time.sleep(sleep_time)
                    else:
                        logger.error(f"All {max_attempts} attempts failed for {func.__name__}: {e}")

            raise last_exception

        return wrapper

    return decorator


def retry_on_exception(
    func: Callable[..., T],
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> T:
    """Execute a function with retry logic (non-decorator version).

    Useful when you can't use a decorator or need dynamic retry parameters.

    Args:
        func: Function to execute (should be a callable with no arguments, use lambda or partial)
        max_attempts: Maximum number of attempts
        delay: Initial delay between retries in seconds
        backoff: Multiplier for delay after each retry
        exceptions: Tuple of exception types to catch and retry

    Returns:
        Result of the function call

    Example:
        result = retry_on_exception(
            lambda: requests.get(url),
            max_attempts=3,
            exceptions=(requests.RequestException,)
        )
    """
    last_exception = None

    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except exceptions as e:
            last_exception = e

            if attempt < max_attempts:
                sleep_time = delay * (backoff ** (attempt - 1)) * random.uniform(0.5, 1.5)
                logger.warning(
                    f"Attempt {attempt}/{max_attempts} failed: {e}. Retrying in {sleep_time:.1f}s"
                )
                time.sleep(sleep_time)
            else:
                logger.error(f"All {max_attempts} attempts failed: {e}")

    raise last_exception


class RetryConfig:
    """Configuration class for retry behavior.

    Provides a reusable configuration object for retry parameters.
    """

    def __init__(
        self,
        max_attempts: int = 3,
        delay: float = 1.0,
        backoff: float = 2.0,
        exceptions: tuple[type[Exception], ...] = (Exception,),
    ):
        self.max_attempts = max_attempts
        self.delay = delay
        self.backoff = backoff
        self.exceptions = exceptions

    def decorator(self) -> Callable:
        """Return a decorator configured with these settings."""
        return with_retry(
            max_attempts=self.max_attempts,
            delay=self.delay,
            backoff=self.backoff,
            exceptions=self.exceptions,
        )


# Pre-configured retry configurations for common use cases
API_RETRY = RetryConfig(max_attempts=3, delay=1.0, backoff=2.0)
DATABASE_RETRY = RetryConfig(max_attempts=2, delay=0.5, backoff=1.5)
SEARCH_RETRY = RetryConfig(max_attempts=2, delay=2.0, backoff=2.0)
