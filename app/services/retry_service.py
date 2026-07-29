"""
Retry Service — Exponential Backoff and Transient Error Handling
=================================================================
Provides retry logic for transient database and network errors.

Handles:
  - Database connection errors (OperationalError)
  - Timeout errors
  - Deadlock errors
  - Network temporary failures
"""

import time
import random
from typing import TypeVar, Callable, Any, Optional
from functools import wraps

from sqlalchemy.exc import (
    DataError,
    IntegrityError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.orm import Session

from app.core.logger import get_logger

logger = get_logger("retry_service")

T = TypeVar('T')

# Transient errors that should trigger a retry.
#
# OperationalError only. DatabaseError is its **parent** and also the parent of
# IntegrityError, ProgrammingError, DataError and InternalError — so listing it
# made every unique-constraint violation and every SQL syntax error "transient".
# Those cannot succeed on a retry: the request just took an extra ~1.5s and held
# a pooled connection (the pool is 5 + 2 overflow) before failing anyway.
TRANSIENT_ERRORS = (OperationalError,)

# Errors that are permanent by definition, listed explicitly so a future edit
# that widens TRANSIENT_ERRORS cannot silently swallow them.
PERMANENT_ERRORS = (IntegrityError, ProgrammingError, DataError)


class RetryConfig:
    """Configuration for exponential backoff retry logic."""
    def __init__(
        self,
        max_retries: int = 3,
        initial_delay: float = 0.5,
        max_delay: float = 10.0,
        exponential_base: float = 2.0,
        jitter: bool = True,
    ):
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter

    def get_delay(self, attempt: int) -> float:
        """Calculate delay for the given attempt number with exponential backoff."""
        delay = min(
            self.initial_delay * (self.exponential_base ** attempt),
            self.max_delay
        )

        # Add jitter: random ±25% to prevent thundering herd
        if self.jitter:
            jitter_range = delay * 0.25
            delay += random.uniform(-jitter_range, jitter_range)
            delay = max(0, delay)  # Ensure delay is not negative

        return delay


def retry_on_transient_error(
    config: Optional[RetryConfig] = None,
    logger_obj: Any = None
):
    """Decorator for retrying functions on transient database errors.

    Implements exponential backoff with jitter to prevent thundering herd.

    Args:
        config: RetryConfig instance (uses defaults if None)
        logger_obj: Logger instance (uses default if None)

    Example:
        @retry_on_transient_error()
        def get_user(user_id: str, db: Session) -> User:
            return db.query(User).filter(User.id == user_id).first()
    """
    if config is None:
        config = RetryConfig()

    log = logger_obj or logger

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exception: Optional[Exception] = None

            for attempt in range(config.max_retries):
                try:
                    result = func(*args, **kwargs)
                    if attempt > 0:
                        log.info(f"{func.__name__} succeeded on retry attempt {attempt + 1}")
                    return result
                except TRANSIENT_ERRORS as e:
                    last_exception = e

                    if attempt < config.max_retries - 1:
                        delay = config.get_delay(attempt)
                        log.warning(
                            f"{func.__name__} failed on attempt {attempt + 1}/{config.max_retries}: {str(e)}. "
                            f"Retrying in {delay:.2f}s..."
                        )
                        time.sleep(delay)
                    else:
                        log.error(
                            f"{func.__name__} failed after {config.max_retries} attempts: {str(e)}",
                            exc_info=True
                        )

            # All retries exhausted
            if last_exception:
                raise last_exception
            raise RuntimeError(f"{func.__name__} failed unexpectedly")

        return wrapper

    return decorator


def execute_with_retry(
    func: Callable[..., T],
    *args,
    config: Optional[RetryConfig] = None,
    session: Optional[Session] = None,
    **kwargs
) -> T:
    """Execute a function with exponential backoff retry logic.

    Args:
        func: Function to execute
        args: Positional arguments for func
        config: RetryConfig instance (uses defaults if None)
        session: The Session ``func`` uses, if any. Rolled back between attempts
                 — an OperationalError invalidates the session, so retrying on
                 it without a rollback raises PendingRollbackError instead of
                 recovering.
        kwargs: Keyword arguments for func

    Returns:
        Result of func if successful

    Raises:
        Last exception if all retries are exhausted
    """
    if config is None:
        config = RetryConfig()

    last_exception: Optional[Exception] = None

    for attempt in range(config.max_retries):
        try:
            return func(*args, **kwargs)
        except PERMANENT_ERRORS:
            # Never retried. Listed before TRANSIENT_ERRORS because these are
            # subclasses of DatabaseError and would otherwise be caught below.
            raise
        except TRANSIENT_ERRORS as e:
            last_exception = e

            # After an OperationalError the Session is invalidated: re-issuing a
            # query on it raises PendingRollbackError rather than recovering.
            # The retry loop did not roll back, so every "retry" was guaranteed
            # to fail with a different, more confusing error.
            if session is not None:
                try:
                    session.rollback()
                except Exception as rb:
                    logger.warning(f"Rollback before retry failed: {rb}")

            if attempt < config.max_retries - 1:
                delay = config.get_delay(attempt)
                logger.warning(
                    f"Attempt {attempt + 1}/{config.max_retries} failed: {str(e)}. "
                    f"Retrying in {delay:.2f}s..."
                )
                time.sleep(delay)
            else:
                logger.error(
                    f"Failed after {config.max_retries} attempts: {str(e)}",
                    exc_info=True
                )

    if last_exception:
        raise last_exception
    raise RuntimeError("Execution failed unexpectedly")


def is_transient_error(error: Exception) -> bool:
    """Check if an error is transient and should trigger a retry."""
    return isinstance(error, TRANSIENT_ERRORS)


# Pre-configured retry strategies
AGGRESSIVE_RETRY = RetryConfig(
    max_retries=5,
    initial_delay=0.25,
    max_delay=5.0,
)

STANDARD_RETRY = RetryConfig(
    max_retries=3,
    initial_delay=0.5,
    max_delay=10.0,
)

CONSERVATIVE_RETRY = RetryConfig(
    max_retries=2,
    initial_delay=1.0,
    max_delay=5.0,
)
