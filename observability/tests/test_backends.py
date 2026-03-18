"""Tests for backend implementations."""

import sys
from typing import Any
from unittest import mock

import pytest

from libs.observability.config import OTELConfig, SentryConfig
from libs.observability.otel_backend import (
    OTELBackend,
    _parse_otlp_headers,
    _validate_metric_name,
    _check_label_cardinality,
)
from libs.observability.sentry_backend import SentryBackend

# Check if sentry_sdk is available
try:
    import sentry_sdk

    HAS_SENTRY_SDK = True
except ImportError:
    HAS_SENTRY_SDK = False

requires_sentry_sdk = pytest.mark.skipif(
    not HAS_SENTRY_SDK, reason="sentry_sdk not installed"
)

# Check if opentelemetry SDK is available
try:
    import opentelemetry.sdk.trace

    HAS_OTEL_SDK = True
except ImportError:
    HAS_OTEL_SDK = False

requires_otel_sdk = pytest.mark.skipif(
    not HAS_OTEL_SDK, reason="opentelemetry-sdk not installed"
)


class TestSentryBackendInit:
    """Tests for Sentry backend initialization."""

    def test_disabled_backend_does_not_init(self) -> None:
        """Test disabled backend skips SDK initialization and returns False."""
        config = SentryConfig(enabled=False, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        result = backend.init()
        assert result is False
        assert backend._initialized is False

    def test_no_dsn_does_not_init(self) -> None:
        """Test backend without DSN skips initialization and returns False."""
        config = SentryConfig(enabled=True, dsn=None)
        backend = SentryBackend(config)
        result = backend.init()
        assert result is False
        assert backend._initialized is False

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.init")
    def test_init_calls_sentry_sdk(self, mock_init: mock.MagicMock) -> None:
        """Test init() calls sentry_sdk.init with correct params."""
        config = SentryConfig(
            enabled=True,
            dsn="https://test@sentry.io/123",
            environment="production",
            release="1.0.0",
            traces_sample_rate=0.5,
            profiles_sample_rate=0.1,
            send_default_pii=True,
        )
        backend = SentryBackend(config)
        result = backend.init()

        mock_init.assert_called_once()
        call_kwargs = mock_init.call_args[1]
        assert call_kwargs["dsn"] == "https://test@sentry.io/123"
        assert call_kwargs["environment"] == "production"
        assert call_kwargs["release"] == "1.0.0"
        assert call_kwargs["traces_sample_rate"] == 0.5
        assert call_kwargs["profiles_sample_rate"] == 0.1
        assert call_kwargs["send_default_pii"] is True
        assert result is True
        assert backend._initialized is True

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.init")
    def test_init_returns_true_on_success(self, mock_init: mock.MagicMock) -> None:
        """Test init() returns True on successful initialization."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        result = backend.init()

        assert result is True
        assert backend._initialized is True
        mock_init.assert_called_once()

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.init")
    def test_init_includes_fastapi_integration(self, mock_init: mock.MagicMock) -> None:
        """Test init() includes FastAPI integration when available."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend.init()

        mock_init.assert_called_once()
        call_kwargs = mock_init.call_args[1]
        integrations = call_kwargs["integrations"]

        # Check that FastApiIntegration is in the list
        integration_types = [type(i).__name__ for i in integrations]
        assert "FastApiIntegration" in integration_types

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.init")
    def test_init_includes_starlette_integration(self, mock_init: mock.MagicMock) -> None:
        """Test init() includes Starlette integration when available."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend.init()

        mock_init.assert_called_once()
        call_kwargs = mock_init.call_args[1]
        integrations = call_kwargs["integrations"]

        # Check that StarletteIntegration is in the list
        integration_types = [type(i).__name__ for i in integrations]
        assert "StarletteIntegration" in integration_types

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.init")
    def test_fastapi_integration_uses_endpoint_transaction_style(
        self, mock_init: mock.MagicMock
    ) -> None:
        """Test FastAPI integration configured with transaction_style='endpoint'."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend.init()

        mock_init.assert_called_once()
        call_kwargs = mock_init.call_args[1]
        integrations = call_kwargs["integrations"]

        # Find FastAPI integration and check transaction_style
        fastapi_integration = next(
            (i for i in integrations if type(i).__name__ == "FastApiIntegration"), None
        )
        assert fastapi_integration is not None
        assert fastapi_integration.transaction_style == "endpoint"

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.init")
    def test_starlette_integration_uses_endpoint_transaction_style(
        self, mock_init: mock.MagicMock
    ) -> None:
        """Test Starlette integration configured with transaction_style='endpoint'."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend.init()

        mock_init.assert_called_once()
        call_kwargs = mock_init.call_args[1]
        integrations = call_kwargs["integrations"]

        # Find Starlette integration and check transaction_style
        starlette_integration = next(
            (i for i in integrations if type(i).__name__ == "StarletteIntegration"), None
        )
        assert starlette_integration is not None
        assert starlette_integration.transaction_style == "endpoint"

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.init")
    def test_init_logs_success(self, mock_init: mock.MagicMock) -> None:
        """Test init() logs INFO message on successful initialization."""
        config = SentryConfig(
            enabled=True,
            dsn="https://test@sentry.io/123",
            environment="production",
            traces_sample_rate=0.5,
        )
        backend = SentryBackend(config)

        with mock.patch("libs.observability.sentry_backend.logger") as mock_logger:
            backend.init()
            mock_logger.info.assert_called_once()
            log_message = mock_logger.info.call_args[0][0]
            assert "production" in log_message
            assert "50%" in log_message

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.init")
    def test_init_returns_false_on_error(self, mock_init: mock.MagicMock) -> None:
        """Test init() returns False and logs error when initialization fails."""
        mock_init.side_effect = RuntimeError("SDK init failed")
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)

        with mock.patch("libs.observability.sentry_backend.logger") as mock_logger:
            result = backend.init()

            assert result is False
            assert backend._initialized is False
            mock_logger.error.assert_called_once()

    def test_init_logs_debug_when_disabled(self) -> None:
        """Test init() logs DEBUG message when disabled."""
        config = SentryConfig(enabled=False, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)

        with mock.patch("libs.observability.sentry_backend.logger") as mock_logger:
            backend.init()
            mock_logger.debug.assert_called_once()


class TestSentryBackendCapture:
    """Tests for Sentry backend capture methods."""

    def test_capture_error_when_not_initialized(self) -> None:
        """Test capture_error returns None when not initialized."""
        config = SentryConfig(enabled=False)
        backend = SentryBackend(config)
        result = backend.capture_error(ValueError("test"))
        assert result is None

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_error_calls_sdk(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_error calls SDK with correct parameters."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)
        mock_scope.capture_exception.return_value = "event-id"

        exc = ValueError("test")
        result = backend.capture_error(
            exc,
            context={"key": "value"},
            level="warning",
            user={"id": "123"},
            tags={"tag": "val"},
            fingerprint=["custom"],
        )

        assert result == "event-id"
        mock_scope.set_context.assert_called_once_with("additional", {"key": "value"})
        mock_scope.set_user.assert_called_once_with({"id": "123"})
        mock_scope.set_tag.assert_called_once_with("tag", "val")
        assert mock_scope.fingerprint == ["custom"]
        assert mock_scope.level == "warning"

    def test_capture_message_when_not_initialized(self) -> None:
        """Test capture_message returns None when not initialized."""
        config = SentryConfig(enabled=False)
        backend = SentryBackend(config)
        result = backend.capture_message("test")
        assert result is None


class TestSentryBackendContext:
    """Tests for Sentry backend context methods."""

    def test_set_user_when_not_initialized(self) -> None:
        """Test set_user does nothing when not initialized."""
        config = SentryConfig(enabled=False)
        backend = SentryBackend(config)
        # Should not raise
        backend.set_user("user-123")

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.set_user")
    def test_set_user_calls_sdk(self, mock_set_user: mock.MagicMock) -> None:
        """Test set_user calls SDK."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        backend.set_user("user-123", email="test@example.com")
        mock_set_user.assert_called_once_with({"id": "user-123", "email": "test@example.com"})

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.set_user")
    def test_set_user_none_clears_user(self, mock_set_user: mock.MagicMock) -> None:
        """Test set_user(None) clears user context."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        backend.set_user(None)
        mock_set_user.assert_called_once_with(None)

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.set_tag")
    def test_set_tag_calls_sdk(self, mock_set_tag: mock.MagicMock) -> None:
        """Test set_tag calls SDK."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        backend.set_tag("key", "value")
        mock_set_tag.assert_called_once_with("key", "value")

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.set_context")
    def test_set_context_calls_sdk(self, mock_set_context: mock.MagicMock) -> None:
        """Test set_context calls SDK."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        backend.set_context("request", {"url": "/test"})
        mock_set_context.assert_called_once_with("request", {"url": "/test"})


class TestOTELBackendInit:
    """Tests for OTEL backend initialization."""

    def test_disabled_backend_does_not_init(self) -> None:
        """Test disabled backend skips initialization."""
        config = OTELConfig(enabled=False)
        backend = OTELBackend(config)
        backend.init()
        assert backend._initialized is False

    def test_disabled_metrics_skips_meter(self) -> None:
        """Test disabled metrics skips meter provider setup."""
        config = OTELConfig(enabled=True, metrics_enabled=False, traces_enabled=False)
        backend = OTELBackend(config)
        backend.init()
        assert backend._meter_provider is None

    def test_disabled_traces_skips_tracer(self) -> None:
        """Test disabled traces skips tracer provider setup."""
        config = OTELConfig(enabled=True, metrics_enabled=False, traces_enabled=False)
        backend = OTELBackend(config)
        backend.init()
        assert backend._tracer_provider is None


class TestOTELBackendMetrics:
    """Tests for OTEL backend metrics recording."""

    def test_record_metric_when_not_initialized(self) -> None:
        """Test record_metric does nothing when not initialized."""
        config = OTELConfig(enabled=False)
        backend = OTELBackend(config)
        # Should not raise
        backend.record_metric("test.metric", 1)

    def test_record_metric_counter_creates_instrument(self) -> None:
        """Test record_metric creates counter instrument."""
        config = OTELConfig(enabled=True, metrics_enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True
        backend._meter = mock.MagicMock()
        mock_counter = mock.MagicMock()
        backend._meter.create_counter.return_value = mock_counter

        backend.record_metric("test.counter", 5, metric_type="counter")

        backend._meter.create_counter.assert_called_once_with(
            name="test.counter",
            unit="1",
            description="Counter for test.counter",
        )
        mock_counter.add.assert_called_once_with(5, attributes={})

    def test_record_metric_reuses_counter(self) -> None:
        """Test record_metric reuses existing counter."""
        config = OTELConfig(enabled=True, metrics_enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True
        backend._meter = mock.MagicMock()
        mock_counter = mock.MagicMock()
        backend._counters["test.counter"] = mock_counter

        backend.record_metric("test.counter", 5, metric_type="counter")

        # Should not create new counter
        backend._meter.create_counter.assert_not_called()
        mock_counter.add.assert_called_once_with(5, attributes={})

    def test_record_metric_histogram(self) -> None:
        """Test record_metric creates histogram instrument."""
        config = OTELConfig(enabled=True, metrics_enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True
        backend._meter = mock.MagicMock()
        mock_histogram = mock.MagicMock()
        backend._meter.create_histogram.return_value = mock_histogram

        backend.record_metric("test.duration", 0.5, metric_type="histogram", unit="s")

        backend._meter.create_histogram.assert_called_once_with(
            name="test.duration",
            unit="s",
            description="Histogram for test.duration",
        )
        mock_histogram.record.assert_called_once_with(0.5, attributes={})

    def test_record_metric_with_labels(self) -> None:
        """Test record_metric passes labels as attributes."""
        config = OTELConfig(enabled=True, metrics_enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True
        backend._meter = mock.MagicMock()
        mock_counter = mock.MagicMock()
        backend._meter.create_counter.return_value = mock_counter

        backend.record_metric(
            "test.counter",
            1,
            labels={"service": "api", "endpoint": "/test"},
            metric_type="counter",
        )

        mock_counter.add.assert_called_once_with(
            1, attributes={"service": "api", "endpoint": "/test"}
        )


class TestOTELBackendTracing:
    """Tests for OTEL backend tracing."""

    def test_start_span_when_not_initialized(self) -> None:
        """Test start_span yields None when not initialized."""
        config = OTELConfig(enabled=False)
        backend = OTELBackend(config)

        with backend.start_span("test") as span:
            assert span is None

    def test_start_span_when_no_tracer(self) -> None:
        """Test start_span yields None when tracer is None."""
        config = OTELConfig(enabled=True, traces_enabled=False)
        backend = OTELBackend(config)
        backend._initialized = True
        backend._tracer = None

        with backend.start_span("test") as span:
            assert span is None

    def test_start_span_with_tracer(self) -> None:
        """Test start_span yields span object when tracer is configured."""
        config = OTELConfig(enabled=True, traces_enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True

        mock_span = mock.MagicMock()
        mock_tracer = mock.MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = mock.MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = mock.MagicMock(
            return_value=False
        )
        backend._tracer = mock_tracer

        with backend.start_span("test_operation") as span:
            assert span is mock_span

    def test_start_span_with_attributes(self) -> None:
        """Test start_span passes attributes to tracer."""
        config = OTELConfig(enabled=True, traces_enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True

        mock_span = mock.MagicMock()
        mock_tracer = mock.MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = mock.MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = mock.MagicMock(
            return_value=False
        )
        backend._tracer = mock_tracer

        with backend.start_span("test_operation", attributes={"key": "value", "count": 42}):
            pass

        mock_tracer.start_as_current_span.assert_called_once()
        call_kwargs = mock_tracer.start_as_current_span.call_args[1]
        assert call_kwargs["attributes"] == {"key": "value", "count": 42}

    def test_start_span_internal_kind(self) -> None:
        """Test start_span with internal kind."""
        config = OTELConfig(enabled=True, traces_enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True

        mock_span = mock.MagicMock()
        mock_tracer = mock.MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = mock.MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = mock.MagicMock(
            return_value=False
        )
        backend._tracer = mock_tracer

        with backend.start_span("test_operation", kind="internal"):
            pass

        mock_tracer.start_as_current_span.assert_called_once()
        call_args = mock_tracer.start_as_current_span.call_args
        # kind should be mapped to SpanKind.INTERNAL
        assert "kind" in call_args[1]

    def test_start_span_server_kind(self) -> None:
        """Test start_span with server kind."""
        config = OTELConfig(enabled=True, traces_enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True

        mock_span = mock.MagicMock()
        mock_tracer = mock.MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = mock.MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = mock.MagicMock(
            return_value=False
        )
        backend._tracer = mock_tracer

        with backend.start_span("handle_request", kind="server"):
            pass

        mock_tracer.start_as_current_span.assert_called_once()

    def test_start_span_client_kind(self) -> None:
        """Test start_span with client kind."""
        config = OTELConfig(enabled=True, traces_enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True

        mock_span = mock.MagicMock()
        mock_tracer = mock.MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = mock.MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = mock.MagicMock(
            return_value=False
        )
        backend._tracer = mock_tracer

        with backend.start_span("api_call", kind="client"):
            pass

        mock_tracer.start_as_current_span.assert_called_once()

    def test_start_span_producer_kind(self) -> None:
        """Test start_span with producer kind."""
        config = OTELConfig(enabled=True, traces_enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True

        mock_span = mock.MagicMock()
        mock_tracer = mock.MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = mock.MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = mock.MagicMock(
            return_value=False
        )
        backend._tracer = mock_tracer

        with backend.start_span("publish_message", kind="producer"):
            pass

        mock_tracer.start_as_current_span.assert_called_once()

    def test_start_span_consumer_kind(self) -> None:
        """Test start_span with consumer kind."""
        config = OTELConfig(enabled=True, traces_enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True

        mock_span = mock.MagicMock()
        mock_tracer = mock.MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = mock.MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = mock.MagicMock(
            return_value=False
        )
        backend._tracer = mock_tracer

        with backend.start_span("process_message", kind="consumer"):
            pass

        mock_tracer.start_as_current_span.assert_called_once()

    @requires_otel_sdk
    def test_start_span_with_real_tracer(self) -> None:
        """Test start_span with a real OTEL tracer (console exporter)."""
        config = OTELConfig(
            enabled=True,
            exporter="console",
            traces_enabled=True,
            metrics_enabled=False,
            logs_enabled=False,
        )
        backend = OTELBackend(config)
        result = backend.init()

        assert result is True
        assert backend._tracer is not None

        # Create a span and verify it works
        with backend.start_span("test_operation", attributes={"test_key": "test_value"}) as span:
            assert span is not None
            span.set_attribute("another_key", "another_value")
            span.add_event("test_event", {"event_attr": "event_value"})

        backend.shutdown()

    @requires_otel_sdk
    def test_nested_spans_with_real_tracer(self) -> None:
        """Test nested spans with a real OTEL tracer."""
        config = OTELConfig(
            enabled=True,
            exporter="console",
            traces_enabled=True,
            metrics_enabled=False,
            logs_enabled=False,
        )
        backend = OTELBackend(config)
        result = backend.init()

        assert result is True

        # Create nested spans
        with backend.start_span("outer_span") as outer:
            assert outer is not None
            outer.set_attribute("level", "outer")

            with backend.start_span("inner_span") as inner:
                assert inner is not None
                inner.set_attribute("level", "inner")

        backend.shutdown()


class TestOTELBackendLifecycle:
    """Tests for OTEL backend lifecycle methods."""

    def test_flush_when_not_initialized(self) -> None:
        """Test flush does nothing when not initialized."""
        config = OTELConfig(enabled=False)
        backend = OTELBackend(config)
        # Should not raise
        backend.flush()

    def test_flush_calls_providers(self) -> None:
        """Test flush calls force_flush on providers."""
        config = OTELConfig(enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True
        backend._meter_provider = mock.MagicMock()
        backend._tracer_provider = mock.MagicMock()

        backend.flush(timeout=5.0)

        backend._meter_provider.force_flush.assert_called_once_with(timeout_millis=5000)
        backend._tracer_provider.force_flush.assert_called_once_with(timeout_millis=5000)

    def test_shutdown_when_not_initialized(self) -> None:
        """Test shutdown does nothing when not initialized."""
        config = OTELConfig(enabled=False)
        backend = OTELBackend(config)
        # Should not raise
        backend.shutdown()

    def test_shutdown_calls_providers(self) -> None:
        """Test shutdown calls shutdown on providers."""
        config = OTELConfig(enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True
        mock_meter_provider = mock.MagicMock()
        mock_tracer_provider = mock.MagicMock()
        backend._meter_provider = mock_meter_provider
        backend._tracer_provider = mock_tracer_provider

        backend.shutdown()

        mock_meter_provider.shutdown.assert_called_once()
        mock_tracer_provider.shutdown.assert_called_once()
        assert backend._initialized is False
        assert backend._meter_provider is None
        assert backend._tracer_provider is None

    def test_flush_logs_warning_on_error(self) -> None:
        """Test flush logs warning when provider flush fails."""
        config = OTELConfig(enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True
        backend._meter_provider = mock.MagicMock()
        backend._meter_provider.force_flush.side_effect = RuntimeError("flush failed")

        # Should not raise, should log warning
        with mock.patch("libs.observability.otel_backend.logger") as mock_logger:
            backend.flush()
            mock_logger.warning.assert_called()

    def test_shutdown_logs_warning_on_error(self) -> None:
        """Test shutdown logs warning when provider shutdown fails."""
        config = OTELConfig(enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True
        backend._meter_provider = mock.MagicMock()
        backend._meter_provider.shutdown.side_effect = RuntimeError("shutdown failed")

        # Should not raise, should log warning
        with mock.patch("libs.observability.otel_backend.logger") as mock_logger:
            backend.shutdown()
            mock_logger.warning.assert_called()


class TestContextSerialization:
    """Tests for context serialization in Sentry backend."""

    def test_serialize_value_primitives(self) -> None:
        """Test serialization of primitive types."""
        from libs.observability.sentry_backend import _serialize_value

        assert _serialize_value(None) is None
        assert _serialize_value(True) is True
        assert _serialize_value(42) == 42
        assert _serialize_value(3.14) == 3.14
        assert _serialize_value("hello") == "hello"

    def test_serialize_value_datetime(self) -> None:
        """Test serialization of datetime objects."""
        from datetime import date, datetime

        from libs.observability.sentry_backend import _serialize_value

        dt = datetime(2024, 1, 15, 10, 30, 0)
        assert _serialize_value(dt) == "2024-01-15T10:30:00"

        d = date(2024, 1, 15)
        assert _serialize_value(d) == "2024-01-15"

    def test_serialize_value_uuid(self) -> None:
        """Test serialization of UUID objects."""
        from uuid import UUID

        from libs.observability.sentry_backend import _serialize_value

        uuid = UUID("12345678-1234-5678-1234-567812345678")
        assert _serialize_value(uuid) == "12345678-1234-5678-1234-567812345678"

    def test_serialize_value_bytes(self) -> None:
        """Test serialization of bytes."""
        from libs.observability.sentry_backend import _serialize_value

        # UTF-8 decodable bytes
        assert _serialize_value(b"hello") == "hello"

        # Non-UTF-8 bytes
        result = _serialize_value(b"\xff\xfe")
        assert "<bytes:" in result

    def test_serialize_value_nested_dict(self) -> None:
        """Test serialization of nested dictionaries."""
        from datetime import datetime
        from uuid import UUID

        from libs.observability.sentry_backend import _serialize_value

        nested = {
            "user_id": UUID("12345678-1234-5678-1234-567812345678"),
            "timestamp": datetime(2024, 1, 15, 10, 30, 0),
            "metadata": {
                "nested_uuid": UUID("87654321-4321-8765-4321-876543218765"),
            },
        }
        result = _serialize_value(nested)

        assert result["user_id"] == "12345678-1234-5678-1234-567812345678"
        assert result["timestamp"] == "2024-01-15T10:30:00"
        assert result["metadata"]["nested_uuid"] == "87654321-4321-8765-4321-876543218765"

    def test_serialize_value_list(self) -> None:
        """Test serialization of lists."""
        from uuid import UUID

        from libs.observability.sentry_backend import _serialize_value

        items = [UUID("12345678-1234-5678-1234-567812345678"), "hello", 42]
        result = _serialize_value(items)

        assert result == ["12345678-1234-5678-1234-567812345678", "hello", 42]

    def test_serialize_value_set(self) -> None:
        """Test serialization of sets (converted to sorted list)."""
        from libs.observability.sentry_backend import _serialize_value

        items = {3, 1, 2}
        result = _serialize_value(items)

        assert result == [1, 2, 3]

    def test_serialize_value_custom_object(self) -> None:
        """Test serialization of custom objects with __dict__."""
        from libs.observability.sentry_backend import _serialize_value

        class CustomObj:
            def __init__(self) -> None:
                self.name = "test"
                self.value = 42
                self._private = "hidden"

        obj = CustomObj()
        result = _serialize_value(obj)

        assert result == {"name": "test", "value": 42}
        assert "_private" not in result

    def test_serialize_context(self) -> None:
        """Test full context serialization."""
        from datetime import datetime
        from uuid import UUID

        from libs.observability.sentry_backend import _serialize_context

        context = {
            "request_id": UUID("12345678-1234-5678-1234-567812345678"),
            "timestamp": datetime(2024, 1, 15, 10, 30, 0),
            "user_id": "user-123",
            "count": 5,
        }
        result = _serialize_context(context)

        assert result == {
            "request_id": "12345678-1234-5678-1234-567812345678",
            "timestamp": "2024-01-15T10:30:00",
            "user_id": "user-123",
            "count": 5,
        }

    def test_serialize_value_circular_reference_dict(self) -> None:
        """Test serialization handles circular reference in dict."""
        from libs.observability.sentry_backend import _serialize_value

        d: dict[str, Any] = {"name": "test"}
        d["self"] = d  # Create circular reference

        result = _serialize_value(d)

        assert result["name"] == "test"
        assert result["self"] == "<circular reference: dict>"

    def test_serialize_value_circular_reference_list(self) -> None:
        """Test serialization handles circular reference in list."""
        from libs.observability.sentry_backend import _serialize_value

        lst: list[Any] = [1, 2, 3]
        lst.append(lst)  # Create circular reference

        result = _serialize_value(lst)

        assert result[0] == 1
        assert result[1] == 2
        assert result[2] == 3
        assert result[3] == "<circular reference: list>"

    def test_serialize_value_indirect_circular_reference(self) -> None:
        """Test serialization handles indirect circular references."""
        from libs.observability.sentry_backend import _serialize_value

        a: dict[str, Any] = {"name": "a"}
        b: dict[str, Any] = {"name": "b"}
        a["child"] = b
        b["parent"] = a  # Creates indirect circular reference

        result = _serialize_value(a)

        assert result["name"] == "a"
        assert result["child"]["name"] == "b"
        assert result["child"]["parent"] == "<circular reference: dict>"

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_error_serializes_context(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_error serializes non-JSON types in context."""
        from datetime import datetime
        from uuid import UUID

        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)
        mock_scope.capture_exception.return_value = "event-id"

        exc = ValueError("test")
        context = {
            "request_id": UUID("12345678-1234-5678-1234-567812345678"),
            "timestamp": datetime(2024, 1, 15, 10, 30, 0),
        }
        backend.capture_error(exc, context=context)

        # Verify context was serialized
        mock_scope.set_context.assert_called_once()
        call_args = mock_scope.set_context.call_args
        assert call_args[0][0] == "additional"
        serialized_context = call_args[0][1]
        assert serialized_context["request_id"] == "12345678-1234-5678-1234-567812345678"
        assert serialized_context["timestamp"] == "2024-01-15T10:30:00"

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.set_context")
    def test_set_context_serializes_values(self, mock_set_context: mock.MagicMock) -> None:
        """Test set_context serializes non-JSON types."""
        from datetime import datetime

        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        backend.set_context("request", {"timestamp": datetime(2024, 1, 15, 10, 30, 0)})

        mock_set_context.assert_called_once_with("request", {"timestamp": "2024-01-15T10:30:00"})


class TestCaptureErrorLevels:
    """Tests for error level handling in capture_error."""

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_error_with_fatal_level(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_error with fatal level."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)

        backend.capture_error(ValueError("test"), level="fatal")
        assert mock_scope.level == "fatal"

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_error_with_info_level(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_error with info level."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)

        backend.capture_error(ValueError("test"), level="info")
        assert mock_scope.level == "info"

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_error_default_level_is_error(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_error uses 'error' level by default."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)

        backend.capture_error(ValueError("test"))
        assert mock_scope.level == "error"


class TestCaptureErrorFingerprinting:
    """Tests for custom fingerprinting in capture_error."""

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_error_with_custom_fingerprint(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_error with custom fingerprint for grouping."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock()
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)

        backend.capture_error(
            ValueError("test"),
            fingerprint=["{{ default }}", "payment-error", "user-123"],
        )
        assert mock_scope.fingerprint == ["{{ default }}", "payment-error", "user-123"]

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.new_scope")
    def test_capture_error_without_fingerprint_uses_default(
        self, mock_new_scope: mock.MagicMock
    ) -> None:
        """Test capture_error without fingerprint lets Sentry use default grouping."""
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        backend._initialized = True

        mock_scope = mock.MagicMock(spec=["set_context", "set_user", "set_tag", "level", "capture_exception"])
        mock_new_scope.return_value.__enter__ = mock.MagicMock(return_value=mock_scope)
        mock_new_scope.return_value.__exit__ = mock.MagicMock(return_value=False)

        backend.capture_error(ValueError("test"))
        # Verify fingerprint attribute was never set on scope (uses Sentry's default grouping)
        assert not hasattr(mock_scope, "fingerprint")


class TestSentryOTELTraceIntegration:
    """Tests for Sentry-OTEL trace correlation."""

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.init")
    def test_init_gracefully_handles_otel_not_configured(
        self, mock_init: mock.MagicMock
    ) -> None:
        """Test init() gracefully handles when OTEL is not configured (DidNotEnable)."""
        # Without mocking, OpenTelemetryIntegration() raises DidNotEnable
        # because OTEL tracer provider isn't configured. This should be handled gracefully.
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)
        result = backend.init()

        # Should still initialize successfully
        assert result is True
        assert backend._initialized is True
        mock_init.assert_called_once()

        # Verify the standard integrations are still present
        call_kwargs = mock_init.call_args[1]
        integrations = call_kwargs["integrations"]
        integration_types = [type(i).__name__ for i in integrations]
        assert "LoggingIntegration" in integration_types
        assert "FastApiIntegration" in integration_types
        assert "StarletteIntegration" in integration_types

    @requires_sentry_sdk
    @mock.patch("sentry_sdk.init")
    def test_init_attempts_to_add_opentelemetry_integration(
        self, mock_init: mock.MagicMock
    ) -> None:
        """Test init() attempts to add OpenTelemetry integration.

        This test verifies that the code path exists to add OpenTelemetryIntegration.
        The integration may or may not be added depending on whether OTEL SDK is available.
        """
        config = SentryConfig(enabled=True, dsn="https://test@sentry.io/123")
        backend = SentryBackend(config)

        # The init should succeed regardless of OTEL availability
        result = backend.init()
        assert result is True

        # Verify init was called with some integrations
        mock_init.assert_called_once()
        call_kwargs = mock_init.call_args[1]
        integrations = call_kwargs["integrations"]

        # At minimum, these integrations should always be present
        integration_types = [type(i).__name__ for i in integrations]
        assert "LoggingIntegration" in integration_types

        # OpenTelemetryIntegration may or may not be present depending on environment
        # This is expected behavior - we just verify the code doesn't crash


class TestParseOTLPHeaders:
    """Tests for OTLP headers parsing."""

    def test_parse_empty_string(self) -> None:
        """Test parsing empty string returns empty dict."""
        assert _parse_otlp_headers("") == {}

    def test_parse_single_header(self) -> None:
        """Test parsing single key=value pair."""
        assert _parse_otlp_headers("Authorization=Basic xxx") == {"Authorization": "Basic xxx"}

    def test_parse_multiple_headers(self) -> None:
        """Test parsing multiple key=value pairs."""
        result = _parse_otlp_headers("key1=value1,key2=value2")
        assert result == {"key1": "value1", "key2": "value2"}

    def test_parse_strips_whitespace(self) -> None:
        """Test parsing strips whitespace around keys and values."""
        result = _parse_otlp_headers(" key1 = value1 , key2 = value2 ")
        assert result == {"key1": "value1", "key2": "value2"}

    def test_parse_skips_empty_keys(self) -> None:
        """Test parsing skips entries with empty keys."""
        result = _parse_otlp_headers("=value,key=value")
        assert result == {"key": "value"}

    def test_parse_skips_empty_values(self) -> None:
        """Test parsing skips entries with empty values."""
        result = _parse_otlp_headers("key=,key2=value2")
        assert result == {"key2": "value2"}

    def test_parse_handles_value_with_equals(self) -> None:
        """Test parsing handles values containing equals signs."""
        result = _parse_otlp_headers("Authorization=Basic dXNlcjpwYXNz=")
        assert result == {"Authorization": "Basic dXNlcjpwYXNz="}

    def test_parse_skips_entries_without_equals(self) -> None:
        """Test parsing skips entries without equals sign."""
        result = _parse_otlp_headers("invalid,key=value")
        assert result == {"key": "value"}

    def test_parse_rejects_headers_with_newlines(self) -> None:
        """Test parsing rejects headers containing newline characters."""
        result = _parse_otlp_headers("key=value\ninjected,key2=value2")
        assert result == {"key2": "value2"}

    def test_parse_rejects_headers_with_carriage_return(self) -> None:
        """Test parsing rejects headers containing carriage return characters."""
        result = _parse_otlp_headers("key=value\rinjected,key2=value2")
        assert result == {"key2": "value2"}

    def test_parse_rejects_key_with_control_chars(self) -> None:
        """Test parsing rejects keys containing control characters."""
        result = _parse_otlp_headers("bad\x00key=value,good=value")
        assert result == {"good": "value"}


class TestOTELBackendInitReturnValue:
    """Tests for OTEL backend init() return value."""

    def test_init_returns_false_when_disabled(self) -> None:
        """Test init() returns False when OTEL is disabled."""
        config = OTELConfig(enabled=False)
        backend = OTELBackend(config)
        result = backend.init()
        assert result is False
        assert backend._initialized is False

    def test_init_returns_false_when_exporter_none(self) -> None:
        """Test init() returns False when exporter='none'."""
        config = OTELConfig(enabled=True, exporter="none")
        backend = OTELBackend(config)
        result = backend.init()
        assert result is False
        assert backend._initialized is False

    def test_init_returns_true_when_already_initialized(self) -> None:
        """Test init() returns True and logs warning when already initialized."""
        config = OTELConfig(enabled=True, traces_enabled=False, metrics_enabled=False)
        backend = OTELBackend(config)
        backend._initialized = True

        with mock.patch("libs.observability.otel_backend.logger") as mock_logger:
            result = backend.init()
            assert result is True
            mock_logger.warning.assert_called_once()
            assert "already initialized" in mock_logger.warning.call_args[0][0]


class TestOTELBackendConsoleExporter:
    """Tests for OTEL backend with console exporter."""

    @requires_otel_sdk
    def test_init_with_console_exporter_succeeds(self) -> None:
        """Test init() with console exporter succeeds and enables tracing."""
        config = OTELConfig(
            enabled=True,
            exporter="console",
            traces_enabled=True,
            metrics_enabled=False,
            logs_enabled=False,
        )
        backend = OTELBackend(config)
        result = backend.init()

        assert result is True
        assert backend._initialized is True
        assert backend._tracer is not None
        assert backend._tracer_provider is not None

        # Cleanup
        backend.shutdown()


class TestOTELBackendNewConfigFields:
    """Tests for new OTEL config fields."""

    def test_config_with_service_version(self) -> None:
        """Test OTELConfig with service_version field."""
        config = OTELConfig(
            enabled=True,
            service_name="my-service",
            service_version="2.0.0",
        )
        assert config.service_version == "2.0.0"

    def test_config_with_traces_sample_rate(self) -> None:
        """Test OTELConfig with traces_sample_rate field."""
        config = OTELConfig(
            enabled=True,
            traces_sample_rate=0.25,
        )
        assert config.traces_sample_rate == 0.25

    def test_config_with_metrics_export_interval(self) -> None:
        """Test OTELConfig with metrics_export_interval_millis field."""
        config = OTELConfig(
            enabled=True,
            metrics_export_interval_millis=30000,
        )
        assert config.metrics_export_interval_millis == 30000

    def test_config_with_logs_enabled(self) -> None:
        """Test OTELConfig with logs_enabled field."""
        config = OTELConfig(
            enabled=True,
            logs_enabled=True,
        )
        assert config.logs_enabled is True

    def test_config_with_otlp_headers(self) -> None:
        """Test OTELConfig with otlp_headers field."""
        config = OTELConfig(
            enabled=True,
            otlp_headers="Authorization=Basic xxx",
        )
        assert config.otlp_headers == "Authorization=Basic xxx"


class TestOTELBackendLoggerProvider:
    """Tests for OTEL backend logger provider handling."""

    def test_flush_includes_logger_provider(self) -> None:
        """Test flush() calls force_flush on logger provider."""
        config = OTELConfig(enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True
        backend._meter_provider = mock.MagicMock()
        backend._tracer_provider = mock.MagicMock()
        backend._logger_provider = mock.MagicMock()

        backend.flush(timeout=5.0)

        backend._meter_provider.force_flush.assert_called_once_with(timeout_millis=5000)
        backend._tracer_provider.force_flush.assert_called_once_with(timeout_millis=5000)
        backend._logger_provider.force_flush.assert_called_once_with(timeout_millis=5000)

    def test_shutdown_includes_logger_provider(self) -> None:
        """Test shutdown() calls shutdown on logger provider."""
        config = OTELConfig(enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True
        mock_meter_provider = mock.MagicMock()
        mock_tracer_provider = mock.MagicMock()
        mock_logger_provider = mock.MagicMock()
        backend._meter_provider = mock_meter_provider
        backend._tracer_provider = mock_tracer_provider
        backend._logger_provider = mock_logger_provider

        backend.shutdown()

        mock_meter_provider.shutdown.assert_called_once()
        mock_tracer_provider.shutdown.assert_called_once()
        mock_logger_provider.shutdown.assert_called_once()
        assert backend._initialized is False
        assert backend._logger_provider is None


class TestOTELBackendSampleRate:
    """Tests for OTEL backend trace sampling."""

    @requires_otel_sdk
    def test_init_with_sample_rate_succeeds(self) -> None:
        """Test init() succeeds with configured sample rate."""
        config = OTELConfig(
            enabled=True,
            exporter="console",
            traces_enabled=True,
            metrics_enabled=False,
            logs_enabled=False,
            traces_sample_rate=0.5,
        )
        backend = OTELBackend(config)
        result = backend.init()

        assert result is True
        assert backend._initialized is True
        assert backend._tracer_provider is not None

        # Cleanup
        backend.shutdown()


class TestMetricNameValidation:
    """Tests for metric name validation."""

    def test_valid_simple_name(self) -> None:
        """Test validation of simple valid metric name."""
        is_valid, error = _validate_metric_name("requests")
        assert is_valid is True
        assert error is None

    def test_valid_name_with_dots(self) -> None:
        """Test validation of metric name with dots for hierarchy."""
        is_valid, error = _validate_metric_name("http.server.requests.total")
        assert is_valid is True
        assert error is None

    def test_valid_name_with_underscores(self) -> None:
        """Test validation of metric name with underscores."""
        is_valid, error = _validate_metric_name("request_duration_seconds")
        assert is_valid is True
        assert error is None

    def test_valid_name_with_numbers(self) -> None:
        """Test validation of metric name with numbers."""
        is_valid, error = _validate_metric_name("api_v2_requests")
        assert is_valid is True
        assert error is None

    def test_invalid_empty_name(self) -> None:
        """Test validation rejects empty name."""
        is_valid, error = _validate_metric_name("")
        assert is_valid is False
        assert error is not None
        assert "empty" in error.lower()

    def test_invalid_name_with_spaces(self) -> None:
        """Test validation rejects name with spaces."""
        is_valid, error = _validate_metric_name("my metric")
        assert is_valid is False
        assert error is not None
        assert "spaces" in error.lower()

    def test_invalid_name_starting_with_number(self) -> None:
        """Test validation rejects name starting with number."""
        is_valid, error = _validate_metric_name("123requests")
        assert is_valid is False
        assert error is not None
        assert "conventions" in error.lower()

    def test_invalid_name_with_uppercase(self) -> None:
        """Test validation rejects name with uppercase letters."""
        is_valid, error = _validate_metric_name("HTTP_Requests")
        assert is_valid is False
        assert error is not None
        assert "conventions" in error.lower()

    def test_invalid_name_with_special_chars(self) -> None:
        """Test validation rejects name with special characters."""
        is_valid, error = _validate_metric_name("requests@total")
        assert is_valid is False
        assert error is not None
        assert "conventions" in error.lower()


class TestLabelCardinalityValidation:
    """Tests for label cardinality checking."""

    def test_no_warning_for_normal_labels(self) -> None:
        """Test no warning for normal low-cardinality labels."""
        with mock.patch("libs.observability.otel_backend.logger") as mock_logger:
            _check_label_cardinality({"endpoint": "/api", "method": "GET"}, "requests")
            mock_logger.warning.assert_not_called()

    def test_warns_on_user_id(self) -> None:
        """Test warning on user_id label."""
        with mock.patch("libs.observability.otel_backend.logger") as mock_logger:
            _check_label_cardinality({"user_id": "12345"}, "requests")
            mock_logger.warning.assert_called_once()
            assert "user_id" in mock_logger.warning.call_args[0][0]

    def test_warns_on_userid_variant(self) -> None:
        """Test warning on userid label (no underscore)."""
        with mock.patch("libs.observability.otel_backend.logger") as mock_logger:
            _check_label_cardinality({"userid": "12345"}, "requests")
            mock_logger.warning.assert_called_once()

    def test_warns_on_request_id(self) -> None:
        """Test warning on request_id label."""
        with mock.patch("libs.observability.otel_backend.logger") as mock_logger:
            _check_label_cardinality({"request_id": "abc-123"}, "requests")
            mock_logger.warning.assert_called_once()
            assert "request_id" in mock_logger.warning.call_args[0][0]

    def test_warns_on_session_id(self) -> None:
        """Test warning on session_id label."""
        with mock.patch("libs.observability.otel_backend.logger") as mock_logger:
            _check_label_cardinality({"session_id": "sess-xyz"}, "requests")
            mock_logger.warning.assert_called_once()

    def test_warns_on_timestamp(self) -> None:
        """Test warning on timestamp label."""
        with mock.patch("libs.observability.otel_backend.logger") as mock_logger:
            _check_label_cardinality({"timestamp": "2024-01-15T10:30:00"}, "requests")
            mock_logger.warning.assert_called_once()

    def test_warns_on_email(self) -> None:
        """Test warning on email label."""
        with mock.patch("libs.observability.otel_backend.logger") as mock_logger:
            _check_label_cardinality({"email": "user@example.com"}, "requests")
            mock_logger.warning.assert_called_once()

    def test_warns_on_ip_address(self) -> None:
        """Test warning on ip_address label."""
        with mock.patch("libs.observability.otel_backend.logger") as mock_logger:
            _check_label_cardinality({"ip_address": "192.168.1.1"}, "requests")
            mock_logger.warning.assert_called_once()

    def test_none_labels_no_error(self) -> None:
        """Test no error when labels is None."""
        with mock.patch("libs.observability.otel_backend.logger") as mock_logger:
            _check_label_cardinality(None, "requests")
            mock_logger.warning.assert_not_called()


class TestUpDownCounterMetric:
    """Tests for UpDownCounter metric type."""

    def test_record_updown_counter_creates_instrument(self) -> None:
        """Test record_metric creates UpDownCounter instrument."""
        config = OTELConfig(enabled=True, metrics_enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True
        backend._meter = mock.MagicMock()
        mock_updown = mock.MagicMock()
        backend._meter.create_up_down_counter.return_value = mock_updown

        backend.record_metric("queue.size", 10, metric_type="updown_counter")

        backend._meter.create_up_down_counter.assert_called_once_with(
            name="queue.size",
            unit="1",
            description="UpDownCounter for queue.size",
        )
        mock_updown.add.assert_called_once_with(10, attributes={})

    def test_record_updown_counter_reuses_instrument(self) -> None:
        """Test record_metric reuses existing UpDownCounter."""
        config = OTELConfig(enabled=True, metrics_enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True
        backend._meter = mock.MagicMock()
        mock_updown = mock.MagicMock()
        backend._updown_counters["queue.size"] = mock_updown

        backend.record_metric("queue.size", -5, metric_type="updown_counter")

        # Should not create new instrument
        backend._meter.create_up_down_counter.assert_not_called()
        mock_updown.add.assert_called_once_with(-5, attributes={})

    def test_record_updown_counter_with_labels(self) -> None:
        """Test record_metric passes labels to UpDownCounter."""
        config = OTELConfig(enabled=True, metrics_enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True
        backend._meter = mock.MagicMock()
        mock_updown = mock.MagicMock()
        backend._meter.create_up_down_counter.return_value = mock_updown

        backend.record_metric(
            "active.connections",
            3,
            labels={"service": "api"},
            metric_type="updown_counter",
        )

        mock_updown.add.assert_called_once_with(3, attributes={"service": "api"})

    def test_record_updown_counter_with_custom_unit(self) -> None:
        """Test record_metric uses custom unit for UpDownCounter."""
        config = OTELConfig(enabled=True, metrics_enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True
        backend._meter = mock.MagicMock()
        mock_updown = mock.MagicMock()
        backend._meter.create_up_down_counter.return_value = mock_updown

        backend.record_metric(
            "memory.usage",
            1024,
            metric_type="updown_counter",
            unit="bytes",
        )

        backend._meter.create_up_down_counter.assert_called_once_with(
            name="memory.usage",
            unit="bytes",
            description="UpDownCounter for memory.usage",
        )


class TestRecordMetricValidation:
    """Tests for validation in record_metric."""

    def test_invalid_metric_name_logs_warning(self) -> None:
        """Test invalid metric name logs warning but still records."""
        config = OTELConfig(enabled=True, metrics_enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True
        backend._meter = mock.MagicMock()
        mock_counter = mock.MagicMock()
        backend._meter.create_counter.return_value = mock_counter

        with mock.patch("libs.observability.otel_backend.logger") as mock_logger:
            backend.record_metric("Invalid Metric Name", 1, metric_type="counter")

            # Should log warning about invalid name
            mock_logger.warning.assert_called()
            warning_msg = mock_logger.warning.call_args[0][0]
            assert "Invalid Metric Name" in warning_msg

            # Should still create and record the metric (graceful degradation)
            mock_counter.add.assert_called_once()

    def test_high_cardinality_label_logs_warning(self) -> None:
        """Test high-cardinality label logs warning but still records."""
        config = OTELConfig(enabled=True, metrics_enabled=True)
        backend = OTELBackend(config)
        backend._initialized = True
        backend._meter = mock.MagicMock()
        mock_counter = mock.MagicMock()
        backend._meter.create_counter.return_value = mock_counter

        with mock.patch("libs.observability.otel_backend.logger") as mock_logger:
            backend.record_metric(
                "requests",
                1,
                labels={"user_id": "12345"},
                metric_type="counter",
            )

            # Should log warning about high cardinality
            mock_logger.warning.assert_called()
            warning_msg = mock_logger.warning.call_args[0][0]
            assert "user_id" in warning_msg

            # Should still record the metric
            mock_counter.add.assert_called_once()
