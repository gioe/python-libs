"""Comprehensive unit tests for the Sentry backend with mocked SDK.

This module provides 90%+ code coverage for sentry_backend.py by testing:
- init() with valid/invalid config
- capture_error() with various exception types
- capture_message() with full functionality
- Context attachment (user, custom, request)
- Error level mapping
- start_span() context manager
- flush() and shutdown() lifecycle methods
- Graceful handling of Sentry unavailability
- Serialization edge cases

All tests use mocked Sentry SDK - no real API calls are made.
"""

from datetime import datetime
from typing import Any
from unittest import mock
from uuid import UUID

import pytest

from libs.observability.config import SentryConfig
from libs.observability.sentry_backend import (
    SentryBackend,
    _serialize_context,
    _serialize_value,
)

# Check if sentry_sdk is available
try:
    import sentry_sdk

    HAS_SENTRY_SDK = True
except ImportError:
    HAS_SENTRY_SDK = False

requires_sentry_sdk = pytest.mark.skipif(
    not HAS_SENTRY_SDK, reason="sentry_sdk not installed"
)


class TestSerializationEdgeCases:
    """Tests for serialization edge cases not covered elsewhere."""

    def test_serialize_value_object_with_dict_raising_in_iteration(self) -> None:
        """Test serialization handles objects whose __dict__ iteration raises."""

        class BadIteration:
            def __init__(self) -> None:
                self._items = {"key": "value"}

            @property
            def __dict__(self) -> dict[str, Any]:  # type: ignore[override]
                # Return a dict-like that fails during iteration
                class FailingDict(dict[str, Any]):
                    def items(self) -> Any:
                        raise RuntimeError("Cannot iterate")

                return FailingDict(self._items)

        obj = BadIteration()
        # Should fall back to str() representation
        result = _serialize_value(obj)
        # Should succeed with string fallback
        assert "BadIteration" in str(result)

    def test_serialize_value_object_without_dict(self) -> None:
        """Test serialization of objects that fall through to str()."""

        class NoDict:
            __slots__ = ["value"]

            def __init__(self) -> None:
                self.value = 42

        obj = NoDict()
        # Should fall back to str() representation since slots don't have __dict__
        result = _serialize_value(obj)
        # str(obj) will be something like '<test_sentry_backend.NoDict object at 0x...>'
        assert "NoDict" in result

    def test_serialize_value_object_with_failing_str(self) -> None:
        """Test serialization handles objects whose str() raises."""

        class BadStr:
            __slots__: list[str] = []  # No __dict__

            def __str__(self) -> str:
                raise RuntimeError("Cannot stringify")

        obj = BadStr()
        # Should return unserializable placeholder
        result = _serialize_value(obj)
        assert "<unserializable:" in result
        assert "BadStr" in result

    def test_serialize_value_tuple(self) -> None:
        """Test serialization of tuples."""
        t = (1, "hello", UUID("12345678-1234-5678-1234-567812345678"))
        result = _serialize_value(t)
        assert result == [1, "hello", "12345678-1234-5678-1234-567812345678"]


class TestCaptureMessageInitialized:
    """Tests for capture_message when backend is initialized."""

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_message_calls_sdk(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_message calls SDK with correct parameters."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)
        mock_scope.capture_message.return_value = "event-id"

        result = backend.capture_message(
            "Test message",
            level="warning",
            context={"key": "value"},
            tags={"tag": "val"},
        )

        assert result == "event-id"
        mock_scope.set_context.assert_called_once_with("additional", {"key": "value"})
        mock_scope.set_tag.assert_called_once_with("tag", "val")
        assert mock_scope.level == "warning"
        mock_scope.capture_message.assert_called_once_with("Test message", level="warning")

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_message_default_level_is_info(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_message uses 'info' level by default."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)
        mock_scope.capture_message.return_value = "event-id"

        backend.capture_message("Test message")

        assert mock_scope.level == "info"
        mock_scope.capture_message.assert_called_once_with("Test message", level="info")

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_message_without_optional_params(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_message works without optional context/tags."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)
        mock_scope.capture_message.return_value = "event-id"

        result = backend.capture_message("Test message")

        assert result == "event-id"
        mock_scope.set_context.assert_not_called()
        mock_scope.set_tag.assert_not_called()

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_message_serializes_context(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_message serializes non-JSON types in context."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)
        mock_scope.capture_message.return_value = "event-id"

        context = {
            "timestamp": datetime(2024, 1, 15, 10, 30, 0),
            "user_id": UUID("12345678-1234-5678-1234-567812345678"),
        }
        backend.capture_message("Test message", context=context)

        call_args = mock_scope.set_context.call_args
        serialized_context = call_args[0][1]
        assert serialized_context["timestamp"] == "2024-01-15T10:30:00"
        assert serialized_context["user_id"] == "12345678-1234-5678-1234-567812345678"


class TestStartSpanInitialized:
    """Tests for start_span when backend is initialized."""

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.start_span")
    def test_start_span_calls_sdk(self, mock_start_span: mock.MagicMock) -> None:
        """Test start_span calls SDK with correct parameters."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_span = mock.MagicMock()
        mock_start_span.return_value.__enter__ = mock.MagicMock(return_value=mock_span)
        mock_start_span.return_value.__exit__ = mock.MagicMock(return_value=False)

        with backend.start_span("test_operation") as span:
            assert span is mock_span

        mock_start_span.assert_called_once_with(op="function", description="test_operation")

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.start_span")
    def test_start_span_with_attributes(self, mock_start_span: mock.MagicMock) -> None:
        """Test start_span sets attributes on the span."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_span = mock.MagicMock()
        mock_start_span.return_value.__enter__ = mock.MagicMock(return_value=mock_span)
        mock_start_span.return_value.__exit__ = mock.MagicMock(return_value=False)

        with backend.start_span("test_operation", attributes={"key": "value", "count": 42}):
            pass

        mock_span.set_data.assert_any_call("key", "value")
        mock_span.set_data.assert_any_call("count", 42)
        assert mock_span.set_data.call_count == 2


class TestFlushMethod:
    """Tests for flush lifecycle method."""

    def test_flush_when_not_initialized(self) -> None:
        """Test flush does nothing when not initialized."""
        config = SentryConfig(enabled=False)
        backend = SentryBackend(config)
        # Should not raise
        backend.flush()

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.flush")
    def test_flush_calls_sdk(self, mock_flush: mock.MagicMock) -> None:
        """Test flush calls SDK with default timeout."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        backend.flush()

        mock_flush.assert_called_once_with(timeout=2.0)

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.flush")
    def test_flush_with_custom_timeout(self, mock_flush: mock.MagicMock) -> None:
        """Test flush calls SDK with custom timeout."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        backend.flush(timeout=5.0)

        mock_flush.assert_called_once_with(timeout=5.0)


class TestShutdownMethod:
    """Tests for shutdown lifecycle method."""

    def test_shutdown_when_not_initialized(self) -> None:
        """Test shutdown does nothing when not initialized."""
        config = SentryConfig(enabled=False)
        backend = SentryBackend(config)
        # Should not raise
        backend.shutdown()

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.get_client")
    def test_shutdown_calls_client_close(self, mock_get_client: mock.MagicMock) -> None:
        """Test shutdown calls client.close() with timeout."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_client = mock.MagicMock()
        mock_get_client.return_value = mock_client

        backend.shutdown()

        mock_client.close.assert_called_once_with(timeout=2.0)
        assert backend._initialized is False

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.get_client")
    def test_shutdown_handles_none_client(self, mock_get_client: mock.MagicMock) -> None:
        """Test shutdown handles case where client is None."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_get_client.return_value = None

        # Should not raise
        backend.shutdown()
        assert backend._initialized is False


class TestNotInitializedBranches:
    """Tests for method branches when backend is not initialized."""

    def test_set_tag_when_not_initialized(self) -> None:
        """Test set_tag does nothing when not initialized."""
        config = SentryConfig(enabled=False)
        backend = SentryBackend(config)
        # Should not raise
        backend.set_tag("key", "value")

    def test_set_context_when_not_initialized(self) -> None:
        """Test set_context does nothing when not initialized."""
        config = SentryConfig(enabled=False)
        backend = SentryBackend(config)
        # Should not raise
        backend.set_context("request", {"url": "/test"})


class TestExceptionTypes:
    """Tests for capture_error with various exception types."""

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_error_with_value_error(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_error with ValueError."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)
        mock_scope.capture_exception.return_value = "event-id"

        exc = ValueError("invalid input")
        result = backend.capture_error(exc)

        assert result == "event-id"
        mock_scope.capture_exception.assert_called_once_with(exc)

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_error_with_type_error(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_error with TypeError."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)
        mock_scope.capture_exception.return_value = "event-id"

        exc = TypeError("wrong type")
        result = backend.capture_error(exc)

        assert result == "event-id"
        mock_scope.capture_exception.assert_called_once_with(exc)

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_error_with_runtime_error(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_error with RuntimeError."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)
        mock_scope.capture_exception.return_value = "event-id"

        exc = RuntimeError("something went wrong")
        result = backend.capture_error(exc)

        assert result == "event-id"
        mock_scope.capture_exception.assert_called_once_with(exc)

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_error_with_custom_exception(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_error with a custom exception class."""

        class CustomError(Exception):
            pass

        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)
        mock_scope.capture_exception.return_value = "event-id"

        exc = CustomError("custom error")
        result = backend.capture_error(exc)

        assert result == "event-id"
        mock_scope.capture_exception.assert_called_once_with(exc)

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_error_with_keyboard_interrupt(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_error with KeyboardInterrupt (BaseException subclass)."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)
        mock_scope.capture_exception.return_value = "event-id"

        exc = KeyboardInterrupt()
        result = backend.capture_error(exc)

        assert result == "event-id"
        mock_scope.capture_exception.assert_called_once_with(exc)


class TestCaptureErrorWithContext:
    """Tests for capture_error with various context scenarios."""

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_error_with_request_context(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_error with request context information."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)
        mock_scope.capture_exception.return_value = "event-id"

        context = {
            "request_id": UUID("12345678-1234-5678-1234-567812345678"),
            "path": "/v1/users",
            "method": "POST",
            "user_agent": "AIQ-iOS/1.0",
        }
        backend.capture_error(ValueError("test"), context=context)

        call_args = mock_scope.set_context.call_args
        serialized = call_args[0][1]
        assert serialized["request_id"] == "12345678-1234-5678-1234-567812345678"
        assert serialized["path"] == "/v1/users"
        assert serialized["method"] == "POST"
        assert serialized["user_agent"] == "AIQ-iOS/1.0"

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_error_with_user_context(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_error with user context information."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)
        mock_scope.capture_exception.return_value = "event-id"

        user = {"id": "user-123", "email": "test@example.com", "username": "testuser"}
        backend.capture_error(ValueError("test"), user=user)

        mock_scope.set_user.assert_called_once_with(user)

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_error_with_multiple_tags(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_error with multiple tags."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)
        mock_scope.capture_exception.return_value = "event-id"

        tags = {"service": "api", "endpoint": "/v1/tests", "version": "1.0.0"}
        backend.capture_error(ValueError("test"), tags=tags)

        assert mock_scope.set_tag.call_count == 3
        mock_scope.set_tag.assert_any_call("service", "api")
        mock_scope.set_tag.assert_any_call("endpoint", "/v1/tests")
        mock_scope.set_tag.assert_any_call("version", "1.0.0")


class TestIntegrationImportFailures:
    """Tests for graceful handling when integrations are unavailable."""

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.init")
    @mock.patch.dict("sys.modules", {"sentry_sdk.integrations.fastapi": None})
    def test_init_without_fastapi_integration(self, mock_init: mock.MagicMock) -> None:
        """Test init() succeeds when FastAPI integration is not available."""
        # Force reimport to trigger ImportError
        import importlib

        import libs.observability.sentry_backend as sb

        importlib.reload(sb)

        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = sb.SentryBackend(config)
        result = backend.init()

        assert result is True
        mock_init.assert_called_once()

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.init")
    @mock.patch.dict("sys.modules", {"sentry_sdk.integrations.starlette": None})
    def test_init_without_starlette_integration(self, mock_init: mock.MagicMock) -> None:
        """Test init() succeeds when Starlette integration is not available."""
        # Force reimport to trigger ImportError
        import importlib

        import libs.observability.sentry_backend as sb

        importlib.reload(sb)

        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = sb.SentryBackend(config)
        result = backend.init()

        assert result is True
        mock_init.assert_called_once()


class TestAllErrorLevels:
    """Tests for all valid error levels."""

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    @pytest.mark.parametrize(
        "level",
        ["debug", "info", "warning", "error", "fatal"],
        ids=["debug", "info", "warning", "error", "fatal"],
    )
    def test_capture_error_with_all_levels(
        self,
        mock_new_scope: mock.MagicMock,
        level: str,
    ) -> None:
        """Test capture_error with all valid severity levels."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)
        mock_scope.capture_exception.return_value = "event-id"

        backend.capture_error(ValueError("test"), level=level)
        assert mock_scope.level == level
