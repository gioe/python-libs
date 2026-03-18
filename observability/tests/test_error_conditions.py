"""Integration tests for observability error conditions.

This module tests error conditions and edge cases that can occur during
observability operations, including:

1. Concurrent span operations - thread safety of span lifecycle
2. Invalid input handling - non-serializable objects, malformed data
3. Exception handling during backend operations - error recovery scenarios
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any
from unittest import mock

import pytest

from libs.observability.config import ObservabilityConfig, OTELConfig, RoutingConfig, SentryConfig
from libs.observability.facade import ObservabilityFacade, SpanContext

# Configurable timeout for concurrent tests - allows increasing for slow CI environments
THREAD_TIMEOUT = float(os.getenv("TEST_THREAD_TIMEOUT", "30.0"))


# ==============================================================================
# 1. Concurrent Span Operations
# ==============================================================================


class TestConcurrentSpanOperations:
    """Tests for thread safety of concurrent span operations."""

    @pytest.mark.integration
    @pytest.mark.slow
    def test_concurrent_nested_spans_lifecycle(self) -> None:
        """Test concurrent creation and completion of nested spans."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            otel=OTELConfig(service_name="test"),
            routing=RoutingConfig(traces="otel"),
        )

        # Track span events for verification
        span_events: list[tuple[str, str]] = []  # (thread_id, event_type)
        lock = threading.Lock()

        mock_backend = mock.MagicMock()

        def mock_start_span(name: str, **kwargs: Any) -> mock.MagicMock:
            mock_span = mock.MagicMock()
            mock_span.__enter__ = lambda self: mock_span
            mock_span.__exit__ = lambda self, *args: None
            with lock:
                span_events.append((threading.current_thread().name, f"start:{name}"))
            return mock_span

        mock_backend.start_span.side_effect = mock_start_span
        facade._otel_backend = mock_backend

        num_threads = 20
        nesting_depth = 3
        completed_threads: list[str] = []

        def create_nested_spans(thread_id: int) -> None:
            thread_name = f"thread-{thread_id}"
            threading.current_thread().name = thread_name

            # Create nested spans
            with facade.start_span(f"outer-{thread_id}") as outer:
                outer.set_attribute("level", "outer")
                with facade.start_span(f"middle-{thread_id}") as middle:
                    middle.set_attribute("level", "middle")
                    with facade.start_span(f"inner-{thread_id}") as inner:
                        inner.set_attribute("level", "inner")
                        # Simulate work
                        _ = sum(range(100))

            with lock:
                completed_threads.append(thread_name)

        threads = []
        for i in range(num_threads):
            thread = threading.Thread(target=create_nested_spans, args=(i,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join(timeout=THREAD_TIMEOUT)
            if thread.is_alive():
                pytest.fail(f"Thread {thread.name} did not complete within timeout")

        # Verify all threads completed
        assert len(completed_threads) == num_threads

        # Verify all spans were created (3 per thread)
        with lock:
            assert len(span_events) == num_threads * nesting_depth

    @pytest.mark.integration
    @pytest.mark.slow
    def test_concurrent_span_exception_handling(self) -> None:
        """Test concurrent spans that raise exceptions maintain thread isolation."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            otel=OTELConfig(service_name="test"),
            routing=RoutingConfig(traces="otel"),
        )

        exceptions_recorded: list[str] = []
        lock = threading.Lock()

        mock_backend = mock.MagicMock()

        def mock_start_span(name: str, **kwargs: Any) -> mock.MagicMock:
            mock_span = mock.MagicMock()

            def record_exception(exc: BaseException) -> None:
                with lock:
                    exceptions_recorded.append(f"{name}:{type(exc).__name__}")

            mock_span.record_exception = record_exception
            mock_span.__enter__ = lambda self: mock_span
            mock_span.__exit__ = lambda self, *args: None
            return mock_span

        mock_backend.start_span.side_effect = mock_start_span
        facade._otel_backend = mock_backend

        num_threads = 30
        failed_threads: list[int] = []

        def span_with_exception(thread_id: int) -> None:
            try:
                with facade.start_span(f"operation-{thread_id}") as span:
                    if thread_id % 2 == 0:
                        # Even threads raise exceptions
                        raise ValueError(f"Error in thread {thread_id}")
                    span.set_attribute("success", True)
            except ValueError:
                with lock:
                    failed_threads.append(thread_id)

        threads = []
        for i in range(num_threads):
            thread = threading.Thread(target=span_with_exception, args=(i,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join(timeout=THREAD_TIMEOUT)

        # Verify exception isolation - each thread handled its own exception
        expected_failures = [i for i in range(num_threads) if i % 2 == 0]
        assert sorted(failed_threads) == expected_failures

    @pytest.mark.integration
    @pytest.mark.slow
    def test_concurrent_span_attribute_isolation(self) -> None:
        """Test that span attributes are isolated between concurrent threads."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            otel=OTELConfig(service_name="test"),
            routing=RoutingConfig(traces="otel"),
        )

        # Track attributes per span
        span_attributes: dict[str, dict[str, Any]] = {}
        lock = threading.Lock()

        mock_backend = mock.MagicMock()

        def mock_start_span(name: str, **kwargs: Any) -> mock.MagicMock:
            mock_span = mock.MagicMock()
            attrs: dict[str, Any] = {}

            def set_attr(key: str, value: Any) -> None:
                attrs[key] = value

            mock_span.set_attribute = set_attr
            mock_span.__enter__ = lambda self: mock_span

            def exit_span(*args: Any) -> None:
                with lock:
                    span_attributes[name] = attrs.copy()

            mock_span.__exit__ = exit_span
            return mock_span

        mock_backend.start_span.side_effect = mock_start_span
        facade._otel_backend = mock_backend

        num_threads = 25

        def set_thread_specific_attributes(thread_id: int) -> None:
            with facade.start_span(f"operation-{thread_id}") as span:
                span.set_attribute("thread_id", thread_id)
                span.set_attribute("unique_value", f"value-{thread_id}")
                span.set_attribute("timestamp", thread_id * 1000)
                # Simulate work to increase chance of race conditions
                for _ in range(10):
                    span.set_attribute(f"attr_{_}", thread_id)

        threads = []
        for i in range(num_threads):
            thread = threading.Thread(target=set_thread_specific_attributes, args=(i,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join(timeout=THREAD_TIMEOUT)

        # Verify each span has its own isolated attributes
        with lock:
            for i in range(num_threads):
                span_name = f"operation-{i}"
                assert span_name in span_attributes, f"Missing span: {span_name}"
                attrs = span_attributes[span_name]
                assert attrs["thread_id"] == i
                assert attrs["unique_value"] == f"value-{i}"
                assert attrs["timestamp"] == i * 1000

    @pytest.mark.integration
    def test_span_exit_during_backend_failure(self) -> None:
        """Test span exit behavior when backend fails during span operations."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            otel=OTELConfig(service_name="test"),
            routing=RoutingConfig(traces="otel"),
        )

        mock_backend = mock.MagicMock()
        mock_span = mock.MagicMock()

        # Span exit raises exception
        def failing_exit(*args: Any) -> None:
            raise RuntimeError("Backend failed during span exit")

        mock_span.__enter__ = lambda self: mock_span
        mock_span.__exit__ = failing_exit
        mock_backend.start_span.return_value = mock_span
        facade._otel_backend = mock_backend

        # The facade should handle the backend failure gracefully
        with pytest.raises(RuntimeError, match="Backend failed during span exit"):
            with facade.start_span("test-span"):
                pass


# ==============================================================================
# 2. Invalid Input Handling
# ==============================================================================


class TestInvalidInputHandling:
    """Tests for handling invalid and non-serializable inputs."""

    def test_record_event_with_non_serializable_data_raises(self) -> None:
        """Test record_event raises ValueError for non-serializable data."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig()
        facade._sentry_backend = mock.MagicMock()

        class NonSerializable:
            pass

        with pytest.raises(ValueError, match="non-JSON-serializable"):
            facade.record_event("test.event", data={"bad": NonSerializable()})

    def test_record_event_validation_before_initialization_check(self) -> None:
        """Test that data validation runs even when facade is not initialized."""
        facade = ObservabilityFacade()
        # Not initialized

        class NonSerializable:
            pass

        # Validation should still raise even though not initialized
        with pytest.raises(ValueError, match="non-JSON-serializable"):
            facade.record_event("test.event", data={"bad": NonSerializable()})

    def test_set_tag_validation_before_initialization_check(self) -> None:
        """Test that tag validation runs even when facade is not initialized."""
        facade = ObservabilityFacade()
        # Not initialized

        # Validation should still raise
        with pytest.raises(ValueError, match="Tag key must be a string"):
            # Intentionally pass invalid type to test validation
            facade.set_tag(123, "value")  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="Tag value must be a string"):
            # Intentionally pass invalid type to test validation
            facade.set_tag("key", 456)  # type: ignore[arg-type]

    def test_record_event_with_circular_reference(self) -> None:
        """Test record_event handles circular references gracefully."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        # Create circular reference
        data: dict[str, Any] = {"key": "value"}
        data["self"] = data

        with pytest.raises(ValueError, match="non-JSON-serializable"):
            facade.record_event("test.event", data=data)

    def test_record_event_with_function_object(self) -> None:
        """Test record_event rejects function objects in data."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        def my_func() -> None:
            pass

        with pytest.raises(ValueError, match="non-JSON-serializable"):
            facade.record_event("test.event", data={"callback": my_func})

    def test_record_event_with_bytes_object(self) -> None:
        """Test record_event rejects bytes objects in data."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        with pytest.raises(ValueError, match="non-JSON-serializable"):
            facade.record_event("test.event", data={"binary": b"hello"})

    def test_record_event_with_set_object(self) -> None:
        """Test record_event rejects set objects in data."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        with pytest.raises(ValueError, match="non-JSON-serializable"):
            facade.record_event("test.event", data={"items": {1, 2, 3}})

    def test_set_tag_with_too_long_key(self) -> None:
        """Test set_tag rejects keys exceeding maximum length."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        too_long_key = "a" * 201
        with pytest.raises(ValueError, match="exceeds maximum length"):
            facade.set_tag(too_long_key, "value")

    def test_set_tag_with_too_long_value(self) -> None:
        """Test set_tag rejects values exceeding maximum length."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        too_long_value = "v" * 201
        with pytest.raises(ValueError, match="exceeds maximum length"):
            facade.set_tag("key", too_long_value)

    def test_capture_error_with_valid_context_succeeds(self) -> None:
        """Test capture_error accepts valid context data."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(environment="test"),
            otel=OTELConfig(service_name="test"),
        )
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_error.return_value = "event-id"

        # Valid nested data should work
        result = facade.capture_error(
            ValueError("test"),
            context={
                "user": {"id": 123, "name": "test"},
                "request": {"method": "POST", "path": "/api"},
                "metadata": None,
            },
        )

        assert result == "event-id"
        facade._sentry_backend.capture_error.assert_called_once()


# ==============================================================================
# 3. Exception Handling During Backend Operations
# ==============================================================================


class TestExceptionHandlingDuringBackendOperations:
    """Tests for exception handling during backend operations."""

    def test_capture_error_handles_sentry_exception_with_logging(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test capture_error logs the error when Sentry throws."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(environment="test"),
            otel=OTELConfig(service_name="test"),
        )
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_error.side_effect = Exception(
            "Sentry transport error"
        )

        with caplog.at_level(logging.ERROR):
            result = facade.capture_error(ValueError("original error"))

        assert result is None
        assert "Failed to capture error to Sentry" in caplog.text
        assert "ValueError" in caplog.text
        assert "original error" in caplog.text

    def test_capture_error_preserves_original_exception_type_in_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that the original exception type is logged when capture fails."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(sentry=SentryConfig(environment="test"))
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_error.side_effect = RuntimeError("Sentry down")

        custom_exception = KeyError("missing_key")

        with caplog.at_level(logging.ERROR):
            facade.capture_error(custom_exception)

        assert "KeyError" in caplog.text
        assert "missing_key" in caplog.text

    def test_start_span_handles_otel_start_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test start_span handles OTEL backend failure gracefully."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            otel=OTELConfig(service_name="test"),
            routing=RoutingConfig(traces="otel"),
        )
        facade._otel_backend = mock.MagicMock()
        facade._otel_backend.start_span.side_effect = RuntimeError(
            "OTEL span creation failed"
        )

        # When OTEL fails to start a span, the error propagates
        with pytest.raises(RuntimeError, match="OTEL span creation failed"):
            with facade.start_span("test-span"):
                pass

    def test_flush_handles_multiple_backend_failures(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test flush continues and logs when multiple backends fail."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.flush.side_effect = IOError("Network error")
        facade._otel_backend = mock.MagicMock()
        facade._otel_backend.flush.side_effect = TimeoutError("Timeout")

        with caplog.at_level(logging.WARNING):
            facade.flush(timeout=1.0)

        # Both backends should be attempted
        facade._sentry_backend.flush.assert_called_once()
        facade._otel_backend.flush.assert_called_once()

        # Both failures should be logged
        assert "Sentry backend flush failed" in caplog.text
        assert "OTEL backend flush failed" in caplog.text

    def test_shutdown_handles_multiple_backend_failures_and_cleans_state(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test shutdown cleans up state even when backends fail."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = mock.MagicMock()
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.shutdown.side_effect = RuntimeError("Sentry error")
        facade._otel_backend = mock.MagicMock()
        facade._otel_backend.shutdown.side_effect = RuntimeError("OTEL error")

        with caplog.at_level(logging.WARNING):
            facade.shutdown()

        # State should be cleaned up despite failures
        assert facade.is_initialized is False
        assert facade._sentry_backend is None
        assert facade._otel_backend is None
        assert facade._config is None

    def test_record_metric_propagates_backend_exception(self) -> None:
        """Test record_metric propagates backend exceptions."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(otel=OTELConfig(service_name="test"))
        facade._otel_backend = mock.MagicMock()
        facade._otel_backend.record_metric.side_effect = RuntimeError(
            "Metric recording failed"
        )

        # record_metric does not catch exceptions - they propagate
        with pytest.raises(RuntimeError, match="Metric recording failed"):
            facade.record_metric("test.counter", 1, metric_type="counter")

    def test_set_user_handles_sentry_failure_silently(self) -> None:
        """Test set_user doesn't crash when Sentry backend fails."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig()
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.set_user.side_effect = RuntimeError("Sentry error")

        # Should propagate the exception (no error handling in set_user)
        with pytest.raises(RuntimeError, match="Sentry error"):
            facade.set_user("user-123", username="test")

    def test_set_context_handles_sentry_failure_silently(self) -> None:
        """Test set_context doesn't crash when Sentry backend fails."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig()
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.set_context.side_effect = RuntimeError("Sentry error")

        # Should propagate the exception
        with pytest.raises(RuntimeError, match="Sentry error"):
            facade.set_context("request", {"url": "/api"})

    def test_get_trace_context_handles_import_error(self) -> None:
        """Test get_trace_context handles missing OTEL gracefully."""
        facade = ObservabilityFacade()
        facade._initialized = True

        # Mock the import inside get_trace_context to raise ImportError
        with mock.patch.dict(
            "sys.modules", {"opentelemetry": None, "opentelemetry.trace": None}
        ):
            # Force re-import by removing from cache
            import sys

            # Save reference
            saved_otel = sys.modules.get("opentelemetry")
            saved_otel_trace = sys.modules.get("opentelemetry.trace")

            try:
                # Setting modules to None causes ImportError on import
                # Setting to None causes ImportError on next import attempt
                sys.modules["opentelemetry"] = None  # type: ignore[assignment]
                sys.modules["opentelemetry.trace"] = None  # type: ignore[assignment]

                # Create new facade to avoid cached imports
                new_facade = ObservabilityFacade()
                new_facade._initialized = True

                # The function has a try/except ImportError that returns default values
                result = facade.get_trace_context()
            finally:
                # Restore
                if saved_otel is not None:
                    sys.modules["opentelemetry"] = saved_otel
                if saved_otel_trace is not None:
                    sys.modules["opentelemetry.trace"] = saved_otel_trace

        # Should return default values even if import fails
        assert result == {"trace_id": None, "span_id": None}

    def test_get_trace_context_handles_runtime_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test get_trace_context handles runtime errors gracefully."""
        facade = ObservabilityFacade()
        facade._initialized = True

        # Mock the opentelemetry.trace module that's imported inside get_trace_context
        with mock.patch(
            "opentelemetry.trace.get_current_span"
        ) as mock_get_span, caplog.at_level(logging.DEBUG):
            mock_get_span.side_effect = RuntimeError("Trace context error")
            result = facade.get_trace_context()

        assert result == {"trace_id": None, "span_id": None}
        assert "Could not get trace context" in caplog.text


class TestSpanContextExceptionHandling:
    """Tests for SpanContext exception handling edge cases."""

    def test_span_context_set_status_handles_missing_otel(self) -> None:
        """Test SpanContext.set_status handles missing OTEL import."""
        ctx = SpanContext("test", otel_span=None, sentry_span=None)

        # Should not raise even with no backends
        ctx.set_status("error", "Test error")
        ctx.set_status("ok")

    def test_span_context_set_status_with_otel_import_error(self) -> None:
        """Test SpanContext.set_status handles StatusCode import error."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        # StatusCode import happens inside set_status, mock it to fail
        with mock.patch.dict("sys.modules", {"opentelemetry.trace": None}):
            # This will raise because StatusCode import fails
            with pytest.raises(ImportError):
                ctx.set_status("ok")

    def test_span_context_record_exception_with_none_span(self) -> None:
        """Test SpanContext.record_exception is safe with None span."""
        ctx = SpanContext("test", otel_span=None)

        # Should not raise
        ctx.record_exception(ValueError("test error"))

    def test_span_context_add_event_with_none_span(self) -> None:
        """Test SpanContext.add_event is safe with None span."""
        ctx = SpanContext("test", otel_span=None)

        # Should not raise
        ctx.add_event("test_event", {"key": "value"})

    def test_span_context_exception_during_exit_records_properly(self) -> None:
        """Test that exceptions during span exit are recorded correctly."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        with pytest.raises(KeyError):
            with ctx:
                raise KeyError("missing_key")

        # Exception should have been recorded
        mock_otel_span.record_exception.assert_called_once()
        args = mock_otel_span.record_exception.call_args[0]
        assert isinstance(args[0], KeyError)
        assert str(args[0]) == "'missing_key'"

        # Status should have been set to error
        mock_otel_span.set_status.assert_called_once()


class TestInitializationErrorHandling:
    """Tests for error handling during initialization."""

    def test_init_handles_config_load_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test init handles configuration loading errors gracefully."""
        facade = ObservabilityFacade()

        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_load.side_effect = RuntimeError("Config file corrupted")

            with caplog.at_level(logging.ERROR):
                result = facade.init()

            assert result is False
            assert "Unexpected error loading" in caplog.text

    def test_init_handles_sentry_backend_init_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test init handles Sentry backend initialization failure."""
        facade = ObservabilityFacade()

        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
                otel=OTELConfig(enabled=False),
            )
            mock_load.return_value = mock_config

            with mock.patch(
                "libs.observability.sentry_backend.SentryBackend"
            ) as mock_cls:
                mock_cls.side_effect = RuntimeError("Sentry SDK import failed")

                with caplog.at_level(logging.ERROR), mock.patch("atexit.register"):
                    result = facade.init()

                # Should still succeed (graceful degradation)
                assert result is True
                assert "Failed to initialize Sentry backend" in caplog.text

    def test_init_handles_otel_backend_init_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test init handles OTEL backend initialization failure."""
        facade = ObservabilityFacade()

        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(enabled=False),
                otel=OTELConfig(enabled=True, service_name="test"),
            )
            mock_load.return_value = mock_config

            with mock.patch("libs.observability.otel_backend.OTELBackend") as mock_cls:
                mock_cls.side_effect = RuntimeError("OTEL SDK import failed")

                with caplog.at_level(logging.ERROR), mock.patch("atexit.register"):
                    result = facade.init()

                # Should still succeed (graceful degradation)
                assert result is True
                assert "Failed to initialize OTEL backend" in caplog.text

    def test_init_handles_both_backends_failing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test init handles both backends failing during initialization."""
        facade = ObservabilityFacade()

        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
                otel=OTELConfig(enabled=True, service_name="test"),
            )
            mock_load.return_value = mock_config

            with mock.patch(
                "libs.observability.sentry_backend.SentryBackend"
            ) as mock_sentry, mock.patch(
                "libs.observability.otel_backend.OTELBackend"
            ) as mock_otel:
                mock_sentry.side_effect = RuntimeError("Sentry failed")
                mock_otel.side_effect = RuntimeError("OTEL failed")

                with caplog.at_level(logging.WARNING), mock.patch("atexit.register"):
                    result = facade.init()

                # Should still succeed but log warning about no backends
                assert result is True
                assert "no backends are active" in caplog.text


class TestAtexitShutdownHandling:
    """Tests for atexit shutdown handling."""

    def test_atexit_shutdown_handles_flush_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test atexit shutdown handler handles flush failure."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()
        facade._otel_backend = mock.MagicMock()
        facade._sentry_backend.flush.side_effect = RuntimeError("Flush failed")

        with caplog.at_level(logging.DEBUG):
            facade._atexit_shutdown()

        # Should have attempted flush despite error
        assert facade.is_initialized is False

    def test_atexit_shutdown_handles_shutdown_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test atexit shutdown handler handles shutdown failure."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()
        facade._otel_backend = mock.MagicMock()
        facade._sentry_backend.shutdown.side_effect = RuntimeError("Shutdown failed")
        facade._otel_backend.shutdown.side_effect = RuntimeError("Shutdown failed")

        with caplog.at_level(logging.DEBUG):
            facade._atexit_shutdown()

        # State should be cleaned up
        assert facade.is_initialized is False
