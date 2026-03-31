"""
Generic error handling with retry logic and exponential backoff.

Provides RetryConfig (backoff parameters) and ErrorHandler (async retry
execution with smart error classification: no retry on 4xx, Retry-After
support for rate-limit errors, retry-once for data errors).
"""

import asyncio
import logging
import random
from enum import Enum
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class ErrorSeverity(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    FATAL = "fatal"


class RetryableError(Exception):
    """Base class for errors that support severity-based retry decisions."""

    def __init__(self, message: str, severity: ErrorSeverity = ErrorSeverity.MEDIUM):
        super().__init__(message)
        self.severity = severity


class NetworkError(RetryableError):
    """HTTP or network-level failure."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        severity: ErrorSeverity = ErrorSeverity.MEDIUM,
    ):
        super().__init__(message, severity)
        self.status_code = status_code


class RateLimitError(RetryableError):
    """Rate limit exceeded; optionally carries a Retry-After delay."""

    def __init__(
        self,
        message: str,
        retry_after: Optional[float] = None,
        severity: ErrorSeverity = ErrorSeverity.MEDIUM,
    ):
        super().__init__(message, severity)
        self.retry_after = retry_after


class DataError(RetryableError):
    """Data parsing or validation failure."""


class RetryConfig:
    """Configuration for retry behavior."""

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        jitter: bool = True,
    ):
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter

    def get_delay(self, attempt: int) -> float:
        """Calculate delay for a specific attempt using exponential backoff."""
        delay = self.base_delay * (self.exponential_base ** (attempt - 1))
        delay = min(delay, self.max_delay)
        if self.jitter:
            delay = delay * (0.5 + random.random() * 0.5)
        return delay


class ErrorHandler:
    """Executes async operations with retry logic and per-operation error tracking."""

    def __init__(self, retry_config: Optional[RetryConfig] = None):
        self.retry_config = retry_config or RetryConfig()
        self.error_counts: Dict[str, int] = {}

    async def execute_with_retry(
        self,
        operation: Callable,
        operation_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """
        Execute an async operation with retry logic.

        Args:
            operation: Async callable to execute.
            operation_name: Name used for logging and error tracking.
            *args, **kwargs: Forwarded to *operation*.

        Returns:
            The result of *operation* on success.

        Raises:
            RetryableError subclass: When all retry attempts are exhausted.
            Exception: When a non-retryable error is raised by *operation*.
        """
        last_error: Optional[RetryableError] = None

        for attempt in range(1, self.retry_config.max_attempts + 1):
            try:
                result = await operation(*args, **kwargs)
                self.error_counts.pop(operation_name, None)
                return result

            except Exception as exc:
                classified = self._classify(exc, operation_name)
                last_error = classified
                self.error_counts[operation_name] = self.error_counts.get(operation_name, 0) + 1

                logger.warning(
                    "Attempt %d/%d failed for %s: %s",
                    attempt,
                    self.retry_config.max_attempts,
                    operation_name,
                    exc,
                )

                if not self._should_retry(classified, attempt):
                    break

                if attempt < self.retry_config.max_attempts:
                    delay = self.retry_config.get_delay(attempt)
                    if isinstance(classified, RateLimitError) and classified.retry_after:
                        delay = max(delay, classified.retry_after)
                    logger.info("Retrying %s in %.1f seconds...", operation_name, delay)
                    await asyncio.sleep(delay)

        status_suffix = (
            f": HTTP {last_error.status_code}"
            if isinstance(last_error, NetworkError) and last_error.status_code is not None
            else ""
        )
        logger.error("All attempts failed for %s%s", operation_name, status_suffix)

        if last_error is not None:
            raise last_error
        raise RetryableError(
            f"Operation {operation_name} failed after {self.retry_config.max_attempts} attempts"
        )

    def _classify(self, exc: Exception, operation_name: str) -> RetryableError:
        """Wrap an arbitrary exception as a RetryableError subclass if needed."""
        if isinstance(exc, RetryableError):
            return exc
        return DataError(f"{operation_name}: {exc}")

    def _should_retry(self, error: RetryableError, attempt: int) -> bool:
        """Return True if *error* warrants another attempt."""
        if attempt >= self.retry_config.max_attempts:
            return False
        if error.severity in (ErrorSeverity.FATAL, ErrorSeverity.HIGH):
            return False
        if isinstance(error, (NetworkError, RateLimitError)):
            if (
                isinstance(error, NetworkError)
                and error.status_code is not None
                and 400 <= error.status_code < 500
            ):
                return False
            return True
        if isinstance(error, DataError):
            return attempt == 1
        return True

    def get_error_stats(self) -> Dict[str, Any]:
        """Return a snapshot of per-operation error counts."""
        return {
            "total_operations_with_errors": len(self.error_counts),
            "error_counts": self.error_counts.copy(),
            "most_problematic_operations": sorted(
                self.error_counts.items(), key=lambda x: x[1], reverse=True
            )[:5],
        }
