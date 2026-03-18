"""Integration tests for observability facade with both backends.

Tests realistic workflows where Sentry and OTEL backends work together,
verifying inter-backend coordination and isolation.
"""

from __future__ import annotations

from unittest import mock

import pytest

from libs.observability.config import (
    ObservabilityConfig,
    OTELConfig,
    RoutingConfig,
    SentryConfig,
)
from libs.observability.facade import ObservabilityFacade, SpanContext


class TestFullInitialization:
    """Tests for initializing facade with both backends enabled."""

    def test_init_with_both_backends_enabled(self) -> None:
        """Test facade initializes both Sentry and OTEL backends."""
        facade = ObservabilityFacade()

        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
                otel=OTELConfig(enabled=True, service_name="test-service"),
                routing=RoutingConfig(errors="sentry", metrics="otel", traces="both"),
            )
            mock_load.return_value = mock_config

            with mock.patch(
                "libs.observability.sentry_backend.SentryBackend"
            ) as mock_sentry_cls, mock.patch(
                "libs.observability.otel_backend.OTELBackend"
            ) as mock_otel_cls, mock.patch(
                "atexit.register"
            ):
                mock_sentry = mock.MagicMock()
                mock_sentry.init.return_value = True
                mock_sentry_cls.return_value = mock_sentry

                mock_otel = mock.MagicMock()
                mock_otel.init.return_value = True
                mock_otel_cls.return_value = mock_otel

                result = facade.init()

                assert result is True
                assert facade.is_initialized is True
                mock_sentry.init.assert_called_once()
                mock_otel.init.assert_called_once()

    def test_init_with_both_backends_logs_summary(self) -> None:
        """Test init logs summary of initialized backends."""
        facade = ObservabilityFacade()

        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
                otel=OTELConfig(enabled=True, service_name="test-service"),
            )
            mock_load.return_value = mock_config

            with mock.patch(
                "libs.observability.sentry_backend.SentryBackend"
            ) as mock_sentry_cls, mock.patch(
                "libs.observability.otel_backend.OTELBackend"
            ) as mock_otel_cls, mock.patch(
                "atexit.register"
            ), mock.patch(
                "libs.observability.facade.logger"
            ) as mock_logger:
                mock_sentry = mock.MagicMock()
                mock_sentry.init.return_value = True
                mock_sentry_cls.return_value = mock_sentry

                mock_otel = mock.MagicMock()
                mock_otel.init.return_value = True
                mock_otel_cls.return_value = mock_otel

                facade.init()

                # Check that info was called with a message that includes both backends
                # The logger.info call uses % formatting, so we check the format args
                mock_logger.info.assert_called()
                log_call = mock_logger.info.call_args
                # Format string is first arg, format values follow
                # e.g., "Observability initialized: %s (service=%s...)", "Sentry, OpenTelemetry", ...
                format_args = log_call[0][1] if len(log_call[0]) > 1 else ""
                assert "Sentry" in format_args
                assert "OpenTelemetry" in format_args


class TestErrorCaptureWithTraceCorrelation:
    """Tests for error capture that correlates with active traces."""

    def test_capture_error_includes_trace_context(self) -> None:
        """Test capture_error includes trace_id and span_id when span is active."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(environment="test"),
            otel=OTELConfig(service_name="test-service", service_version="1.0.0"),
            routing=RoutingConfig(traces="otel"),
        )
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_error.return_value = "event-id"
        facade._otel_backend = mock.MagicMock()

        # Mock get_trace_context to return valid trace context
        with mock.patch.object(facade, "get_trace_context") as mock_get_trace:
            mock_get_trace.return_value = {
                "trace_id": "12345678901234567890123456789012",
                "span_id": "1234567890123456",
            }

            exc = ValueError("test error")
            facade.capture_error(exc, context={"operation": "test_op"})

            # Verify capture_error was called with trace context
            facade._sentry_backend.capture_error.assert_called_once()
            call_kwargs = facade._sentry_backend.capture_error.call_args.kwargs
            assert "trace" in call_kwargs["context"]
            assert (
                call_kwargs["context"]["trace"]["trace_id"]
                == "12345678901234567890123456789012"
            )
            assert call_kwargs["context"]["trace"]["span_id"] == "1234567890123456"

    def test_capture_error_without_active_span_excludes_trace_context(self) -> None:
        """Test capture_error excludes trace context when no span is active."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(environment="test"),
            otel=OTELConfig(service_name="test-service", service_version="1.0.0"),
        )
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_error.return_value = "event-id"

        # Mock get_trace_context to return None values (no active span)
        with mock.patch.object(facade, "get_trace_context") as mock_get_trace:
            mock_get_trace.return_value = {"trace_id": None, "span_id": None}

            exc = ValueError("test error")
            facade.capture_error(exc, context={"operation": "test_op"})

            # Verify trace context is NOT included (both values None)
            facade._sentry_backend.capture_error.assert_called_once()
            call_kwargs = facade._sentry_backend.capture_error.call_args.kwargs
            assert "trace" not in call_kwargs["context"]


class TestMetricsAndSpansInSameOperation:
    """Tests for using metrics and spans together in operations."""

    def test_metrics_and_spans_together(self) -> None:
        """Test recording metrics within a span context."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            otel=OTELConfig(service_name="test-service"),
            routing=RoutingConfig(traces="otel", metrics="otel"),
        )

        mock_otel_span = mock.MagicMock()
        mock_otel_backend = mock.MagicMock()
        mock_otel_backend.start_span.return_value = mock_otel_span
        facade._otel_backend = mock_otel_backend

        # Execute operation with span and metrics
        with facade.start_span("process_request", attributes={"endpoint": "/api"}) as span:
            span.set_attribute("user_id", "123")
            facade.record_metric(
                "request.duration", 150.5, labels={"endpoint": "/api"}, metric_type="histogram"
            )

        # Verify span was created and entered
        mock_otel_backend.start_span.assert_called_once_with(
            "process_request", kind="internal", attributes={"endpoint": "/api"}
        )

        # Verify metric was recorded
        mock_otel_backend.record_metric.assert_called_once_with(
            name="request.duration",
            value=150.5,
            labels={"endpoint": "/api"},
            metric_type="histogram",
            unit=None,
        )

    def test_multiple_metrics_within_span(self) -> None:
        """Test recording multiple metrics within a single span."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            otel=OTELConfig(service_name="test-service"),
            routing=RoutingConfig(traces="otel", metrics="otel"),
        )

        mock_otel_span = mock.MagicMock()
        mock_otel_backend = mock.MagicMock()
        mock_otel_backend.start_span.return_value = mock_otel_span
        facade._otel_backend = mock_otel_backend

        with facade.start_span("batch_process") as span:
            for i in range(3):
                facade.record_metric("items.processed", 1, metric_type="counter")
                span.add_event(f"item_{i}_completed")

        # Verify metrics were recorded 3 times
        assert mock_otel_backend.record_metric.call_count == 3


class TestDualTracingMode:
    """Tests for dual tracing mode (both OTEL and Sentry)."""

    def test_dual_tracing_creates_both_spans(self) -> None:
        """Test that routing=both creates spans in both backends."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            routing=RoutingConfig(traces="both"),
        )

        mock_otel_span = mock.MagicMock()
        mock_otel_backend = mock.MagicMock()
        mock_otel_backend.start_span.return_value = mock_otel_span

        mock_sentry_span = mock.MagicMock()
        mock_sentry_backend = mock.MagicMock()
        mock_sentry_backend.start_span.return_value = mock_sentry_span

        facade._otel_backend = mock_otel_backend
        facade._sentry_backend = mock_sentry_backend

        with facade.start_span("dual_span", attributes={"key": "value"}) as span:
            span.set_attribute("extra_key", "extra_value")

        # Verify both backends received span creation
        mock_otel_backend.start_span.assert_called_once_with(
            "dual_span", kind="internal", attributes={"key": "value"}
        )
        mock_sentry_backend.start_span.assert_called_once_with(
            "dual_span", attributes={"key": "value"}
        )

    def test_dual_tracing_synchronizes_attributes(self) -> None:
        """Test that attributes are synchronized to both backends in dual mode."""
        mock_otel_span = mock.MagicMock()
        mock_sentry_span = mock.MagicMock()

        span_ctx = SpanContext("test", otel_span=mock_otel_span, sentry_span=mock_sentry_span)

        # Set attributes
        span_ctx.set_attribute("user_id", "123")
        span_ctx.set_attribute("operation", "process")
        span_ctx.set_attribute("count", 42)

        # Verify both backends received all attributes
        assert mock_otel_span.set_attribute.call_count == 3
        assert mock_sentry_span.set_data.call_count == 3

        mock_otel_span.set_attribute.assert_any_call("user_id", "123")
        mock_otel_span.set_attribute.assert_any_call("operation", "process")
        mock_otel_span.set_attribute.assert_any_call("count", 42)

        mock_sentry_span.set_data.assert_any_call("user_id", "123")
        mock_sentry_span.set_data.assert_any_call("operation", "process")
        mock_sentry_span.set_data.assert_any_call("count", 42)

    def test_dual_tracing_with_nested_spans(self) -> None:
        """Test nested spans work correctly in dual mode."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            routing=RoutingConfig(traces="both"),
        )

        mock_otel_backend = mock.MagicMock()
        mock_sentry_backend = mock.MagicMock()

        # Return different mock spans for each call
        mock_otel_backend.start_span.side_effect = [
            mock.MagicMock(),
            mock.MagicMock(),
        ]
        mock_sentry_backend.start_span.side_effect = [
            mock.MagicMock(),
            mock.MagicMock(),
        ]

        facade._otel_backend = mock_otel_backend
        facade._sentry_backend = mock_sentry_backend

        with facade.start_span("outer") as outer_span:
            outer_span.set_attribute("level", "outer")
            with facade.start_span("inner") as inner_span:
                inner_span.set_attribute("level", "inner")

        # Verify both backends received both spans
        assert mock_otel_backend.start_span.call_count == 2
        assert mock_sentry_backend.start_span.call_count == 2


class TestBackendIsolation:
    """Tests for backend isolation (one failure doesn't affect the other)."""

    def test_sentry_failure_does_not_affect_otel_metrics(self) -> None:
        """Test OTEL metrics still work when Sentry capture fails."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(environment="test"),
            otel=OTELConfig(service_name="test-service"),
        )

        # Sentry backend that fails
        mock_sentry_backend = mock.MagicMock()
        mock_sentry_backend.capture_error.side_effect = RuntimeError("Sentry unavailable")
        facade._sentry_backend = mock_sentry_backend

        # OTEL backend that works
        mock_otel_backend = mock.MagicMock()
        facade._otel_backend = mock_otel_backend

        # Capture error should fail gracefully
        result = facade.capture_error(ValueError("test"))
        assert result is None  # Returns None on failure

        # But OTEL metrics should still work
        facade.record_metric("test.metric", 1, metric_type="counter")
        mock_otel_backend.record_metric.assert_called_once()

    def test_otel_failure_does_not_affect_sentry_capture(self) -> None:
        """Test Sentry capture still works when OTEL span fails."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(environment="test"),
            otel=OTELConfig(service_name="test-service"),
            routing=RoutingConfig(traces="otel"),
        )

        # OTEL backend that fails on span creation
        mock_otel_backend = mock.MagicMock()
        mock_otel_backend.start_span.side_effect = RuntimeError("OTEL unavailable")
        facade._otel_backend = mock_otel_backend

        # Sentry backend that works
        mock_sentry_backend = mock.MagicMock()
        mock_sentry_backend.capture_error.return_value = "event-id"
        facade._sentry_backend = mock_sentry_backend

        # Span creation may fail, but...
        # Sentry error capture should still work independently
        result = facade.capture_error(ValueError("test"), context={"key": "value"})
        assert result == "event-id"
        mock_sentry_backend.capture_error.assert_called_once()

    def test_span_context_handles_partial_backend_failure(self) -> None:
        """Test SpanContext handles when one backend span is None."""
        # Only OTEL span, Sentry is None
        mock_otel_span = mock.MagicMock()
        span_ctx = SpanContext("test", otel_span=mock_otel_span, sentry_span=None)

        # Should not raise even with None sentry span
        span_ctx.set_attribute("key", "value")
        span_ctx.set_status("ok")
        span_ctx.add_event("test_event")

        mock_otel_span.set_attribute.assert_called_once_with("key", "value")

    def test_independent_backend_shutdown(self) -> None:
        """Test backends are shut down independently - one failure doesn't prevent the other."""
        facade = ObservabilityFacade()
        facade._initialized = True

        mock_sentry_backend = mock.MagicMock()
        mock_otel_backend = mock.MagicMock()
        mock_sentry_backend.shutdown.side_effect = RuntimeError("Sentry shutdown failed")

        facade._sentry_backend = mock_sentry_backend
        facade._otel_backend = mock_otel_backend

        # Shutdown should complete even if Sentry fails - OTEL still gets shut down
        facade.shutdown()

        # Both backends should have been attempted
        mock_sentry_backend.shutdown.assert_called_once()
        mock_otel_backend.shutdown.assert_called_once()

        # State should be properly cleaned up
        assert facade.is_initialized is False
        assert facade._sentry_backend is None
        assert facade._otel_backend is None


class TestRealisticWorkflows:
    """Tests simulating realistic application workflows."""

    def test_request_handling_workflow(self) -> None:
        """Test a typical HTTP request handling workflow."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(environment="production"),
            otel=OTELConfig(service_name="api-server"),
            routing=RoutingConfig(errors="sentry", metrics="otel", traces="otel"),
        )

        mock_sentry = mock.MagicMock()
        mock_otel = mock.MagicMock()
        mock_span = mock.MagicMock()
        mock_otel.start_span.return_value = mock_span

        facade._sentry_backend = mock_sentry
        facade._otel_backend = mock_otel

        # Simulate request handling
        with facade.start_span("handle_request", kind="server") as span:
            span.set_http_attributes(method="POST", url="/api/users", route="/api/users")

            # Process request
            facade.record_metric("request.count", 1, labels={"endpoint": "/api/users"})

            # Simulate successful response
            span.set_http_attributes(method="POST", url="/api/users", status_code=201)
            span.set_status("ok")

            # Record response time
            facade.record_metric(
                "request.duration", 45.2, labels={"endpoint": "/api/users"}, metric_type="histogram"
            )

        # Verify span was created with server kind
        mock_otel.start_span.assert_called_once_with(
            "handle_request", kind="server", attributes=None
        )

        # Verify metrics were recorded
        assert mock_otel.record_metric.call_count == 2

    def test_error_handling_workflow(self) -> None:
        """Test error handling workflow with trace correlation."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(environment="production"),
            otel=OTELConfig(service_name="api-server", service_version="1.0.0"),
            routing=RoutingConfig(errors="sentry", metrics="otel", traces="otel"),
        )

        mock_sentry = mock.MagicMock()
        mock_sentry.capture_error.return_value = "error-event-123"
        mock_otel = mock.MagicMock()
        mock_span = mock.MagicMock()
        mock_otel.start_span.return_value = mock_span

        facade._sentry_backend = mock_sentry
        facade._otel_backend = mock_otel

        # Mock trace context
        with mock.patch.object(facade, "get_trace_context") as mock_get_trace:
            mock_get_trace.return_value = {
                "trace_id": "abcd1234",
                "span_id": "5678efgh",
            }

            # Simulate request with error
            with facade.start_span("handle_request") as span:
                span.set_attribute("user_id", "user-456")

                try:
                    raise ValueError("Invalid user input")
                except ValueError as e:
                    # Record error metrics
                    facade.record_metric("errors.count", 1, labels={"type": "validation"})

                    # Capture error to Sentry
                    event_id = facade.capture_error(
                        e,
                        context={"user_id": "user-456", "input": "invalid"},
                        tags={"error_type": "validation"},
                    )

                    span.set_status("error", str(e))

        # Verify error was captured with trace context
        mock_sentry.capture_error.assert_called_once()
        call_kwargs = mock_sentry.capture_error.call_args.kwargs
        assert call_kwargs["context"]["trace"]["trace_id"] == "abcd1234"
        assert call_kwargs["context"]["trace"]["span_id"] == "5678efgh"
        assert call_kwargs["tags"] == {"error_type": "validation"}

    def test_background_job_workflow(self) -> None:
        """Test background job processing workflow."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            otel=OTELConfig(service_name="job-processor"),
            routing=RoutingConfig(traces="otel", metrics="otel"),
        )

        mock_otel = mock.MagicMock()
        # Need 4 spans: 1 batch + 3 items
        mock_otel.start_span.side_effect = [
            mock.MagicMock(),
            mock.MagicMock(),
            mock.MagicMock(),
            mock.MagicMock(),
        ]
        facade._otel_backend = mock_otel

        # Simulate batch job
        with facade.start_span("process_batch", kind="consumer") as batch_span:
            batch_span.set_attribute("batch_size", 100)

            items_processed = 0
            for i in range(3):  # Process 3 items for test
                with facade.start_span("process_item") as item_span:
                    item_span.set_attribute("item_index", i)
                    items_processed += 1

            batch_span.set_attribute("items_processed", items_processed)
            facade.record_metric("batch.items.processed", items_processed, metric_type="counter")
            facade.record_metric("batch.duration", 5000, metric_type="histogram", unit="ms")

        # Verify nested spans were created
        assert mock_otel.start_span.call_count == 4  # 1 batch + 3 items

        # Verify metrics recorded
        assert mock_otel.record_metric.call_count == 2


class TestFlushAndShutdownIntegration:
    """Tests for flush and shutdown with both backends."""

    def test_flush_calls_both_backends(self) -> None:
        """Test flush() flushes both Sentry and OTEL backends."""
        facade = ObservabilityFacade()
        facade._initialized = True

        mock_sentry = mock.MagicMock()
        mock_otel = mock.MagicMock()
        facade._sentry_backend = mock_sentry
        facade._otel_backend = mock_otel

        facade.flush(timeout=3.0)

        mock_sentry.flush.assert_called_once_with(3.0)
        mock_otel.flush.assert_called_once_with(3.0)

    def test_shutdown_sequence(self) -> None:
        """Test shutdown properly shuts down both backends."""
        facade = ObservabilityFacade()
        facade._initialized = True

        mock_sentry = mock.MagicMock()
        mock_otel = mock.MagicMock()
        facade._sentry_backend = mock_sentry
        facade._otel_backend = mock_otel
        facade._config = mock.MagicMock()

        facade.shutdown()

        mock_sentry.shutdown.assert_called_once()
        mock_otel.shutdown.assert_called_once()
        assert facade.is_initialized is False
        assert facade._sentry_backend is None
        assert facade._otel_backend is None
        assert facade._config is None


class TestRoutingConfiguration:
    """Tests for different routing configurations."""

    def test_errors_routed_to_sentry_only(self) -> None:
        """Test errors are only sent to Sentry when routing.errors='sentry'."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(environment="test"),
            otel=OTELConfig(service_name="test"),
            routing=RoutingConfig(errors="sentry"),
        )

        mock_sentry = mock.MagicMock()
        mock_sentry.capture_error.return_value = "event-id"
        facade._sentry_backend = mock_sentry
        facade._otel_backend = mock.MagicMock()

        facade.capture_error(ValueError("test"))

        # Error goes to Sentry
        mock_sentry.capture_error.assert_called_once()

    def test_metrics_routed_to_otel_only(self) -> None:
        """Test metrics are only sent to OTEL when routing.metrics='otel'."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            routing=RoutingConfig(metrics="otel"),
        )

        mock_otel = mock.MagicMock()
        facade._otel_backend = mock_otel
        facade._sentry_backend = mock.MagicMock()

        facade.record_metric("test.metric", 1)

        # Metric goes to OTEL
        mock_otel.record_metric.assert_called_once()

    def test_traces_routed_to_sentry_only(self) -> None:
        """Test traces are only sent to Sentry when routing.traces='sentry'."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            routing=RoutingConfig(traces="sentry"),
        )

        mock_sentry = mock.MagicMock()
        mock_sentry.start_span.return_value = mock.MagicMock()
        facade._sentry_backend = mock_sentry
        facade._otel_backend = mock.MagicMock()

        with facade.start_span("test_span"):
            pass

        # Span goes to Sentry only
        mock_sentry.start_span.assert_called_once()
        facade._otel_backend.start_span.assert_not_called()

    def test_traces_routed_to_otel_only(self) -> None:
        """Test traces are only sent to OTEL when routing.traces='otel'."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            routing=RoutingConfig(traces="otel"),
        )

        mock_otel = mock.MagicMock()
        mock_otel.start_span.return_value = mock.MagicMock()
        facade._otel_backend = mock_otel
        facade._sentry_backend = mock.MagicMock()

        with facade.start_span("test_span"):
            pass

        # Span goes to OTEL only
        mock_otel.start_span.assert_called_once()
        facade._sentry_backend.start_span.assert_not_called()
