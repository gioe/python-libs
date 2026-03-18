"""Tests for graceful degradation when backends are disabled or fail.

This module verifies that the observability facade handles disabled backends
and runtime failures gracefully, ensuring:
1. No exceptions when backends disabled
2. Operations become no-ops with appropriate logging
3. Application continues to function
4. Initialization warns about disabled backends
"""

from __future__ import annotations

import logging
from unittest import mock

import pytest

from libs.observability.config import ObservabilityConfig, OTELConfig, RoutingConfig, SentryConfig
from libs.observability.facade import ObservabilityFacade, SpanContext


class TestSentryDisabledOTELEnabled:
    """Tests for scenario: Sentry disabled, OTEL enabled.

    In this configuration:
    - Error capture should log warning and return None
    - Metrics should work normally via OTEL
    - Spans should work via OTEL only
    - User/tag/context operations should be no-ops
    """

    @pytest.fixture
    def facade_sentry_disabled(self) -> ObservabilityFacade:
        """Create a facade with Sentry disabled and OTEL enabled."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(enabled=False),
            otel=OTELConfig(enabled=True, service_name="test-service"),
            routing=RoutingConfig(errors="sentry", metrics="otel", traces="otel"),
        )
        facade._sentry_backend = None  # Disabled
        facade._otel_backend = mock.MagicMock()
        facade._otel_backend.start_span.return_value = mock.MagicMock()
        return facade

    def test_capture_error_logs_warning_when_sentry_disabled(
        self, facade_sentry_disabled: ObservabilityFacade, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test capture_error logs warning when Sentry is disabled."""
        with caplog.at_level(logging.WARNING):
            result = facade_sentry_disabled.capture_error(ValueError("test error"))

        assert result is None
        assert "Sentry backend not available" in caplog.text
        assert "ValueError" in caplog.text

    def test_capture_error_does_not_raise_when_sentry_disabled(
        self, facade_sentry_disabled: ObservabilityFacade
    ) -> None:
        """Test capture_error does not raise exception when Sentry is disabled."""
        # Should not raise
        result = facade_sentry_disabled.capture_error(
            ValueError("test"),
            context={"key": "value"},
            level="error",
            user={"id": "user-123"},
            tags={"tag": "value"},
        )
        assert result is None

    def test_capture_message_returns_none_when_sentry_disabled(
        self, facade_sentry_disabled: ObservabilityFacade
    ) -> None:
        """Test capture_message returns None when Sentry is disabled."""
        result = facade_sentry_disabled.capture_message("test message", level="warning")
        assert result is None

    def test_record_event_returns_none_when_sentry_disabled(
        self, facade_sentry_disabled: ObservabilityFacade
    ) -> None:
        """Test record_event returns None when Sentry is disabled."""
        result = facade_sentry_disabled.record_event(
            "user.signup", data={"user_id": "123"}, tags={"source": "web"}
        )
        assert result is None

    def test_record_metric_works_when_sentry_disabled(
        self, facade_sentry_disabled: ObservabilityFacade
    ) -> None:
        """Test record_metric still works via OTEL when Sentry is disabled."""
        facade_sentry_disabled.record_metric(
            "test.counter", 1, labels={"label": "value"}, metric_type="counter"
        )

        facade_sentry_disabled._otel_backend.record_metric.assert_called_once_with(
            name="test.counter",
            value=1,
            labels={"label": "value"},
            metric_type="counter",
            unit=None,
        )

    def test_start_span_works_via_otel_when_sentry_disabled(
        self, facade_sentry_disabled: ObservabilityFacade
    ) -> None:
        """Test start_span works via OTEL when Sentry is disabled."""
        with facade_sentry_disabled.start_span("test_span", kind="server") as span:
            assert isinstance(span, SpanContext)
            span.set_attribute("key", "value")

        facade_sentry_disabled._otel_backend.start_span.assert_called_once_with(
            "test_span", kind="server", attributes=None
        )

    def test_set_user_is_noop_when_sentry_disabled(
        self, facade_sentry_disabled: ObservabilityFacade
    ) -> None:
        """Test set_user is a no-op when Sentry is disabled."""
        # Should not raise
        facade_sentry_disabled.set_user("user-123", username="alice", email="alice@test.com")
        # No assertion - just verify no exception

    def test_set_tag_is_noop_when_sentry_disabled(
        self, facade_sentry_disabled: ObservabilityFacade
    ) -> None:
        """Test set_tag is a no-op when Sentry is disabled."""
        # Should not raise
        facade_sentry_disabled.set_tag("environment", "production")
        # No assertion - just verify no exception

    def test_set_context_is_noop_when_sentry_disabled(
        self, facade_sentry_disabled: ObservabilityFacade
    ) -> None:
        """Test set_context is a no-op when Sentry is disabled."""
        # Should not raise
        facade_sentry_disabled.set_context("request", {"url": "/api/test"})
        # No assertion - just verify no exception


class TestOTELDisabledSentryEnabled:
    """Tests for scenario: OTEL disabled, Sentry enabled.

    In this configuration:
    - Metrics should log warning and be no-ops
    - Error capture should work normally via Sentry
    - Spans may work via Sentry depending on routing
    """

    @pytest.fixture
    def facade_otel_disabled(self) -> ObservabilityFacade:
        """Create a facade with OTEL disabled and Sentry enabled."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123", environment="test"),
            otel=OTELConfig(enabled=False, service_name="test-service"),
            routing=RoutingConfig(errors="sentry", metrics="otel", traces="sentry"),
        )
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_error.return_value = "event-id-123"
        facade._sentry_backend.capture_message.return_value = "msg-id-456"
        facade._sentry_backend.start_span.return_value = mock.MagicMock()
        facade._otel_backend = None  # Disabled
        return facade

    def test_record_metric_logs_warning_when_otel_disabled(
        self, facade_otel_disabled: ObservabilityFacade, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test record_metric logs warning when OTEL is disabled."""
        with caplog.at_level(logging.WARNING):
            facade_otel_disabled.record_metric("test.counter", 1)

        assert "OTEL backend not available" in caplog.text
        assert "test.counter" in caplog.text

    def test_record_metric_does_not_raise_when_otel_disabled(
        self, facade_otel_disabled: ObservabilityFacade
    ) -> None:
        """Test record_metric does not raise exception when OTEL is disabled."""
        # Should not raise
        facade_otel_disabled.record_metric(
            "test.histogram",
            42.5,
            labels={"endpoint": "/api"},
            metric_type="histogram",
            unit="ms",
        )

    def test_capture_error_works_when_otel_disabled(
        self, facade_otel_disabled: ObservabilityFacade
    ) -> None:
        """Test capture_error still works via Sentry when OTEL is disabled."""
        result = facade_otel_disabled.capture_error(
            ValueError("test error"),
            context={"operation": "test"},
            level="error",
        )

        assert result == "event-id-123"
        facade_otel_disabled._sentry_backend.capture_error.assert_called_once()

    def test_capture_message_works_when_otel_disabled(
        self, facade_otel_disabled: ObservabilityFacade
    ) -> None:
        """Test capture_message still works via Sentry when OTEL is disabled."""
        result = facade_otel_disabled.capture_message("test message", level="warning")

        assert result == "msg-id-456"
        facade_otel_disabled._sentry_backend.capture_message.assert_called_once()

    def test_start_span_works_via_sentry_when_otel_disabled(
        self, facade_otel_disabled: ObservabilityFacade
    ) -> None:
        """Test start_span works via Sentry when OTEL is disabled and routing=sentry."""
        with facade_otel_disabled.start_span("test_span", attributes={"key": "value"}) as span:
            assert isinstance(span, SpanContext)

        facade_otel_disabled._sentry_backend.start_span.assert_called_once_with(
            "test_span", attributes={"key": "value"}
        )

    def test_set_user_works_when_otel_disabled(
        self, facade_otel_disabled: ObservabilityFacade
    ) -> None:
        """Test set_user works via Sentry when OTEL is disabled."""
        facade_otel_disabled.set_user("user-123", username="alice")
        facade_otel_disabled._sentry_backend.set_user.assert_called_once()

    def test_set_tag_works_when_otel_disabled(
        self, facade_otel_disabled: ObservabilityFacade
    ) -> None:
        """Test set_tag works via Sentry when OTEL is disabled."""
        facade_otel_disabled.set_tag("environment", "production")
        facade_otel_disabled._sentry_backend.set_tag.assert_called_once_with(
            "environment", "production"
        )

    def test_set_context_works_when_otel_disabled(
        self, facade_otel_disabled: ObservabilityFacade
    ) -> None:
        """Test set_context works via Sentry when OTEL is disabled."""
        facade_otel_disabled.set_context("request", {"url": "/api/test"})
        facade_otel_disabled._sentry_backend.set_context.assert_called_once_with(
            "request", {"url": "/api/test"}
        )


class TestBothBackendsDisabled:
    """Tests for scenario: Both Sentry and OTEL disabled.

    In this configuration:
    - All observability operations become no-ops
    - Debug logging should occur for tracking
    - Application should continue functioning normally
    """

    @pytest.fixture
    def facade_both_disabled(self) -> ObservabilityFacade:
        """Create a facade with both backends disabled."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(enabled=False),
            otel=OTELConfig(enabled=False, service_name="test-service"),
        )
        facade._sentry_backend = None
        facade._otel_backend = None
        return facade

    def test_capture_error_logs_warning_both_disabled(
        self, facade_both_disabled: ObservabilityFacade, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test capture_error logs warning when both backends are disabled."""
        with caplog.at_level(logging.WARNING):
            result = facade_both_disabled.capture_error(ValueError("test"))

        assert result is None
        assert "Sentry backend not available" in caplog.text

    def test_record_metric_logs_warning_both_disabled(
        self, facade_both_disabled: ObservabilityFacade, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test record_metric logs warning when both backends are disabled."""
        with caplog.at_level(logging.WARNING):
            facade_both_disabled.record_metric("test.metric", 1)

        assert "OTEL backend not available" in caplog.text

    def test_start_span_logs_debug_both_disabled(
        self, facade_both_disabled: ObservabilityFacade, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test start_span yields empty SpanContext when both backends disabled."""
        # Note: When both backends are None but initialized=True, start_span won't hit
        # the debug log because it checks _initialized first. It will just yield empty spans.
        with facade_both_disabled.start_span("test_span") as span:
            assert isinstance(span, SpanContext)
            assert span._otel_span is None
            assert span._sentry_span is None
            # Operations on empty SpanContext should be no-ops
            span.set_attribute("key", "value")
            span.set_status("ok")
            span.add_event("test_event")

    def test_all_operations_are_noops_both_disabled(
        self, facade_both_disabled: ObservabilityFacade
    ) -> None:
        """Test all operations are no-ops when both backends disabled."""
        # None of these should raise
        facade_both_disabled.capture_error(ValueError("test"))
        facade_both_disabled.capture_message("test message")
        facade_both_disabled.record_metric("test.metric", 1)
        facade_both_disabled.record_event("test.event", data={"key": "value"})
        facade_both_disabled.set_user("user-123")
        facade_both_disabled.set_tag("key", "value")
        facade_both_disabled.set_context("name", {"key": "value"})

        with facade_both_disabled.start_span("test"):
            pass

    def test_flush_is_noop_both_disabled(
        self, facade_both_disabled: ObservabilityFacade
    ) -> None:
        """Test flush is a no-op when both backends disabled."""
        # Should not raise
        facade_both_disabled.flush(timeout=1.0)

    def test_shutdown_is_noop_both_disabled(
        self, facade_both_disabled: ObservabilityFacade
    ) -> None:
        """Test shutdown is a no-op when both backends disabled."""
        # Should not raise
        facade_both_disabled.shutdown()
        assert facade_both_disabled.is_initialized is False

    def test_get_trace_context_returns_none_values_both_disabled(
        self, facade_both_disabled: ObservabilityFacade
    ) -> None:
        """Test get_trace_context returns None values when both backends disabled."""
        result = facade_both_disabled.get_trace_context()
        assert result == {"trace_id": None, "span_id": None}


class TestInitializationWarnings:
    """Tests for initialization warnings about disabled backends."""

    def test_init_warns_when_no_backends_active(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test init logs warning when no backends are active."""
        facade = ObservabilityFacade()

        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(enabled=False),
                otel=OTELConfig(enabled=False, service_name="test"),
            )
            mock_load.return_value = mock_config

            with mock.patch("atexit.register"), caplog.at_level(logging.WARNING):
                result = facade.init()

            assert result is True
            assert "no backends are active" in caplog.text

    def test_init_succeeds_with_sentry_only(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test init succeeds and logs info with only Sentry enabled."""
        facade = ObservabilityFacade()

        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
                otel=OTELConfig(enabled=False, service_name="test"),
            )
            mock_load.return_value = mock_config

            with mock.patch(
                "libs.observability.sentry_backend.SentryBackend"
            ) as mock_sentry_cls:
                mock_sentry = mock.MagicMock()
                mock_sentry.init.return_value = True
                mock_sentry_cls.return_value = mock_sentry

                with mock.patch("atexit.register"), caplog.at_level(logging.INFO):
                    result = facade.init()

                assert result is True
                assert "Sentry" in caplog.text

    def test_init_succeeds_with_otel_only(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test init succeeds and logs info with only OTEL enabled."""
        facade = ObservabilityFacade()

        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(enabled=False),
                otel=OTELConfig(enabled=True, service_name="test"),
            )
            mock_load.return_value = mock_config

            with mock.patch("libs.observability.otel_backend.OTELBackend") as mock_otel_cls:
                mock_otel = mock.MagicMock()
                mock_otel.init.return_value = True
                mock_otel_cls.return_value = mock_otel

                with mock.patch("atexit.register"), caplog.at_level(logging.INFO):
                    result = facade.init()

                assert result is True
                assert "OpenTelemetry" in caplog.text


class TestBackendRuntimeFailures:
    """Tests for handling backend failures at runtime."""

    def test_capture_error_handles_sentry_runtime_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test capture_error handles Sentry failure gracefully at runtime."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(environment="test"),
            otel=OTELConfig(service_name="test"),
        )
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_error.side_effect = RuntimeError("Sentry SDK crashed")
        facade._otel_backend = mock.MagicMock()

        with caplog.at_level(logging.ERROR):
            result = facade.capture_error(ValueError("original error"))

        assert result is None
        assert "Failed to capture error to Sentry" in caplog.text
        assert "ValueError" in caplog.text
        assert "original error" in caplog.text

    def test_metrics_continue_after_sentry_failure(self) -> None:
        """Test OTEL metrics continue working after Sentry failure."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(environment="test"),
            otel=OTELConfig(service_name="test"),
        )
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_error.side_effect = RuntimeError("Sentry failed")
        facade._otel_backend = mock.MagicMock()

        # Sentry fails
        result = facade.capture_error(ValueError("test"))
        assert result is None

        # But OTEL metrics still work
        facade.record_metric("test.metric", 1)
        facade._otel_backend.record_metric.assert_called_once()

    def test_error_capture_continues_after_otel_span_failure(self) -> None:
        """Test Sentry error capture continues even if OTEL span fails."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(environment="test"),
            otel=OTELConfig(service_name="test"),
            routing=RoutingConfig(traces="otel"),
        )
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_error.return_value = "event-id"
        facade._otel_backend = mock.MagicMock()
        facade._otel_backend.start_span.side_effect = RuntimeError("OTEL span failed")

        # OTEL span fails, but Sentry error capture should still work
        result = facade.capture_error(
            ValueError("test"), context={"operation": "critical"}
        )

        assert result == "event-id"
        facade._sentry_backend.capture_error.assert_called_once()

    def test_flush_handles_sentry_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test flush continues to OTEL even if Sentry flush fails."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.flush.side_effect = RuntimeError("Sentry flush failed")
        facade._otel_backend = mock.MagicMock()

        with caplog.at_level(logging.WARNING):
            facade.flush(timeout=1.0)

        # Both should have been attempted
        facade._sentry_backend.flush.assert_called_once()
        facade._otel_backend.flush.assert_called_once()
        # Warning should be logged
        assert "Sentry backend flush failed" in caplog.text

    def test_flush_handles_otel_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test flush continues even if OTEL flush fails."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()
        facade._otel_backend = mock.MagicMock()
        facade._otel_backend.flush.side_effect = RuntimeError("OTEL flush failed")

        with caplog.at_level(logging.WARNING):
            facade.flush(timeout=1.0)

        # Both should have been attempted
        facade._sentry_backend.flush.assert_called_once()
        facade._otel_backend.flush.assert_called_once()
        # Warning should be logged
        assert "OTEL backend flush failed" in caplog.text

    def test_flush_handles_both_failures(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test flush completes even if both backends fail."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.flush.side_effect = RuntimeError("Sentry flush failed")
        facade._otel_backend = mock.MagicMock()
        facade._otel_backend.flush.side_effect = RuntimeError("OTEL flush failed")

        with caplog.at_level(logging.WARNING):
            facade.flush(timeout=1.0)

        # Both should have been attempted
        facade._sentry_backend.flush.assert_called_once()
        facade._otel_backend.flush.assert_called_once()
        # Both warnings should be logged
        assert "Sentry backend flush failed" in caplog.text
        assert "OTEL backend flush failed" in caplog.text

    def test_shutdown_handles_both_failures(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test shutdown completes even if both backends fail."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.shutdown.side_effect = RuntimeError("Sentry shutdown failed")
        facade._otel_backend = mock.MagicMock()
        facade._otel_backend.shutdown.side_effect = RuntimeError("OTEL shutdown failed")

        with caplog.at_level(logging.WARNING):
            facade.shutdown()

        # Both failures should be logged
        assert "Sentry backend shutdown failed" in caplog.text
        assert "OTEL backend shutdown failed" in caplog.text

        # But state should be properly cleaned up
        assert facade.is_initialized is False
        assert facade._sentry_backend is None
        assert facade._otel_backend is None


class TestNotInitializedScenarios:
    """Tests for operations when facade is not initialized."""

    @pytest.fixture
    def uninitialized_facade(self) -> ObservabilityFacade:
        """Create an uninitialized facade."""
        return ObservabilityFacade()

    def test_capture_error_logs_debug_when_not_initialized(
        self, uninitialized_facade: ObservabilityFacade, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test capture_error logs debug when not initialized."""
        with caplog.at_level(logging.DEBUG):
            result = uninitialized_facade.capture_error(ValueError("test"))

        assert result is None
        assert "observability not initialized" in caplog.text

    def test_capture_message_logs_debug_when_not_initialized(
        self, uninitialized_facade: ObservabilityFacade, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test capture_message logs debug when not initialized."""
        with caplog.at_level(logging.DEBUG):
            result = uninitialized_facade.capture_message("test")

        assert result is None
        assert "observability not initialized" in caplog.text

    def test_record_metric_logs_debug_when_not_initialized(
        self, uninitialized_facade: ObservabilityFacade, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test record_metric logs debug when not initialized."""
        with caplog.at_level(logging.DEBUG):
            uninitialized_facade.record_metric("test.metric", 1)

        assert "observability not initialized" in caplog.text
        assert "test.metric" in caplog.text

    def test_start_span_logs_debug_when_not_initialized(
        self, uninitialized_facade: ObservabilityFacade, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test start_span logs debug when not initialized."""
        with caplog.at_level(logging.DEBUG):
            with uninitialized_facade.start_span("test_span") as span:
                assert isinstance(span, SpanContext)
                assert span._otel_span is None
                assert span._sentry_span is None

        assert "observability not initialized" in caplog.text
        assert "test_span" in caplog.text

    def test_record_event_logs_debug_when_not_initialized(
        self, uninitialized_facade: ObservabilityFacade, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test record_event logs debug when not initialized."""
        with caplog.at_level(logging.DEBUG):
            result = uninitialized_facade.record_event("test.event")

        assert result is None
        assert "observability not initialized" in caplog.text
        assert "test.event" in caplog.text

    def test_set_user_logs_debug_when_not_initialized(
        self, uninitialized_facade: ObservabilityFacade, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test set_user logs debug when not initialized."""
        with caplog.at_level(logging.DEBUG):
            uninitialized_facade.set_user("user-123")

        assert "observability not initialized" in caplog.text

    def test_set_tag_logs_debug_when_not_initialized(
        self, uninitialized_facade: ObservabilityFacade, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test set_tag logs debug when not initialized."""
        with caplog.at_level(logging.DEBUG):
            uninitialized_facade.set_tag("key", "value")

        assert "observability not initialized" in caplog.text

    def test_set_context_logs_debug_when_not_initialized(
        self, uninitialized_facade: ObservabilityFacade, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test set_context logs debug when not initialized."""
        with caplog.at_level(logging.DEBUG):
            uninitialized_facade.set_context("name", {"key": "value"})

        assert "observability not initialized" in caplog.text

    def test_flush_logs_debug_when_not_initialized(
        self, uninitialized_facade: ObservabilityFacade, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test flush logs debug when not initialized."""
        with caplog.at_level(logging.DEBUG):
            uninitialized_facade.flush()

        assert "observability not initialized" in caplog.text

    def test_shutdown_logs_debug_when_not_initialized(
        self, uninitialized_facade: ObservabilityFacade, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test shutdown logs debug when not initialized."""
        with caplog.at_level(logging.DEBUG):
            uninitialized_facade.shutdown()

        assert "observability not initialized" in caplog.text

    def test_get_trace_context_returns_none_when_not_initialized(
        self, uninitialized_facade: ObservabilityFacade
    ) -> None:
        """Test get_trace_context returns None values when not initialized."""
        result = uninitialized_facade.get_trace_context()
        assert result == {"trace_id": None, "span_id": None}

    def test_all_operations_safe_when_not_initialized(
        self, uninitialized_facade: ObservabilityFacade
    ) -> None:
        """Test all operations are safe no-ops when not initialized."""
        # None of these should raise
        uninitialized_facade.capture_error(ValueError("test"))
        uninitialized_facade.capture_message("test")
        uninitialized_facade.record_metric("test.metric", 1)
        uninitialized_facade.record_event("test.event")
        uninitialized_facade.set_user("user-123")
        uninitialized_facade.set_tag("key", "value")
        uninitialized_facade.set_context("name", {})
        uninitialized_facade.get_trace_context()
        uninitialized_facade.flush()
        uninitialized_facade.shutdown()

        with uninitialized_facade.start_span("test"):
            pass


class TestSpanContextGracefulDegradation:
    """Tests for SpanContext graceful degradation with missing backends."""

    def test_span_context_operations_safe_with_no_backends(self) -> None:
        """Test SpanContext operations are safe when no backends are attached."""
        ctx = SpanContext("test")

        # All operations should be no-ops, not raise
        ctx.set_attribute("key", "value")
        ctx.set_status("ok")
        ctx.set_status("error", "description")
        ctx.record_exception(ValueError("test"))
        ctx.add_event("event_name")
        ctx.add_event("event_with_attrs", {"attr": "value"})
        ctx.set_http_attributes(method="GET", url="https://example.com", status_code=200)
        ctx.set_db_attributes(operation="SELECT", table="users", duration_ms=10.0)
        ctx.set_user_attributes(user_id="123", username="alice")
        ctx.set_error_attributes(ValueError("test"), escaped=True)

    def test_span_context_only_otel_backend(self) -> None:
        """Test SpanContext works with only OTEL backend."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span, sentry_span=None)

        ctx.set_attribute("key", "value")
        ctx.add_event("test_event")

        mock_otel_span.set_attribute.assert_called_once_with("key", "value")
        mock_otel_span.add_event.assert_called_once()

    def test_span_context_only_sentry_backend(self) -> None:
        """Test SpanContext works with only Sentry backend."""
        mock_sentry_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=None, sentry_span=mock_sentry_span)

        ctx.set_attribute("key", "value")

        mock_sentry_span.set_data.assert_called_once_with("key", "value")

    def test_span_context_exception_in_context_manager(self) -> None:
        """Test SpanContext handles exceptions properly in context manager."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        with pytest.raises(ValueError):
            with ctx:
                raise ValueError("test error")

        # Exception should be recorded
        mock_otel_span.record_exception.assert_called_once()
        mock_otel_span.set_status.assert_called_once()

    def test_span_context_exception_with_no_backends(self) -> None:
        """Test SpanContext handles exceptions when no backends attached."""
        ctx = SpanContext("test")

        # Should not raise additional errors
        with pytest.raises(ValueError):
            with ctx:
                raise ValueError("test error")
