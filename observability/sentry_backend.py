"""Sentry backend for error tracking.

This module handles all Sentry SDK interactions including initialization,
error capture, and context management.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Iterator
from uuid import UUID

if TYPE_CHECKING:
    from .config import SentryConfig

logger = logging.getLogger(__name__)


def _serialize_value(value: Any, _seen: set[int] | None = None) -> Any:
    """Serialize a value to a JSON-compatible type.

    Handles common non-JSON types like datetime, UUID, and objects with
    __dict__ attribute. Falls back to str() for unknown types.

    Note:
        Circular references are detected and replaced with a placeholder string.
        This prevents infinite recursion when serializing self-referential structures.

    Args:
        value: The value to serialize.
        _seen: Internal tracking set for circular reference detection.

    Returns:
        A JSON-serializable value.
    """
    # Initialize seen set for circular reference detection
    if _seen is None:
        _seen = set()

    # Primitive types are always safe
    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, UUID):
        return str(value)

    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return f"<bytes: {len(value)} bytes>"

    # Check for circular references in container types
    value_id = id(value)
    if value_id in _seen:
        return f"<circular reference: {type(value).__name__}>"
    _seen.add(value_id)

    if isinstance(value, dict):
        return {k: _serialize_value(v, _seen) for k, v in value.items()}

    if isinstance(value, (list, tuple)):
        return [_serialize_value(item, _seen) for item in value]

    if isinstance(value, set):
        return [_serialize_value(item, _seen) for item in sorted(value, key=str)]

    # Try to use __dict__ for custom objects
    if hasattr(value, "__dict__"):
        try:
            return {
                k: _serialize_value(v, _seen)
                for k, v in value.__dict__.items()
                if not k.startswith("_")
            }
        except Exception as e:
            logger.debug("Could not serialize object via __dict__: %s: %s", type(value).__name__, e)

    # Fallback to string representation
    try:
        return str(value)
    except Exception:
        return f"<unserializable: {type(value).__name__}>"


def _serialize_context(context: dict[str, Any]) -> dict[str, Any]:
    """Serialize context dict to ensure all values are JSON-compatible.

    Args:
        context: The context dictionary to serialize.

    Returns:
        A new dictionary with all values serialized to JSON-compatible types.
    """
    seen: set[int] = set()
    return {key: _serialize_value(value, seen) for key, value in context.items()}


class SentryBackend:
    """Backend for Sentry error tracking."""

    def __init__(self, config: SentryConfig) -> None:
        self._config = config
        self._initialized = False

    def init(self) -> bool:
        """Initialize the Sentry SDK with FastAPI/Starlette integrations.

        Configures Sentry with the following integrations when available:
        - LoggingIntegration (always included)
        - FastApiIntegration with transaction_style="endpoint"
        - StarletteIntegration with transaction_style="endpoint"
        - OpenTelemetryIntegration (if OTEL SDK is installed)

        Returns:
            True if Sentry was initialized successfully (SDK configured and ready).
            False if initialization was skipped (disabled/no DSN) or failed (exception).

        Note:
            Does not raise exceptions - failures are logged and return False.
        """
        if not self._config.enabled or not self._config.dsn:
            logger.debug("Sentry initialization skipped (disabled or DSN not configured)")
            return False

        try:
            import sentry_sdk
            from sentry_sdk.integrations.logging import LoggingIntegration

            integrations: list[Any] = [
                LoggingIntegration(
                    level=None,  # Don't capture breadcrumbs from logs
                    event_level=None,  # Don't send log events
                ),
            ]

            # Add FastAPI integration if available
            try:
                from sentry_sdk.integrations.fastapi import FastApiIntegration

                integrations.append(FastApiIntegration(transaction_style="endpoint"))
            except ImportError:
                pass

            # Add Starlette integration if available
            try:
                from sentry_sdk.integrations.starlette import StarletteIntegration

                integrations.append(StarletteIntegration(transaction_style="endpoint"))
            except ImportError:
                pass

            # Add OpenTelemetry integration if available
            # Note: Must catch both ImportError and DidNotEnable - the latter is raised
            # when opentelemetry SDK is not installed even if sentry_sdk is
            try:
                from sentry_sdk.integrations.opentelemetry import OpenTelemetryIntegration

                integrations.append(OpenTelemetryIntegration())
            except ImportError:
                logger.debug("OpenTelemetry integration module not available")
            except Exception as e:
                # DidNotEnable is raised when OTEL SDK is not installed or not configured.
                # This is expected in some deployments, so we log at debug level.
                logger.debug("OpenTelemetry integration unavailable: %s: %s", type(e).__name__, e)

            sentry_sdk.init(
                dsn=self._config.dsn,
                environment=self._config.environment,
                release=self._config.release,
                traces_sample_rate=self._config.traces_sample_rate,
                profiles_sample_rate=self._config.profiles_sample_rate,
                integrations=integrations,
                send_default_pii=self._config.send_default_pii,
            )

            self._initialized = True

            logger.info(
                f"Sentry initialized for environment '{self._config.environment}' "
                f"with {self._config.traces_sample_rate * 100:.0f}% trace sampling"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to initialize Sentry: {e}", exc_info=True)
            return False

    def capture_error(
        self,
        exception: BaseException,
        *,
        context: dict[str, Any] | None = None,
        level: str = "error",
        user: dict[str, Any] | None = None,
        tags: dict[str, str] | None = None,
        fingerprint: list[str] | None = None,
    ) -> str | None:
        """Capture an exception and send to Sentry.

        Captures the exception with full stack trace and any additional context.
        Non-JSON-serializable values in context are automatically converted to
        string representations.

        Args:
            exception: The exception to capture.
            context: Additional context data to attach. Values are automatically
                serialized (datetime -> ISO string, UUID -> string, etc.).
            level: Error severity level. One of "debug", "info", "warning",
                "error", or "fatal". Defaults to "error".
            user: User information dict with keys like "id", "email", "username".
            tags: Tags for categorization and filtering in Sentry.
            fingerprint: Custom grouping fingerprint to override automatic grouping.

        Returns:
            Event ID if captured, None if not initialized.
        """
        if not self._initialized:
            return None

        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            if context:
                # Serialize context to handle non-JSON types
                serialized_context = _serialize_context(context)
                scope.set_context("additional", serialized_context)
            if user:
                scope.set_user(user)
            if tags:
                for key, value in tags.items():
                    scope.set_tag(key, value)
            if fingerprint:
                scope.fingerprint = fingerprint
            scope.level = level

            return scope.capture_exception(exception)

    def capture_message(
        self,
        message: str,
        *,
        level: str = "info",
        context: dict[str, Any] | None = None,
        tags: dict[str, str] | None = None,
    ) -> str | None:
        """Capture a message and send to Sentry.

        Args:
            message: The message to capture.
            level: Message severity level. Defaults to "info".
            context: Additional context data to attach.
            tags: Tags for categorization and filtering.

        Returns:
            Event ID if captured, None if not initialized.
        """
        if not self._initialized:
            return None

        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            if context:
                # Serialize context to handle non-JSON types
                serialized_context = _serialize_context(context)
                scope.set_context("additional", serialized_context)
            if tags:
                for key, value in tags.items():
                    scope.set_tag(key, value)
            scope.level = level

            return scope.capture_message(message, level=level)

    @contextmanager
    def start_span(
        self,
        name: str,
        *,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        """Start a Sentry span/transaction.

        Yields:
            Sentry span object.
        """
        if not self._initialized:
            yield None
            return

        import sentry_sdk

        with sentry_sdk.start_span(op="function", description=name) as span:
            if attributes:
                for key, value in attributes.items():
                    span.set_data(key, value)
            yield span

    def set_user(self, user_id: str | None, **extra: Any) -> None:
        """Set the current user context."""
        if not self._initialized:
            return

        import sentry_sdk

        if user_id is None:
            sentry_sdk.set_user(None)
        else:
            sentry_sdk.set_user({"id": user_id, **extra})

    def set_tag(self, key: str, value: str) -> None:
        """Set a tag on the current scope."""
        if not self._initialized:
            return

        import sentry_sdk

        sentry_sdk.set_tag(key, value)

    def set_context(self, name: str, context: dict[str, Any]) -> None:
        """Set a context block on the current scope.

        Args:
            name: Context block name.
            context: Context data. Non-JSON types are automatically serialized.
        """
        if not self._initialized:
            return

        import sentry_sdk

        # Serialize context to handle non-JSON types
        serialized_context = _serialize_context(context)
        sentry_sdk.set_context(name, serialized_context)

    def flush(self, timeout: float = 2.0) -> None:
        """Flush pending events."""
        if not self._initialized:
            return

        import sentry_sdk

        sentry_sdk.flush(timeout=timeout)

    def shutdown(self) -> None:
        """Shutdown the Sentry SDK."""
        if not self._initialized:
            return

        import sentry_sdk

        client = sentry_sdk.get_client()
        if client is not None:
            client.close(timeout=2.0)

        self._initialized = False
