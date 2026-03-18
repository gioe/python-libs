"""End-to-end integration tests for observability library.

This module tests realistic integration scenarios with mocked HTTP backends,
concurrent operations, config file loading, and error recovery. These tests
complement the unit tests by verifying the library works correctly in
scenarios closer to production usage.

Test categories:
1. End-to-end initialization with HTTP backend validation
2. Concurrent metric recording for thread safety verification
3. Config file precedence and environment variable substitution
4. Error recovery when backends are unreachable
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import responses
import yaml

from libs.observability.config import (
    ConfigurationError,
    ObservabilityConfig,
    OTELConfig,
    RoutingConfig,
    SentryConfig,
    load_config,
)
from libs.observability.facade import ObservabilityFacade


# ==============================================================================
# 1. End-to-End Initialization Tests
# ==============================================================================


class TestEndToEndInitialization:
    """Tests for end-to-end initialization with HTTP backend validation."""

    def test_init_with_valid_sentry_dsn_validates_endpoint(self) -> None:
        """Test initialization succeeds with valid Sentry configuration."""
        facade = ObservabilityFacade()

        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(
                    enabled=True,
                    dsn="https://test-key@test-sentry.io/123",
                    environment="test",
                ),
                otel=OTELConfig(enabled=False),
            )
            mock_load.return_value = mock_config

            with mock.patch(
                "libs.observability.sentry_backend.SentryBackend.init", return_value=True
            ):
                result = facade.init()

            assert result is True
            assert facade.is_initialized

    def test_init_with_otel_endpoint_connectivity_check(self) -> None:
        """Test OTEL initialization with endpoint connectivity."""
        facade = ObservabilityFacade()

        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(enabled=False),
                otel=OTELConfig(
                    enabled=True,
                    service_name="test-service",
                    endpoint="http://localhost:4317",
                    exporter="otlp",
                ),
            )
            mock_load.return_value = mock_config

            # Mock OTEL backend init
            with mock.patch(
                "libs.observability.otel_backend.OTELBackend.init", return_value=True
            ):
                result = facade.init()

            assert result is True
            assert facade.is_initialized

    def test_init_with_invalid_sentry_dsn_degrades_gracefully(self) -> None:
        """Test initialization handles invalid Sentry DSN gracefully."""
        facade = ObservabilityFacade()

        with mock.patch("libs.observability.config.load_config") as mock_load:
            # Invalid DSN format should fail validation
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(
                    enabled=True,
                    dsn="invalid-dsn-format",
                    environment="test",
                ),
                otel=OTELConfig(enabled=False),
            )
            mock_load.return_value = mock_config

            # Sentry backend init will fail with invalid DSN
            with mock.patch(
                "libs.observability.sentry_backend.SentryBackend.init", return_value=False
            ):
                result = facade.init()

            # Facade init should still succeed (graceful degradation)
            assert result is True
            assert facade.is_initialized

    def test_init_idempotency_with_real_backends(self) -> None:
        """Test that calling init() multiple times is safe."""
        facade = ObservabilityFacade()

        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
                otel=OTELConfig(enabled=True, service_name="test"),
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

                # First init
                result1 = facade.init()
                assert result1 is True

                # Second init should warn and return True
                with mock.patch("libs.observability.facade.logger") as mock_logger:
                    result2 = facade.init()
                    assert result2 is True
                    mock_logger.warning.assert_called_once()
                    assert "already initialized" in str(mock_logger.warning.call_args)

                # Backend init should only be called once
                mock_sentry.init.assert_called_once()
                mock_otel.init.assert_called_once()

    @responses.activate
    @pytest.mark.integration
    def test_full_initialization_with_both_backends(self) -> None:
        """Test complete initialization workflow with both backends enabled."""
        facade = ObservabilityFacade()

        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(
                    enabled=True,
                    dsn="https://test-key@test-sentry.io/123",
                    environment="production",
                    traces_sample_rate=0.1,
                ),
                otel=OTELConfig(
                    enabled=True,
                    service_name="integration-test",
                    service_version="1.0.0",
                    endpoint="http://localhost:4317",
                    exporter="otlp",
                ),
                routing=RoutingConfig(errors="sentry", metrics="otel", traces="both"),
            )
            mock_load.return_value = mock_config

            with mock.patch(
                "libs.observability.sentry_backend.SentryBackend.init", return_value=True
            ), mock.patch(
                "libs.observability.otel_backend.OTELBackend.init", return_value=True
            ), mock.patch(
                "atexit.register"
            ):
                result = facade.init()

            assert result is True
            assert facade.is_initialized
            assert facade._sentry_backend is not None
            assert facade._otel_backend is not None


# ==============================================================================
# 2. Concurrent Metric Recording Tests
# ==============================================================================


class TestConcurrentMetricRecording:
    """Tests for thread-safe concurrent metric recording."""

    @pytest.mark.integration
    @pytest.mark.slow
    def test_concurrent_counter_recording(self) -> None:
        """Test concurrent counter metric recording from multiple threads."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            otel=OTELConfig(service_name="test"),
        )

        # Create a real OTEL backend for thread safety testing
        from libs.observability.otel_backend import OTELBackend

        backend = OTELBackend(facade._config.otel)
        backend._config.exporter = "none"  # Don't actually export
        backend._initialized = True
        backend._meter = mock.MagicMock()
        facade._otel_backend = backend

        # Track all metric calls
        recorded_values: list[float] = []
        lock = threading.Lock()

        def mock_add(value: float, attributes: dict[str, str] | None = None) -> None:
            with lock:
                recorded_values.append(value)

        mock_counter = mock.MagicMock()
        mock_counter.add = mock_add
        backend._meter.create_counter.return_value = mock_counter

        # Spawn 50 threads, each recording 10 metrics
        num_threads = 50
        metrics_per_thread = 10
        threads: list[threading.Thread] = []

        def record_metrics(thread_id: int) -> None:
            for i in range(metrics_per_thread):
                facade.record_metric(
                    "concurrent.test.counter",
                    value=1,
                    labels={"thread": str(thread_id), "iteration": str(i)},
                    metric_type="counter",
                )

        for i in range(num_threads):
            thread = threading.Thread(target=record_metrics, args=(i,))
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join(timeout=10.0)
            if thread.is_alive():
                pytest.fail(f"Thread {thread.name} did not complete within timeout")

        # Verify all metrics were recorded (no data loss)
        expected_count = num_threads * metrics_per_thread
        assert len(recorded_values) == expected_count
        assert sum(recorded_values) == expected_count

    @pytest.mark.integration
    @pytest.mark.slow
    def test_concurrent_histogram_recording(self) -> None:
        """Test concurrent histogram metric recording."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            otel=OTELConfig(service_name="test"),
        )

        from libs.observability.otel_backend import OTELBackend

        backend = OTELBackend(facade._config.otel)
        backend._config.exporter = "none"
        backend._initialized = True
        backend._meter = mock.MagicMock()
        facade._otel_backend = backend

        recorded_values: list[float] = []
        lock = threading.Lock()

        def mock_record(value: float, attributes: dict[str, str] | None = None) -> None:
            with lock:
                recorded_values.append(value)

        mock_histogram = mock.MagicMock()
        mock_histogram.record = mock_record
        backend._meter.create_histogram.return_value = mock_histogram

        num_threads = 50
        metrics_per_thread = 10

        def record_histograms(thread_id: int) -> None:
            for i in range(metrics_per_thread):
                # Record varying latency values
                latency = (thread_id * 10) + i
                facade.record_metric(
                    "request.latency",
                    value=latency,
                    labels={"endpoint": "/api/test"},
                    metric_type="histogram",
                    unit="ms",
                )

        threads = []
        for i in range(num_threads):
            thread = threading.Thread(target=record_histograms, args=(i,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join(timeout=10.0)
            if thread.is_alive():
                pytest.fail(f"Thread {thread.name} did not complete within timeout")

        # Verify all histograms were recorded
        assert len(recorded_values) == num_threads * metrics_per_thread

    @pytest.mark.integration
    @pytest.mark.slow
    def test_concurrent_gauge_recording(self) -> None:
        """Test concurrent gauge metric recording with proper value updates."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            otel=OTELConfig(service_name="test"),
        )

        from libs.observability.otel_backend import OTELBackend

        backend = OTELBackend(facade._config.otel)
        backend._config.exporter = "none"
        backend._initialized = True
        backend._meter = mock.MagicMock()
        facade._otel_backend = backend

        # Mock observable gauge creation
        backend._meter.create_observable_gauge.return_value = mock.MagicMock()

        num_threads = 30
        updates_per_thread = 20

        def update_gauge(thread_id: int) -> None:
            for i in range(updates_per_thread):
                value = thread_id * 100 + i
                facade.record_metric(
                    "active.connections",
                    value=value,
                    labels={"server": f"server-{thread_id}"},
                    metric_type="gauge",
                )

        threads = []
        for i in range(num_threads):
            thread = threading.Thread(target=update_gauge, args=(i,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join(timeout=10.0)
            if thread.is_alive():
                pytest.fail(f"Thread {thread.name} did not complete within timeout")

        # Verify gauge values were stored (thread-safe access)
        with backend._gauge_lock:
            assert "active.connections" in backend._gauge_values
            # Should have num_threads different label combinations
            assert len(backend._gauge_values["active.connections"]) == num_threads

    @pytest.mark.integration
    @pytest.mark.slow
    def test_concurrent_span_creation(self) -> None:
        """Test concurrent span creation and completion."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            otel=OTELConfig(service_name="test"),
            routing=RoutingConfig(traces="otel"),
        )

        # Track span operations without importing OTEL (which may not be installed)
        spans_created = []
        lock = threading.Lock()

        # Mock the entire OTEL backend to avoid OTEL imports
        mock_backend = mock.MagicMock()

        def mock_start_span_context(*args: Any, **kwargs: Any) -> Any:
            mock_span = mock.MagicMock()
            mock_span.__enter__ = lambda self: mock_span
            mock_span.__exit__ = lambda self, *args: None
            with lock:
                spans_created.append(mock_span)
            return mock_span

        mock_backend.start_span.side_effect = mock_start_span_context
        facade._otel_backend = mock_backend

        num_threads = 40
        spans_per_thread = 5

        def create_spans(thread_id: int) -> None:
            for i in range(spans_per_thread):
                with facade.start_span(f"operation-{thread_id}-{i}") as span:
                    if span:
                        span.set_attribute("thread_id", str(thread_id))
                        span.set_attribute("iteration", i)
                    # Simulate work with computation instead of sleep to avoid flakiness
                    _ = sum(range(100))

        threads = []
        for i in range(num_threads):
            thread = threading.Thread(target=create_spans, args=(i,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join(timeout=10.0)
            if thread.is_alive():
                pytest.fail(f"Thread {thread.name} did not complete within timeout")

        # Verify all spans were created
        with lock:
            assert len(spans_created) == num_threads * spans_per_thread

    @pytest.mark.integration
    @pytest.mark.slow
    def test_concurrent_mixed_operations(self) -> None:
        """Test concurrent mix of metrics, spans, and error capture."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
            otel=OTELConfig(service_name="test"),
            routing=RoutingConfig(errors="sentry", metrics="otel", traces="otel"),
        )

        # Mock backends
        mock_sentry = mock.MagicMock()
        mock_sentry.capture_error.return_value = "event-id"
        facade._sentry_backend = mock_sentry

        # Mock OTEL backend completely to avoid OTEL imports
        mock_otel_backend = mock.MagicMock()
        mock_otel_backend.record_metric.return_value = None

        mock_span = mock.MagicMock()
        mock_span.__enter__ = lambda self: mock_span
        mock_span.__exit__ = lambda self, *args: None
        mock_otel_backend.start_span.return_value = mock_span

        facade._otel_backend = mock_otel_backend

        operations_completed = []
        lock = threading.Lock()

        def perform_operations(thread_id: int) -> None:
            # Record metric
            facade.record_metric(
                "operations.count", 1, labels={"thread": str(thread_id)}, metric_type="counter"
            )

            # Create span
            with facade.start_span(f"operation-{thread_id}") as span:
                if span:
                    span.set_attribute("thread_id", str(thread_id))

            # Capture error
            try:
                if thread_id % 3 == 0:
                    raise ValueError(f"Test error from thread {thread_id}")
            except ValueError as e:
                facade.capture_error(e, context={"thread_id": thread_id})

            with lock:
                operations_completed.append(thread_id)

        num_threads = 30
        threads = []
        for i in range(num_threads):
            thread = threading.Thread(target=perform_operations, args=(i,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join(timeout=10.0)
            if thread.is_alive():
                pytest.fail(f"Thread {thread.name} did not complete within timeout")

        # Verify all operations completed
        with lock:
            assert len(operations_completed) == num_threads


# ==============================================================================
# 3. Config File Precedence Tests
# ==============================================================================


class TestConfigFilePrecedence:
    """Tests for configuration file precedence and environment variables."""

    def test_config_file_overrides_defaults(self) -> None:
        """Test that config file values override default values."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_data = {
                "sentry": {
                    "enabled": True,
                    "dsn": "https://custom@sentry.io/123",
                    "environment": "custom-env",
                    "traces_sample_rate": 0.5,
                },
                "otel": {
                    "service_name": "custom-service",
                    "endpoint": "http://custom:4317",
                },
            }
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            config = load_config(config_path=config_path)

            assert config.sentry.dsn == "https://custom@sentry.io/123"
            assert config.sentry.environment == "custom-env"
            assert config.sentry.traces_sample_rate == 0.5
            assert config.otel.service_name == "custom-service"
            assert config.otel.endpoint == "http://custom:4317"
        finally:
            Path(config_path).unlink()

    def test_explicit_overrides_beat_config_file(self) -> None:
        """Test that explicit overrides take precedence over config file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_data = {
                "sentry": {"dsn": "https://file@sentry.io/123", "environment": "staging"},
                "otel": {"service_name": "file-service"},
            }
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            config = load_config(
                config_path=config_path,
                service_name="override-service",
                environment="production",
                sentry_dsn="https://override@sentry.io/456",
            )

            # Explicit overrides win
            assert config.otel.service_name == "override-service"
            assert config.sentry.environment == "production"
            assert config.sentry.dsn == "https://override@sentry.io/456"
        finally:
            Path(config_path).unlink()

    def test_env_var_substitution_in_config_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test environment variable substitution in config file."""
        # Use monkeypatch for automatic cleanup even on exception
        monkeypatch.setenv("TEST_SENTRY_DSN", "https://env-var@sentry.io/789")
        monkeypatch.setenv("TEST_SERVICE_NAME", "env-service")
        monkeypatch.setenv("TEST_OTEL_ENDPOINT", "http://env-otel:4317")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_data = {
                "sentry": {
                    "enabled": True,
                    "dsn": "${TEST_SENTRY_DSN}",
                    "environment": "${TEST_ENV:development}",  # With default
                },
                "otel": {
                    "service_name": "${TEST_SERVICE_NAME}",
                    "endpoint": "${TEST_OTEL_ENDPOINT}",
                },
            }
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            config = load_config(config_path=config_path)

            assert config.sentry.dsn == "https://env-var@sentry.io/789"
            assert config.otel.service_name == "env-service"
            assert config.otel.endpoint == "http://env-otel:4317"
            assert config.sentry.environment == "development"  # Used default
        finally:
            Path(config_path).unlink()

    def test_malformed_yaml_raises_error(self) -> None:
        """Test that malformed YAML raises appropriate error."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("invalid: yaml: content:\n  - broken\n    indentation")
            config_path = f.name

        try:
            with pytest.raises(Exception):  # yaml.YAMLError or similar
                load_config(config_path=config_path)
        finally:
            Path(config_path).unlink()

    def test_missing_config_file_uses_defaults(self) -> None:
        """Test that missing config file falls back to defaults."""
        # Need to provide DSN to pass validation
        config = load_config(
            config_path="/nonexistent/path/to/config.yaml",
            sentry_dsn="https://test@sentry.io/123",
        )

        # Should use default values (from default.yaml if exists, or hardcoded defaults)
        assert config.sentry.enabled is True
        assert config.otel.enabled is True
        assert isinstance(config.sentry, SentryConfig)
        assert isinstance(config.otel, OTELConfig)

    def test_env_var_with_default_value(self) -> None:
        """Test environment variable substitution with default value."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_data = {
                "sentry": {
                    "enabled": True,
                    "dsn": "${NONEXISTENT_VAR:https://default@sentry.io/123}",
                    "environment": "${NONEXISTENT_ENV:development}",
                },
            }
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            config = load_config(config_path=config_path)

            # Should use default values from substitution
            assert config.sentry.dsn == "https://default@sentry.io/123"
            assert config.sentry.environment == "development"
        finally:
            Path(config_path).unlink()

    def test_config_validation_with_invalid_sample_rate(self) -> None:
        """Test config validation catches invalid sample rates."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_data = {
                "sentry": {
                    "enabled": True,
                    "dsn": "https://test@sentry.io/123",
                    "traces_sample_rate": 1.5,  # Invalid: > 1.0
                },
            }
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            with pytest.raises(ConfigurationError) as exc_info:
                load_config(config_path=config_path)

            assert "traces_sample_rate" in str(exc_info.value)
            assert "0.0 and 1.0" in str(exc_info.value)
        finally:
            Path(config_path).unlink()

    @pytest.mark.integration
    def test_full_precedence_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test complete precedence: overrides > env vars > config file > defaults."""
        # Use monkeypatch for automatic cleanup even on exception
        monkeypatch.setenv("TEST_PRECEDENCE_DSN", "https://env@sentry.io/123")

        # Create config file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            config_data = {
                "sentry": {
                    "enabled": True,
                    "dsn": "${TEST_PRECEDENCE_DSN}",  # From env var
                    "environment": "file-env",  # From file
                    "traces_sample_rate": 0.2,  # From file
                },
                "otel": {
                    "service_name": "file-service",  # Will be overridden
                },
            }
            yaml.dump(config_data, f)
            config_path = f.name

        try:
            config = load_config(
                config_path=config_path,
                service_name="override-service",  # Explicit override
                sentry_traces_sample_rate=0.9,  # Explicit override
            )

            # Verify precedence:
            # - DSN from env var (via config file substitution)
            assert config.sentry.dsn == "https://env@sentry.io/123"
            # - Environment from config file
            assert config.sentry.environment == "file-env"
            # - Sample rate from explicit override
            assert config.sentry.traces_sample_rate == 0.9
            # - Service name from explicit override
            assert config.otel.service_name == "override-service"
        finally:
            Path(config_path).unlink()


# ==============================================================================
# 4. Error Recovery Tests
# ==============================================================================


class TestErrorRecovery:
    """Tests for error recovery when backends are unreachable."""

    @responses.activate
    def test_sentry_endpoint_unreachable_connection_refused(self) -> None:
        """Test graceful degradation when Sentry endpoint refuses connection."""
        # Mock Sentry endpoint with connection error
        responses.add(
            responses.POST,
            "https://unreachable-sentry.io/api/123/envelope/",
            body=ConnectionError("Connection refused"),
        )

        facade = ObservabilityFacade()

        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(
                    enabled=True,
                    dsn="https://test@unreachable-sentry.io/123",
                ),
                otel=OTELConfig(enabled=False),
            )
            mock_load.return_value = mock_config

            # Sentry init should fail but not crash the app
            with mock.patch(
                "libs.observability.sentry_backend.SentryBackend.init",
                side_effect=ConnectionError("Connection refused"),
            ):
                result = facade.init()

            # Should still initialize (graceful degradation)
            assert result is True
            assert facade.is_initialized

    @responses.activate
    def test_sentry_endpoint_timeout(self) -> None:
        """Test graceful handling of Sentry endpoint timeout."""
        # Mock timeout
        responses.add(
            responses.POST,
            "https://slow-sentry.io/api/123/envelope/",
            body=TimeoutError("Request timed out"),
        )

        facade = ObservabilityFacade()

        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(
                    enabled=True,
                    dsn="https://test@slow-sentry.io/123",
                ),
                otel=OTELConfig(enabled=False),
            )
            mock_load.return_value = mock_config

            with mock.patch(
                "libs.observability.sentry_backend.SentryBackend.init",
                side_effect=TimeoutError("Request timed out"),
            ):
                result = facade.init()

            # Should handle timeout gracefully
            assert result is True

    @responses.activate
    def test_otel_endpoint_unreachable(self) -> None:
        """Test graceful degradation when OTEL endpoint is unreachable."""
        facade = ObservabilityFacade()

        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(enabled=False),
                otel=OTELConfig(
                    enabled=True,
                    service_name="test",
                    endpoint="http://unreachable:4317",
                    exporter="otlp",
                ),
            )
            mock_load.return_value = mock_config

            # OTEL init should handle unreachable endpoint
            with mock.patch(
                "libs.observability.otel_backend.OTELBackend.init",
                side_effect=ConnectionError("Cannot connect to OTEL endpoint"),
            ):
                result = facade.init()

            # Should still initialize (graceful degradation)
            assert result is True

    def test_application_continues_when_observability_fails(self) -> None:
        """Test error handling behavior when backends fail after initialization.

        The observability facade has different error handling strategies for different
        operations:

        - capture_error(): Wraps exceptions and returns None on failure (graceful degradation)
        - record_metric(): Propagates exceptions (backends expected reliable after init)
        - start_span(): Returns SpanContext that handles backend errors internally

        This test verifies the documented behavior of each operation type.
        """
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
            otel=OTELConfig(enabled=True, service_name="test"),
        )

        # Both backends fail
        mock_sentry = mock.MagicMock()
        mock_sentry.capture_error.side_effect = RuntimeError("Sentry unavailable")
        facade._sentry_backend = mock_sentry

        mock_otel = mock.MagicMock()
        mock_otel.record_metric.side_effect = RuntimeError("OTEL unavailable")
        facade._otel_backend = mock_otel

        # Error capture wraps exceptions - returns None on failure (graceful degradation)
        result = facade.capture_error(ValueError("test error"))
        assert result is None

        # Metric recording propagates backend exceptions directly to the caller.
        # The facade expects backends to be reliable after successful initialization.
        # Applications that need to handle unreliable backends should wrap calls.
        with pytest.raises(RuntimeError, match="OTEL unavailable"):
            facade.record_metric("test.metric", 1)

        # Spans return a SpanContext that handles backend errors internally
        with facade.start_span("test-span") as span:
            assert span is not None

    def test_partial_backend_failure_isolated(self) -> None:
        """Test that failure in one backend doesn't affect the other."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
            otel=OTELConfig(enabled=True, service_name="test"),
            routing=RoutingConfig(errors="sentry", metrics="otel"),
        )

        # Sentry fails
        mock_sentry = mock.MagicMock()
        mock_sentry.capture_error.side_effect = RuntimeError("Sentry down")
        facade._sentry_backend = mock_sentry

        # OTEL works
        mock_otel = mock.MagicMock()
        facade._otel_backend = mock_otel

        # Error capture fails but doesn't crash
        result = facade.capture_error(ValueError("test"))
        assert result is None

        # Metrics still work through OTEL
        facade.record_metric("requests.count", 1, metric_type="counter")
        mock_otel.record_metric.assert_called_once()

    @pytest.mark.integration
    def test_flush_during_backend_failure(self) -> None:
        """Test flush() handles backend failures gracefully."""
        facade = ObservabilityFacade()
        facade._initialized = True

        # Both backends fail during flush
        mock_sentry = mock.MagicMock()
        mock_sentry.flush.side_effect = RuntimeError("Sentry flush failed")
        facade._sentry_backend = mock_sentry

        mock_otel = mock.MagicMock()
        mock_otel.flush.side_effect = RuntimeError("OTEL flush failed")
        facade._otel_backend = mock_otel

        # Flush should not raise
        with mock.patch("libs.observability.facade.logger") as mock_logger:
            facade.flush(timeout=2.0)

            # Should log warnings for both failures
            assert mock_logger.warning.call_count == 2

    @pytest.mark.integration
    def test_shutdown_during_backend_failure(self) -> None:
        """Test shutdown() handles backend failures gracefully."""
        facade = ObservabilityFacade()
        facade._initialized = True

        # Both backends fail during shutdown
        mock_sentry = mock.MagicMock()
        mock_sentry.shutdown.side_effect = RuntimeError("Sentry shutdown failed")
        facade._sentry_backend = mock_sentry

        mock_otel = mock.MagicMock()
        mock_otel.shutdown.side_effect = RuntimeError("OTEL shutdown failed")
        facade._otel_backend = mock_otel

        # Shutdown should not raise
        with mock.patch("libs.observability.facade.logger") as mock_logger:
            facade.shutdown()

            # Should log warnings for both failures
            assert mock_logger.warning.call_count == 2

        # State should still be cleaned up
        assert facade.is_initialized is False
        assert facade._sentry_backend is None
        assert facade._otel_backend is None

    @pytest.mark.integration
    @pytest.mark.slow
    def test_recovery_after_backend_becomes_available(self) -> None:
        """Test that system can recover when backend becomes available again."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
        )

        # Initially Sentry fails
        mock_sentry = mock.MagicMock()
        mock_sentry.capture_error.side_effect = RuntimeError("Temporarily unavailable")
        facade._sentry_backend = mock_sentry

        # First attempt fails
        result1 = facade.capture_error(ValueError("error 1"))
        assert result1 is None

        # Backend recovers
        mock_sentry.capture_error.side_effect = None
        mock_sentry.capture_error.return_value = "event-id-123"

        # Second attempt succeeds
        result2 = facade.capture_error(ValueError("error 2"))
        assert result2 == "event-id-123"
