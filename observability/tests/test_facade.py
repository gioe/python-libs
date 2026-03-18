"""Tests for observability facade."""

import logging
from unittest import mock

import pytest

from libs.observability.config import ObservabilityConfig, OTELConfig, SentryConfig
from libs.observability.facade import ObservabilityFacade, SpanContext


class TestObservabilityFacadeInit:
    """Tests for facade initialization."""

    def test_not_initialized_by_default(self) -> None:
        """Test facade is not initialized by default."""
        facade = ObservabilityFacade()
        assert facade.is_initialized is False

    def test_init_sets_initialized(self) -> None:
        """Test init() sets initialized flag."""
        facade = ObservabilityFacade()
        # Patch at the config module since that's where load_config is defined
        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(enabled=False),
                otel=OTELConfig(enabled=False),
            )
            mock_load.return_value = mock_config
            result = facade.init()
            assert result is True
            assert facade.is_initialized is True

    def test_init_with_disabled_backends(self) -> None:
        """Test init() with both backends disabled."""
        facade = ObservabilityFacade()
        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(enabled=False),
                otel=OTELConfig(enabled=False),
            )
            mock_load.return_value = mock_config
            facade.init()
            # Backends should be None when disabled
            assert facade._sentry_backend is None
            assert facade._otel_backend is None

    def test_init_is_idempotent(self) -> None:
        """Test init() is idempotent - calling twice doesn't reinitialize."""
        facade = ObservabilityFacade()
        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(enabled=False),
                otel=OTELConfig(enabled=False),
            )
            mock_load.return_value = mock_config

            # First init
            result1 = facade.init()
            assert result1 is True
            assert mock_load.call_count == 1

            # Second init should return True but not reinitialize
            result2 = facade.init()
            assert result2 is True
            # load_config should not be called again
            assert mock_load.call_count == 1

    def test_init_registers_atexit_handler(self) -> None:
        """Test init() registers an atexit handler for shutdown."""
        facade = ObservabilityFacade()
        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(enabled=False),
                otel=OTELConfig(enabled=False),
            )
            mock_load.return_value = mock_config

            with mock.patch("atexit.register") as mock_atexit:
                facade.init()
                mock_atexit.assert_called_once_with(facade._atexit_shutdown)
                assert facade._atexit_registered is True

    def test_init_registers_atexit_only_once(self) -> None:
        """Test atexit handler is only registered once even after shutdown and reinit."""
        facade = ObservabilityFacade()
        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(enabled=False),
                otel=OTELConfig(enabled=False),
            )
            mock_load.return_value = mock_config

            with mock.patch("atexit.register") as mock_atexit:
                # First init
                facade.init()
                assert mock_atexit.call_count == 1

                # Shutdown and reinit
                facade.shutdown()
                facade.init()
                # atexit should NOT be called again
                assert mock_atexit.call_count == 1

    def test_init_returns_false_on_config_error(self) -> None:
        """Test init() returns False when configuration fails."""
        from libs.observability.config import ConfigurationError

        facade = ObservabilityFacade()
        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_load.side_effect = ConfigurationError("Invalid configuration")

            with mock.patch("atexit.register"):
                result = facade.init()
                assert result is False
                assert facade.is_initialized is False

    def test_init_handles_sentry_backend_init_failure(self) -> None:
        """Test init() continues if Sentry backend init fails."""
        facade = ObservabilityFacade()
        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
                otel=OTELConfig(enabled=False),
            )
            mock_load.return_value = mock_config

            with mock.patch("libs.observability.sentry_backend.SentryBackend") as mock_sentry:
                mock_sentry.side_effect = RuntimeError("Sentry init failed")

                with mock.patch("atexit.register"):
                    result = facade.init()
                    # Should still succeed - graceful degradation
                    assert result is True
                    assert facade.is_initialized is True

    def test_init_handles_otel_backend_init_failure(self) -> None:
        """Test init() continues if OTEL backend init fails."""
        facade = ObservabilityFacade()
        with mock.patch("libs.observability.config.load_config") as mock_load:
            mock_config = ObservabilityConfig(
                sentry=SentryConfig(enabled=False),
                otel=OTELConfig(enabled=True, service_name="test"),
            )
            mock_load.return_value = mock_config

            with mock.patch("libs.observability.otel_backend.OTELBackend") as mock_otel:
                mock_otel.side_effect = RuntimeError("OTEL init failed")

                with mock.patch("atexit.register"):
                    result = facade.init()
                    # Should still succeed - graceful degradation
                    assert result is True
                    assert facade.is_initialized is True


class TestObservabilityFacadeAtexitShutdown:
    """Tests for atexit shutdown handler."""

    def test_atexit_shutdown_calls_flush_and_shutdown(self) -> None:
        """Test _atexit_shutdown flushes and shuts down backends."""
        facade = ObservabilityFacade()
        facade._initialized = True
        mock_sentry = mock.MagicMock()
        mock_otel = mock.MagicMock()
        facade._sentry_backend = mock_sentry
        facade._otel_backend = mock_otel

        facade._atexit_shutdown()

        mock_sentry.flush.assert_called_once_with(2.0)
        mock_otel.flush.assert_called_once_with(2.0)
        mock_sentry.shutdown.assert_called_once()
        mock_otel.shutdown.assert_called_once()

    def test_atexit_shutdown_handles_flush_errors(self) -> None:
        """Test _atexit_shutdown handles flush errors gracefully."""
        facade = ObservabilityFacade()
        facade._initialized = True
        mock_sentry = mock.MagicMock()
        mock_sentry.flush.side_effect = RuntimeError("flush failed")
        facade._sentry_backend = mock_sentry

        # Should not raise
        facade._atexit_shutdown()

        # Shutdown should still be called
        mock_sentry.shutdown.assert_called_once()

    def test_atexit_shutdown_handles_shutdown_errors(self) -> None:
        """Test _atexit_shutdown handles shutdown errors gracefully."""
        facade = ObservabilityFacade()
        facade._initialized = True
        mock_sentry = mock.MagicMock()
        mock_sentry.shutdown.side_effect = RuntimeError("shutdown failed")
        facade._sentry_backend = mock_sentry

        # Should not raise
        facade._atexit_shutdown()

    def test_atexit_shutdown_does_nothing_when_not_initialized(self) -> None:
        """Test _atexit_shutdown does nothing when not initialized."""
        facade = ObservabilityFacade()
        facade._initialized = False
        mock_sentry = mock.MagicMock()
        facade._sentry_backend = mock_sentry

        facade._atexit_shutdown()

        # Backend methods should not be called
        mock_sentry.flush.assert_not_called()
        mock_sentry.shutdown.assert_not_called()


class TestFacadeWithoutInit:
    """Tests for facade methods when not initialized."""

    def test_capture_error_returns_none(self) -> None:
        """Test capture_error returns None when not initialized."""
        facade = ObservabilityFacade()
        result = facade.capture_error(ValueError("test"))
        assert result is None

    def test_capture_message_returns_none(self) -> None:
        """Test capture_message returns None when not initialized."""
        facade = ObservabilityFacade()
        result = facade.capture_message("test message")
        assert result is None

    def test_record_metric_does_nothing(self) -> None:
        """Test record_metric does nothing when not initialized."""
        facade = ObservabilityFacade()
        # Should not raise
        facade.record_metric("test.metric", 1)

    def test_start_span_yields_empty_context(self) -> None:
        """Test start_span yields empty SpanContext when not initialized."""
        facade = ObservabilityFacade()
        with facade.start_span("test") as span:
            assert isinstance(span, SpanContext)
            assert span._otel_span is None
            assert span._sentry_span is None

    def test_set_user_does_nothing(self) -> None:
        """Test set_user does nothing when not initialized."""
        facade = ObservabilityFacade()
        # Should not raise
        facade.set_user("user-123")

    def test_set_tag_does_nothing(self) -> None:
        """Test set_tag does nothing when not initialized."""
        facade = ObservabilityFacade()
        # Should not raise
        facade.set_tag("key", "value")

    def test_set_context_does_nothing(self) -> None:
        """Test set_context does nothing when not initialized."""
        facade = ObservabilityFacade()
        # Should not raise
        facade.set_context("name", {"key": "value"})

    def test_record_event_returns_none(self) -> None:
        """Test record_event returns None when not initialized."""
        facade = ObservabilityFacade()
        result = facade.record_event("test.event", data={"key": "value"})
        assert result is None

    def test_flush_does_nothing(self) -> None:
        """Test flush does nothing when not initialized."""
        facade = ObservabilityFacade()
        # Should not raise
        facade.flush()

    def test_shutdown_does_nothing(self) -> None:
        """Test shutdown does nothing when not initialized."""
        facade = ObservabilityFacade()
        # Should not raise
        facade.shutdown()


class TestSpanContext:
    """Tests for SpanContext."""

    def test_set_attribute_with_no_spans(self) -> None:
        """Test set_attribute does nothing with no spans."""
        ctx = SpanContext("test")
        # Should not raise
        ctx.set_attribute("key", "value")

    def test_set_status_with_no_spans(self) -> None:
        """Test set_status does nothing with no spans."""
        ctx = SpanContext("test")
        # Should not raise
        ctx.set_status("ok")
        ctx.set_status("error", "description")

    def test_record_exception_with_no_spans(self) -> None:
        """Test record_exception does nothing with no spans."""
        ctx = SpanContext("test")
        # Should not raise
        ctx.record_exception(ValueError("test"))

    def test_context_manager_protocol(self) -> None:
        """Test SpanContext implements context manager protocol."""
        ctx = SpanContext("test")
        with ctx as span:
            assert span is ctx

    def test_context_manager_with_exception(self) -> None:
        """Test SpanContext handles exceptions in context manager."""
        ctx = SpanContext("test")
        with pytest.raises(ValueError):
            with ctx:
                raise ValueError("test error")

    def test_add_event_with_no_spans(self) -> None:
        """Test add_event does nothing with no spans."""
        ctx = SpanContext("test")
        # Should not raise
        ctx.add_event("cache_hit")
        ctx.add_event("cache_miss", {"key": "value"})

    def test_set_attribute_with_otel_span(self) -> None:
        """Test set_attribute calls OTEL span."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        ctx.set_attribute("key", "value")

        mock_otel_span.set_attribute.assert_called_once_with("key", "value")

    def test_set_attribute_with_sentry_span(self) -> None:
        """Test set_attribute calls Sentry span."""
        mock_sentry_span = mock.MagicMock()
        ctx = SpanContext("test", sentry_span=mock_sentry_span)

        ctx.set_attribute("key", "value")

        mock_sentry_span.set_data.assert_called_once_with("key", "value")

    def test_set_attribute_with_both_spans(self) -> None:
        """Test set_attribute calls both OTEL and Sentry spans."""
        mock_otel_span = mock.MagicMock()
        mock_sentry_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span, sentry_span=mock_sentry_span)

        ctx.set_attribute("key", "value")

        mock_otel_span.set_attribute.assert_called_once_with("key", "value")
        mock_sentry_span.set_data.assert_called_once_with("key", "value")

    def test_set_status_ok_with_otel_span(self) -> None:
        """Test set_status 'ok' calls OTEL span with OK status."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        # Mock the opentelemetry.trace module that gets imported inside set_status
        mock_status_code = mock.MagicMock()
        mock_status_code.OK = "OK"
        mock_status_code.ERROR = "ERROR"

        with mock.patch.dict(
            "sys.modules",
            {"opentelemetry": mock.MagicMock(), "opentelemetry.trace": mock.MagicMock(StatusCode=mock_status_code)},
        ):
            ctx.set_status("ok")

        mock_otel_span.set_status.assert_called_once()
        call_args = mock_otel_span.set_status.call_args[0]
        assert call_args[0] == "OK"

    def test_set_status_error_with_otel_span(self) -> None:
        """Test set_status 'error' calls OTEL span with ERROR status."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        # Mock the opentelemetry.trace module that gets imported inside set_status
        mock_status_code = mock.MagicMock()
        mock_status_code.OK = "OK"
        mock_status_code.ERROR = "ERROR"

        with mock.patch.dict(
            "sys.modules",
            {"opentelemetry": mock.MagicMock(), "opentelemetry.trace": mock.MagicMock(StatusCode=mock_status_code)},
        ):
            ctx.set_status("error", "Something went wrong")

        mock_otel_span.set_status.assert_called_once()
        call_args = mock_otel_span.set_status.call_args[0]
        assert call_args[0] == "ERROR"
        assert call_args[1] == "Something went wrong"

    def test_record_exception_with_otel_span(self) -> None:
        """Test record_exception calls OTEL span."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        exc = ValueError("test error")
        ctx.record_exception(exc)

        mock_otel_span.record_exception.assert_called_once_with(exc)

    def test_add_event_with_otel_span(self) -> None:
        """Test add_event calls OTEL span."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        ctx.add_event("cache_hit", {"key": "user:123"})

        mock_otel_span.add_event.assert_called_once_with("cache_hit", attributes={"key": "user:123"})

    def test_add_event_without_attributes(self) -> None:
        """Test add_event with no attributes."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        ctx.add_event("retry_attempt")

        mock_otel_span.add_event.assert_called_once_with("retry_attempt", attributes=None)


class TestSpanContextHttpAttributes:
    """Tests for set_http_attributes helper method."""

    def test_set_http_attributes_required_fields(self) -> None:
        """Test set_http_attributes sets method and url."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        ctx.set_http_attributes(method="GET", url="https://example.com/api/users")

        calls = mock_otel_span.set_attribute.call_args_list
        assert mock.call("http.request.method", "GET") in calls
        assert mock.call("url.full", "https://example.com/api/users") in calls

    def test_set_http_attributes_uppercases_method(self) -> None:
        """Test set_http_attributes uppercases the HTTP method."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        ctx.set_http_attributes(method="post", url="https://example.com/api")

        calls = mock_otel_span.set_attribute.call_args_list
        assert mock.call("http.request.method", "POST") in calls

    def test_set_http_attributes_with_status_code(self) -> None:
        """Test set_http_attributes includes status code when provided."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        ctx.set_http_attributes(method="GET", url="https://example.com", status_code=200)

        calls = mock_otel_span.set_attribute.call_args_list
        assert mock.call("http.response.status_code", 200) in calls

    def test_set_http_attributes_without_status_code(self) -> None:
        """Test set_http_attributes excludes status code when not provided."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        ctx.set_http_attributes(method="GET", url="https://example.com")

        # Verify status code was not set
        for call in mock_otel_span.set_attribute.call_args_list:
            assert call[0][0] != "http.response.status_code"

    def test_set_http_attributes_with_all_optional_fields(self) -> None:
        """Test set_http_attributes with all optional fields."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        ctx.set_http_attributes(
            method="POST",
            url="https://api.example.com/users",
            status_code=201,
            route="/users",
            request_size=1024,
            response_size=2048,
        )

        calls = mock_otel_span.set_attribute.call_args_list
        assert mock.call("http.request.method", "POST") in calls
        assert mock.call("url.full", "https://api.example.com/users") in calls
        assert mock.call("http.response.status_code", 201) in calls
        assert mock.call("http.route", "/users") in calls
        assert mock.call("http.request.body.size", 1024) in calls
        assert mock.call("http.response.body.size", 2048) in calls

    def test_set_http_attributes_without_span(self) -> None:
        """Test set_http_attributes does nothing without a span."""
        ctx = SpanContext("test")  # No otel_span or sentry_span

        # Should not raise
        ctx.set_http_attributes(method="GET", url="https://example.com", status_code=200)


class TestSpanContextDbAttributes:
    """Tests for set_db_attributes helper method."""

    def test_set_db_attributes_required_fields(self) -> None:
        """Test set_db_attributes sets operation."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        ctx.set_db_attributes(operation="SELECT")

        calls = mock_otel_span.set_attribute.call_args_list
        assert mock.call("db.operation", "SELECT") in calls

    def test_set_db_attributes_uppercases_operation(self) -> None:
        """Test set_db_attributes uppercases the operation."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        ctx.set_db_attributes(operation="insert")

        calls = mock_otel_span.set_attribute.call_args_list
        assert mock.call("db.operation", "INSERT") in calls

    def test_set_db_attributes_with_table(self) -> None:
        """Test set_db_attributes includes table when provided."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        ctx.set_db_attributes(operation="SELECT", table="users")

        calls = mock_otel_span.set_attribute.call_args_list
        assert mock.call("db.sql.table", "users") in calls

    def test_set_db_attributes_with_duration(self) -> None:
        """Test set_db_attributes includes duration when provided."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        ctx.set_db_attributes(operation="SELECT", table="users", duration_ms=42.5)

        calls = mock_otel_span.set_attribute.call_args_list
        assert mock.call("db.query.duration", 42.5) in calls

    def test_set_db_attributes_with_all_optional_fields(self) -> None:
        """Test set_db_attributes with all optional fields."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        ctx.set_db_attributes(
            operation="SELECT",
            table="users",
            duration_ms=15.3,
            db_system="postgresql",
            db_name="aiq_prod",
            statement="SELECT * FROM users WHERE id = $1",
        )

        calls = mock_otel_span.set_attribute.call_args_list
        assert mock.call("db.operation", "SELECT") in calls
        assert mock.call("db.sql.table", "users") in calls
        assert mock.call("db.query.duration", 15.3) in calls
        assert mock.call("db.system", "postgresql") in calls
        assert mock.call("db.name", "aiq_prod") in calls
        assert mock.call("db.statement", "SELECT * FROM users WHERE id = $1") in calls

    def test_set_db_attributes_without_span(self) -> None:
        """Test set_db_attributes does nothing without a span."""
        ctx = SpanContext("test")  # No otel_span or sentry_span

        # Should not raise
        ctx.set_db_attributes(operation="SELECT", table="users", duration_ms=10.0)


class TestSpanContextUserAttributes:
    """Tests for set_user_attributes helper method."""

    def test_set_user_attributes_with_user_id(self) -> None:
        """Test set_user_attributes sets user ID."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        ctx.set_user_attributes(user_id="user-123")

        calls = mock_otel_span.set_attribute.call_args_list
        assert mock.call("enduser.id", "user-123") in calls

    def test_set_user_attributes_with_username(self) -> None:
        """Test set_user_attributes includes username when provided."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        ctx.set_user_attributes(user_id="user-123", username="alice")

        calls = mock_otel_span.set_attribute.call_args_list
        assert mock.call("enduser.id", "user-123") in calls
        assert mock.call("enduser.username", "alice") in calls

    def test_set_user_attributes_with_role_and_scope(self) -> None:
        """Test set_user_attributes includes role and scope when provided."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        ctx.set_user_attributes(
            user_id="user-123",
            username="alice",
            role="admin",
            scope="read:users write:users",
        )

        calls = mock_otel_span.set_attribute.call_args_list
        assert mock.call("enduser.id", "user-123") in calls
        assert mock.call("enduser.username", "alice") in calls
        assert mock.call("enduser.role", "admin") in calls
        assert mock.call("enduser.scope", "read:users write:users") in calls

    def test_set_user_attributes_with_none_user_id(self) -> None:
        """Test set_user_attributes skips user_id when None."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        ctx.set_user_attributes(user_id=None, username="anonymous")

        calls = mock_otel_span.set_attribute.call_args_list
        # user_id should not be set
        for call in calls:
            assert call[0][0] != "enduser.id"
        # But username should be set
        assert mock.call("enduser.username", "anonymous") in calls

    def test_set_user_attributes_without_span(self) -> None:
        """Test set_user_attributes does nothing without a span."""
        ctx = SpanContext("test")  # No otel_span or sentry_span

        # Should not raise
        ctx.set_user_attributes(user_id="user-123", username="alice")


class TestSpanContextErrorAttributes:
    """Tests for set_error_attributes helper method."""

    def test_set_error_attributes_basic(self) -> None:
        """Test set_error_attributes sets exception type and message."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        exc = ValueError("Something went wrong")
        ctx.set_error_attributes(exc)

        calls = mock_otel_span.set_attribute.call_args_list
        assert mock.call("exception.type", "ValueError") in calls
        assert mock.call("exception.message", "Something went wrong") in calls
        assert mock.call("exception.escaped", True) in calls

    def test_set_error_attributes_with_escaped_false(self) -> None:
        """Test set_error_attributes with escaped=False."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        exc = RuntimeError("Handled error")
        ctx.set_error_attributes(exc, escaped=False)

        calls = mock_otel_span.set_attribute.call_args_list
        assert mock.call("exception.escaped", False) in calls

    def test_set_error_attributes_includes_module(self) -> None:
        """Test set_error_attributes includes module in exception type."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        # Use an exception from a non-builtins module
        from json import JSONDecodeError

        exc = JSONDecodeError("Expecting value", "doc", 0)
        ctx.set_error_attributes(exc)

        calls = mock_otel_span.set_attribute.call_args_list
        # Should include module prefix
        exception_type_call = [c for c in calls if c[0][0] == "exception.type"][0]
        assert "JSONDecodeError" in exception_type_call[0][1]

    def test_set_error_attributes_builtin_exception(self) -> None:
        """Test set_error_attributes handles builtin exceptions correctly."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        exc = KeyError("missing_key")
        ctx.set_error_attributes(exc)

        calls = mock_otel_span.set_attribute.call_args_list
        # Builtin exceptions should not have module prefix
        assert mock.call("exception.type", "KeyError") in calls

    def test_set_error_attributes_without_span(self) -> None:
        """Test set_error_attributes does nothing without a span."""
        ctx = SpanContext("test")  # No otel_span or sentry_span

        # Should not raise
        ctx.set_error_attributes(ValueError("test"))


class TestSpanContextAttributeHelperWithSentry:
    """Tests for attribute helpers with Sentry backend."""

    def test_set_http_attributes_with_sentry_span(self) -> None:
        """Test set_http_attributes works with Sentry span."""
        mock_sentry_span = mock.MagicMock()
        ctx = SpanContext("test", sentry_span=mock_sentry_span)

        ctx.set_http_attributes(method="GET", url="https://example.com", status_code=200)

        calls = mock_sentry_span.set_data.call_args_list
        assert mock.call("http.request.method", "GET") in calls
        assert mock.call("url.full", "https://example.com") in calls
        assert mock.call("http.response.status_code", 200) in calls

    def test_set_db_attributes_with_both_spans(self) -> None:
        """Test set_db_attributes works with both OTEL and Sentry spans."""
        mock_otel_span = mock.MagicMock()
        mock_sentry_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span, sentry_span=mock_sentry_span)

        ctx.set_db_attributes(operation="SELECT", table="users")

        # Check OTEL span
        otel_calls = mock_otel_span.set_attribute.call_args_list
        assert mock.call("db.operation", "SELECT") in otel_calls
        assert mock.call("db.sql.table", "users") in otel_calls

        # Check Sentry span
        sentry_calls = mock_sentry_span.set_data.call_args_list
        assert mock.call("db.operation", "SELECT") in sentry_calls
        assert mock.call("db.sql.table", "users") in sentry_calls

    def test_context_manager_records_exception_on_error(self) -> None:
        """Test context manager records exception when error occurs."""
        mock_otel_span = mock.MagicMock()
        ctx = SpanContext("test", otel_span=mock_otel_span)

        with pytest.raises(ValueError):
            with ctx:
                raise ValueError("test error")

        mock_otel_span.record_exception.assert_called_once()
        mock_otel_span.set_status.assert_called_once()


class TestStartSpanWithBackends:
    """Tests for start_span with mocked backends."""

    def test_start_span_with_otel_backend_only(self) -> None:
        """Test start_span with only OTEL backend enabled."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._otel_backend = mock.MagicMock()
        facade._sentry_backend = None

        mock_otel_context = mock.MagicMock()
        facade._otel_backend.start_span.return_value = mock_otel_context

        mock_config = mock.MagicMock()
        mock_config.routing.traces = "otel"
        facade._config = mock_config

        with facade.start_span("test_span", attributes={"key": "value"}) as span:
            assert isinstance(span, SpanContext)

        facade._otel_backend.start_span.assert_called_once_with(
            "test_span", kind="internal", attributes={"key": "value"}
        )

    def test_start_span_with_sentry_backend_only(self) -> None:
        """Test start_span with only Sentry backend enabled."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._otel_backend = None
        facade._sentry_backend = mock.MagicMock()

        mock_sentry_context = mock.MagicMock()
        facade._sentry_backend.start_span.return_value = mock_sentry_context

        mock_config = mock.MagicMock()
        mock_config.routing.traces = "sentry"
        facade._config = mock_config

        with facade.start_span("test_span", attributes={"key": "value"}) as span:
            assert isinstance(span, SpanContext)

        facade._sentry_backend.start_span.assert_called_once_with(
            "test_span", attributes={"key": "value"}
        )

    def test_start_span_with_both_backends(self) -> None:
        """Test start_span with both backends enabled (routing=both)."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._otel_backend = mock.MagicMock()
        facade._sentry_backend = mock.MagicMock()

        mock_otel_context = mock.MagicMock()
        mock_sentry_context = mock.MagicMock()
        facade._otel_backend.start_span.return_value = mock_otel_context
        facade._sentry_backend.start_span.return_value = mock_sentry_context

        mock_config = mock.MagicMock()
        mock_config.routing.traces = "both"
        facade._config = mock_config

        with facade.start_span("test_span", kind="server", attributes={"key": "value"}) as span:
            assert isinstance(span, SpanContext)

        facade._otel_backend.start_span.assert_called_once_with(
            "test_span", kind="server", attributes={"key": "value"}
        )
        facade._sentry_backend.start_span.assert_called_once_with(
            "test_span", attributes={"key": "value"}
        )

    def test_start_span_with_different_kinds(self) -> None:
        """Test start_span with different span kinds."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._otel_backend = mock.MagicMock()
        facade._otel_backend.start_span.return_value = mock.MagicMock()

        mock_config = mock.MagicMock()
        mock_config.routing.traces = "otel"
        facade._config = mock_config

        for kind in ["internal", "server", "client", "producer", "consumer"]:
            with facade.start_span("test_span", kind=kind):  # type: ignore[arg-type]
                pass

            facade._otel_backend.start_span.assert_called_with(
                "test_span", kind=kind, attributes=None
            )

    def test_nested_spans(self) -> None:
        """Test nested spans work correctly."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._otel_backend = mock.MagicMock()

        outer_context = mock.MagicMock()
        inner_context = mock.MagicMock()
        facade._otel_backend.start_span.side_effect = [outer_context, inner_context]

        mock_config = mock.MagicMock()
        mock_config.routing.traces = "otel"
        facade._config = mock_config

        with facade.start_span("outer") as outer_span:
            assert isinstance(outer_span, SpanContext)
            with facade.start_span("inner") as inner_span:
                assert isinstance(inner_span, SpanContext)

        assert facade._otel_backend.start_span.call_count == 2

    def test_start_span_cleans_up_on_exception(self) -> None:
        """Test start_span properly cleans up contexts when exception is raised.

        The ExitStack ensures both OTEL and Sentry contexts are properly exited
        even when an exception occurs within the span.
        """
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._otel_backend = mock.MagicMock()
        facade._sentry_backend = mock.MagicMock()

        mock_otel_context = mock.MagicMock()
        mock_sentry_context = mock.MagicMock()
        facade._otel_backend.start_span.return_value = mock_otel_context
        facade._sentry_backend.start_span.return_value = mock_sentry_context

        mock_config = mock.MagicMock()
        mock_config.routing.traces = "both"
        facade._config = mock_config

        # When an exception is raised within the span
        with pytest.raises(ValueError, match="test error"):
            with facade.start_span("test_span"):
                raise ValueError("test error")

        # Both contexts should have __exit__ called (via ExitStack.enter_context)
        mock_otel_context.__exit__.assert_called_once()
        mock_sentry_context.__exit__.assert_called_once()


class TestFacadeCaptureMethods:
    """Tests for facade capture methods with mocked backends."""

    def test_capture_error_calls_sentry_backend(self) -> None:
        """Test capture_error delegates to Sentry backend with enriched context."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(environment="test"),
            otel=OTELConfig(service_name="test-service", service_version="1.0.0"),
        )
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_error.return_value = "event-id-123"

        exc = ValueError("test error")
        result = facade.capture_error(
            exc,
            context={"key": "value"},
            level="error",
            user={"id": "user-123"},
            tags={"tag": "value"},
            fingerprint=["custom"],
        )

        assert result == "event-id-123"
        # Verify call was made
        facade._sentry_backend.capture_error.assert_called_once()
        call_kwargs = facade._sentry_backend.capture_error.call_args.kwargs

        # Check enriched context includes service metadata
        assert call_kwargs["context"]["key"] == "value"
        assert call_kwargs["context"]["service"]["name"] == "test-service"
        assert call_kwargs["context"]["service"]["version"] == "1.0.0"
        assert call_kwargs["context"]["service"]["environment"] == "test"
        # Trace context should NOT be added when no span active (no None values)
        assert "trace" not in call_kwargs["context"]

        # Check other parameters passed through
        assert call_kwargs["exception"] is exc
        assert call_kwargs["level"] == "error"
        assert call_kwargs["user"] == {"id": "user-123"}
        assert call_kwargs["tags"] == {"tag": "value"}
        assert call_kwargs["fingerprint"] == ["custom"]

    def test_capture_error_logs_warning_when_sentry_disabled(self) -> None:
        """Test capture_error logs warning when Sentry backend not available."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = None  # Sentry disabled

        exc = ValueError("test error")
        with mock.patch("libs.observability.facade.logger") as mock_logger:
            result = facade.capture_error(exc)

        assert result is None
        mock_logger.warning.assert_called_once()
        assert "Sentry backend not available" in mock_logger.warning.call_args[0][0]

    def test_capture_error_handles_backend_exception(self) -> None:
        """Test capture_error logs error and returns None when backend fails."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig()
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_error.side_effect = RuntimeError("SDK failure")

        exc = ValueError("test error")
        with mock.patch("libs.observability.facade.logger") as mock_logger:
            result = facade.capture_error(exc)

        assert result is None
        mock_logger.error.assert_called_once()
        assert "Failed to capture error to Sentry" in mock_logger.error.call_args[0][0]

    def test_capture_error_enriches_empty_context(self) -> None:
        """Test capture_error adds service context even when no context provided."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(environment="prod"),
            otel=OTELConfig(service_name="my-service", service_version="2.0.0"),
        )
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_error.return_value = "event-id"

        exc = ValueError("test")
        facade.capture_error(exc)  # No context parameter

        call_kwargs = facade._sentry_backend.capture_error.call_args.kwargs
        assert "service" in call_kwargs["context"]
        assert call_kwargs["context"]["service"]["name"] == "my-service"
        assert call_kwargs["context"]["service"]["version"] == "2.0.0"

    def test_capture_error_when_config_is_none(self) -> None:
        """Test capture_error works when config is not set."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = None  # No config
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_error.return_value = "event-id"

        exc = ValueError("test")
        result = facade.capture_error(exc)

        # Should still work, just without service context
        assert result == "event-id"
        call_kwargs = facade._sentry_backend.capture_error.call_args.kwargs
        assert "service" not in call_kwargs["context"]

    def test_capture_error_includes_trace_context_when_span_active(self) -> None:
        """Test capture_error includes trace context when OTEL span is active."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._config = ObservabilityConfig(
            sentry=SentryConfig(environment="test"),
            otel=OTELConfig(service_name="test-service", service_version="1.0.0"),
        )
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_error.return_value = "event-id"

        # Create mock OTEL objects
        mock_span = mock.MagicMock()
        mock_span_context = mock.MagicMock()
        mock_span_context.trace_id = 0x12345678901234567890123456789012
        mock_span_context.span_id = 0x1234567890123456
        mock_span_context.is_valid = True
        mock_span.get_span_context.return_value = mock_span_context

        # Create mock trace module
        mock_trace_module = mock.MagicMock()
        mock_trace_module.get_current_span.return_value = mock_span

        # Create mock opentelemetry package
        mock_otel = mock.MagicMock()
        mock_otel.trace = mock_trace_module

        # Use mock.patch.dict for pytest-safe module mocking
        with mock.patch.dict(
            "sys.modules",
            {"opentelemetry": mock_otel, "opentelemetry.trace": mock_trace_module},
        ):
            exc = ValueError("test error")
            facade.capture_error(exc)

            call_kwargs = facade._sentry_backend.capture_error.call_args.kwargs
            # Trace context should be included
            assert "trace" in call_kwargs["context"]
            assert call_kwargs["context"]["trace"]["trace_id"] == "12345678901234567890123456789012"
            assert call_kwargs["context"]["trace"]["span_id"] == "1234567890123456"

    def test_capture_message_calls_sentry_backend(self) -> None:
        """Test capture_message delegates to Sentry backend."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_message.return_value = "event-id-456"

        result = facade.capture_message(
            "test message",
            level="warning",
            context={"key": "value"},
            tags={"tag": "value"},
        )

        assert result == "event-id-456"
        facade._sentry_backend.capture_message.assert_called_once_with(
            message="test message",
            level="warning",
            context={"key": "value"},
            tags={"tag": "value"},
        )

    def test_record_event_calls_sentry_backend(self) -> None:
        """Test record_event delegates to Sentry backend via capture_message."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_message.return_value = "event-id-789"

        result = facade.record_event(
            "user.signup",
            data={"user_id": "123", "method": "oauth"},
            level="info",
            tags={"source": "web"},
        )

        assert result == "event-id-789"
        facade._sentry_backend.capture_message.assert_called_once_with(
            message="Event: user.signup",
            level="info",
            context={"user_id": "123", "method": "oauth"},
            tags={"source": "web"},
        )

    def test_record_event_without_data(self) -> None:
        """Test record_event works with no data provided."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_message.return_value = "event-id-000"

        result = facade.record_event("test.event")

        assert result == "event-id-000"
        facade._sentry_backend.capture_message.assert_called_once_with(
            message="Event: test.event",
            level="info",
            context=None,
            tags=None,
        )

    def test_record_event_returns_none_without_sentry(self) -> None:
        """Test record_event returns None when Sentry backend is not configured."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = None

        result = facade.record_event("test.event", data={"key": "value"})

        assert result is None

    def test_record_event_validates_data_is_json_serializable(self) -> None:
        """Test record_event raises ValueError if data is not JSON-serializable."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        class CustomClass:
            pass

        with pytest.raises(ValueError, match="non-JSON-serializable"):
            facade.record_event("test.event", data={"custom": CustomClass()})

        # Backend should not be called
        facade._sentry_backend.capture_message.assert_not_called()

    def test_record_event_validates_data_is_dict(self) -> None:
        """Test record_event raises ValueError if data is not a dict."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        with pytest.raises(ValueError, match="Data must be a dictionary"):
            facade.record_event("test.event", data="not a dict")  # type: ignore[arg-type]

        # Backend should not be called
        facade._sentry_backend.capture_message.assert_not_called()

    def test_record_event_identifies_problematic_key(self) -> None:
        """Test record_event error identifies which key has non-serializable value."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        class CustomClass:
            pass

        data = {
            "good_key": "good_value",
            "bad_key": CustomClass(),
        }

        with pytest.raises(ValueError) as exc_info:
            facade.record_event("test.event", data=data)

        error_msg = str(exc_info.value)
        assert "bad_key" in error_msg
        assert "CustomClass" in error_msg

        # Backend should not be called
        facade._sentry_backend.capture_message.assert_not_called()

    def test_record_event_validates_even_when_not_initialized(self) -> None:
        """Test record_event validation happens even when not initialized."""
        facade = ObservabilityFacade()
        # Not initialized
        assert not facade._initialized

        class CustomClass:
            pass

        with pytest.raises(ValueError, match="non-JSON-serializable"):
            facade.record_event("test.event", data={"custom": CustomClass()})

    def test_record_event_allows_none_data(self) -> None:
        """Test record_event accepts None for data parameter."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_message.return_value = "event-id"

        # Should not raise - None is valid
        result = facade.record_event("test.event", data=None)

        assert result == "event-id"
        facade._sentry_backend.capture_message.assert_called_once()


class TestFacadeMetricMethods:
    """Tests for facade metric methods with mocked backends."""

    def test_record_metric_calls_otel_backend(self) -> None:
        """Test record_metric delegates to OTEL backend."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._otel_backend = mock.MagicMock()

        facade.record_metric(
            "test.metric",
            42,
            labels={"label": "value"},
            metric_type="counter",
            unit="requests",
        )

        facade._otel_backend.record_metric.assert_called_once_with(
            name="test.metric",
            value=42,
            labels={"label": "value"},
            metric_type="counter",
            unit="requests",
        )

    def test_record_metric_logs_warning_when_otel_backend_unavailable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test record_metric logs warning when OTEL backend is None."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._otel_backend = None

        with caplog.at_level(logging.WARNING):
            facade.record_metric("test.metric", 42)

        assert "record_metric called but OTEL backend not available" in caplog.text
        assert "test.metric" in caplog.text


class TestFacadeContextMethods:
    """Tests for facade context methods with mocked backends."""

    def test_set_user_calls_sentry_backend(self) -> None:
        """Test set_user delegates to Sentry backend."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        facade.set_user("user-123", email="test@example.com")

        facade._sentry_backend.set_user.assert_called_once_with(
            "user-123", email="test@example.com"
        )

    def test_set_user_with_all_fields(self) -> None:
        """Test set_user passes all fields to Sentry backend."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        facade.set_user(
            "user-123",
            username="alice",
            email="alice@example.com",
            ip_address="192.168.1.1",
        )

        facade._sentry_backend.set_user.assert_called_once_with(
            "user-123",
            username="alice",
            email="alice@example.com",
            ip_address="192.168.1.1",
        )

    def test_set_user_with_none_clears_user(self) -> None:
        """Test set_user with None clears user context."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        facade.set_user(None)

        facade._sentry_backend.set_user.assert_called_once_with(None)

    def test_set_tag_calls_sentry_backend(self) -> None:
        """Test set_tag delegates to Sentry backend."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        facade.set_tag("key", "value")

        facade._sentry_backend.set_tag.assert_called_once_with("key", "value")

    def test_set_tag_validates_key_is_string(self) -> None:
        """Test set_tag raises ValueError if key is not a string."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        with pytest.raises(ValueError, match="Tag key must be a string"):
            facade.set_tag(123, "value")  # type: ignore[arg-type]

        # Backend should not be called
        facade._sentry_backend.set_tag.assert_not_called()

    def test_set_tag_validates_value_is_string(self) -> None:
        """Test set_tag raises ValueError if value is not a string."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        with pytest.raises(ValueError, match="Tag value must be a string"):
            facade.set_tag("key", 123)  # type: ignore[arg-type]

        # Backend should not be called
        facade._sentry_backend.set_tag.assert_not_called()

    def test_set_tag_validates_key_length(self) -> None:
        """Test set_tag raises ValueError if key exceeds max length."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        too_long_key = "a" * 201  # Max is 200

        with pytest.raises(ValueError, match="Tag key exceeds maximum length"):
            facade.set_tag(too_long_key, "value")

        # Backend should not be called
        facade._sentry_backend.set_tag.assert_not_called()

    def test_set_tag_validates_value_length(self) -> None:
        """Test set_tag raises ValueError if value exceeds max length."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        too_long_value = "a" * 201  # Max is 200

        with pytest.raises(ValueError, match="Tag value exceeds maximum length"):
            facade.set_tag("key", too_long_value)

        # Backend should not be called
        facade._sentry_backend.set_tag.assert_not_called()

    def test_set_tag_validates_even_when_not_initialized(self) -> None:
        """Test set_tag validation happens even when not initialized."""
        facade = ObservabilityFacade()
        # Not initialized
        assert not facade._initialized

        with pytest.raises(ValueError, match="Tag key must be a string"):
            facade.set_tag(123, "value")  # type: ignore[arg-type]

    def test_set_context_calls_sentry_backend(self) -> None:
        """Test set_context delegates to Sentry backend."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        facade.set_context("request", {"url": "/test"})

        facade._sentry_backend.set_context.assert_called_once_with(
            "request", {"url": "/test"}
        )


class TestFacadeLifecycleMethods:
    """Tests for facade flush and shutdown methods."""

    def test_flush_calls_both_backends(self) -> None:
        """Test flush calls both backends."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()
        facade._otel_backend = mock.MagicMock()

        facade.flush(timeout=5.0)

        facade._sentry_backend.flush.assert_called_once_with(5.0)
        facade._otel_backend.flush.assert_called_once_with(5.0)

    def test_shutdown_calls_both_backends(self) -> None:
        """Test shutdown calls both backends and clears initialized flag."""
        facade = ObservabilityFacade()
        facade._initialized = True
        mock_sentry = mock.MagicMock()
        mock_otel = mock.MagicMock()
        facade._sentry_backend = mock_sentry
        facade._otel_backend = mock_otel

        facade.shutdown()

        mock_sentry.shutdown.assert_called_once()
        mock_otel.shutdown.assert_called_once()
        assert facade.is_initialized is False
        # Backend references should be cleared
        assert facade._sentry_backend is None
        assert facade._otel_backend is None
        assert facade._config is None

    def test_shutdown_is_idempotent(self) -> None:
        """Test shutdown can be called multiple times safely."""
        facade = ObservabilityFacade()
        facade._initialized = True
        mock_sentry = mock.MagicMock()
        facade._sentry_backend = mock_sentry

        # First shutdown
        facade.shutdown()
        assert mock_sentry.shutdown.call_count == 1

        # Second shutdown should not call backend again (it's None now)
        facade.shutdown()
        assert mock_sentry.shutdown.call_count == 1

    def test_shutdown_handles_sentry_error_gracefully(self) -> None:
        """Test shutdown continues to OTEL even if Sentry shutdown fails."""
        facade = ObservabilityFacade()
        facade._initialized = True
        mock_sentry = mock.MagicMock()
        mock_otel = mock.MagicMock()
        mock_sentry.shutdown.side_effect = RuntimeError("Sentry shutdown failed")
        facade._sentry_backend = mock_sentry
        facade._otel_backend = mock_otel

        # Should not raise, OTEL should still be shut down
        facade.shutdown()

        mock_sentry.shutdown.assert_called_once()
        mock_otel.shutdown.assert_called_once()
        assert facade.is_initialized is False
        assert facade._sentry_backend is None
        assert facade._otel_backend is None

    def test_shutdown_handles_otel_error_gracefully(self) -> None:
        """Test shutdown completes even if OTEL shutdown fails."""
        facade = ObservabilityFacade()
        facade._initialized = True
        mock_sentry = mock.MagicMock()
        mock_otel = mock.MagicMock()
        mock_otel.shutdown.side_effect = RuntimeError("OTEL shutdown failed")
        facade._sentry_backend = mock_sentry
        facade._otel_backend = mock_otel

        # Should not raise, state should be properly cleaned up
        facade.shutdown()

        mock_sentry.shutdown.assert_called_once()
        mock_otel.shutdown.assert_called_once()
        assert facade.is_initialized is False
        assert facade._sentry_backend is None
        assert facade._otel_backend is None

    def test_shutdown_handles_both_errors_gracefully(self) -> None:
        """Test shutdown completes even if both backends fail."""
        facade = ObservabilityFacade()
        facade._initialized = True
        mock_sentry = mock.MagicMock()
        mock_otel = mock.MagicMock()
        mock_sentry.shutdown.side_effect = RuntimeError("Sentry shutdown failed")
        mock_otel.shutdown.side_effect = RuntimeError("OTEL shutdown failed")
        facade._sentry_backend = mock_sentry
        facade._otel_backend = mock_otel

        # Should not raise, state should be properly cleaned up
        facade.shutdown()

        mock_sentry.shutdown.assert_called_once()
        mock_otel.shutdown.assert_called_once()
        assert facade.is_initialized is False
        assert facade._sentry_backend is None
        assert facade._otel_backend is None


class TestAPIContract:
    """Tests validating the public API contract."""

    def test_facade_has_all_required_methods(self) -> None:
        """Test ObservabilityFacade exposes all required API methods."""
        facade = ObservabilityFacade()

        # Core methods
        assert callable(getattr(facade, "init", None))
        assert callable(getattr(facade, "capture_error", None))
        assert callable(getattr(facade, "capture_message", None))
        assert callable(getattr(facade, "record_metric", None))
        assert callable(getattr(facade, "start_span", None))
        assert callable(getattr(facade, "get_trace_context", None))

        # Context methods
        assert callable(getattr(facade, "set_user", None))
        assert callable(getattr(facade, "set_tag", None))
        assert callable(getattr(facade, "set_context", None))
        assert callable(getattr(facade, "record_event", None))

        # Lifecycle methods
        assert callable(getattr(facade, "flush", None))
        assert callable(getattr(facade, "shutdown", None))

        # Properties
        assert hasattr(facade, "is_initialized")

    def test_span_context_has_all_required_methods(self) -> None:
        """Test SpanContext exposes all required methods."""
        ctx = SpanContext("test")

        assert callable(getattr(ctx, "set_attribute", None))
        assert callable(getattr(ctx, "set_status", None))
        assert callable(getattr(ctx, "record_exception", None))
        assert callable(getattr(ctx, "add_event", None))
        assert callable(getattr(ctx, "__enter__", None))
        assert callable(getattr(ctx, "__exit__", None))

        # Convenience attribute helpers
        assert callable(getattr(ctx, "set_http_attributes", None))
        assert callable(getattr(ctx, "set_db_attributes", None))
        assert callable(getattr(ctx, "set_user_attributes", None))
        assert callable(getattr(ctx, "set_error_attributes", None))

    def test_module_exports_singleton(self) -> None:
        """Test the module exports a singleton observability instance."""
        from libs.observability import observability

        assert isinstance(observability, ObservabilityFacade)

    def test_module_exports_facade_class(self) -> None:
        """Test the module exports the ObservabilityFacade class."""
        from libs.observability import ObservabilityFacade as ExportedFacade

        assert ExportedFacade is ObservabilityFacade

    def test_init_accepts_required_parameters(self) -> None:
        """Test init() accepts the documented parameters."""
        facade = ObservabilityFacade()
        with mock.patch("libs.observability.config.load_config") as mock_load:
            from libs.observability.config import ObservabilityConfig, OTELConfig, SentryConfig

            mock_load.return_value = ObservabilityConfig(
                sentry=SentryConfig(enabled=False),
                otel=OTELConfig(enabled=False),
            )
            # Should not raise
            facade.init(
                config_path="config/test.yaml",
                service_name="test-service",
                environment="test",
            )

    def test_capture_error_accepts_required_parameters(self) -> None:
        """Test capture_error() accepts the documented parameters."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()
        facade._sentry_backend.capture_error.return_value = "event-id"

        # All parameters should be accepted without error
        facade.capture_error(
            ValueError("test"),
            context={"key": "value"},
            level="error",
            user={"id": "user-123"},
            tags={"tag": "value"},
            fingerprint=["custom", "fingerprint"],
        )

    def test_record_metric_accepts_required_parameters(self) -> None:
        """Test record_metric() accepts the documented parameters."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._otel_backend = mock.MagicMock()

        # All parameters should be accepted without error
        facade.record_metric(
            "test.metric",
            42.0,
            labels={"label": "value"},
            metric_type="histogram",
            unit="ms",
        )

    def test_start_span_accepts_required_parameters(self) -> None:
        """Test start_span() accepts the documented parameters."""
        facade = ObservabilityFacade()
        # Not initialized, but should accept parameters

        with facade.start_span(
            "test_span",
            kind="server",
            attributes={"key": "value"},
        ):
            pass  # Just testing parameter acceptance

    def test_set_user_accepts_required_parameters(self) -> None:
        """Test set_user() accepts the documented parameters."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        # All parameters should be accepted without error
        facade.set_user(
            "user-123",
            username="alice",
            email="alice@example.com",
        )

    def test_record_event_accepts_required_parameters(self) -> None:
        """Test record_event() accepts the documented parameters."""
        facade = ObservabilityFacade()
        facade._initialized = True
        facade._sentry_backend = mock.MagicMock()

        # All parameters should be accepted without error
        facade.record_event(
            "test.event",
            data={"key": "value"},
            level="info",
            tags={"tag": "value"},
        )


class TestTraceContextIntegration:
    """Tests for get_trace_context() method."""

    def test_get_trace_context_returns_none_when_not_initialized(self) -> None:
        """Test get_trace_context returns None values when not initialized."""
        facade = ObservabilityFacade()
        result = facade.get_trace_context()

        assert result == {"trace_id": None, "span_id": None}

    def test_get_trace_context_returns_none_when_no_active_span(self) -> None:
        """Test get_trace_context returns None when no active span."""
        facade = ObservabilityFacade()
        facade._initialized = True

        result = facade.get_trace_context()

        assert result == {"trace_id": None, "span_id": None}

    def test_get_trace_context_with_active_span(self) -> None:
        """Test get_trace_context returns trace and span IDs from active span."""
        import sys

        facade = ObservabilityFacade()
        facade._initialized = True

        # Create mock OTEL objects
        mock_span = mock.MagicMock()
        mock_span_context = mock.MagicMock()
        mock_span_context.trace_id = 0x12345678901234567890123456789012
        mock_span_context.span_id = 0x1234567890123456
        mock_span_context.is_valid = True
        mock_span.get_span_context.return_value = mock_span_context

        # Create mock trace module
        mock_trace_module = mock.MagicMock()
        mock_trace_module.get_current_span.return_value = mock_span

        # Create mock opentelemetry package
        mock_otel = mock.MagicMock()
        mock_otel.trace = mock_trace_module

        # Store original sys.modules state
        original_modules = sys.modules.copy()

        try:
            # Install mocks in sys.modules before the import happens
            sys.modules["opentelemetry"] = mock_otel
            sys.modules["opentelemetry.trace"] = mock_trace_module

            result = facade.get_trace_context()

            assert result["trace_id"] == "12345678901234567890123456789012"
            assert result["span_id"] == "1234567890123456"
        finally:
            # Restore original sys.modules
            sys.modules.clear()
            sys.modules.update(original_modules)

    def test_get_trace_context_handles_invalid_span_context(self) -> None:
        """Test get_trace_context returns None for invalid span context."""
        facade = ObservabilityFacade()
        facade._initialized = True

        # Mock the OTEL trace module with invalid span context
        mock_trace_module = mock.MagicMock()
        mock_span = mock.MagicMock()
        mock_span_context = mock.MagicMock()
        mock_span_context.is_valid = False
        mock_span.get_span_context.return_value = mock_span_context
        mock_trace_module.get_current_span.return_value = mock_span

        with mock.patch.dict("sys.modules", {"opentelemetry": mock.MagicMock(), "opentelemetry.trace": mock_trace_module}):
            result = facade.get_trace_context()

            assert result == {"trace_id": None, "span_id": None}

    def test_get_trace_context_handles_exception_gracefully(self) -> None:
        """Test get_trace_context handles exceptions gracefully."""
        facade = ObservabilityFacade()
        facade._initialized = True

        # Mock the OTEL trace module to raise an exception
        mock_trace_module = mock.MagicMock()
        mock_trace_module.get_current_span.side_effect = RuntimeError("OTEL not available")

        with mock.patch.dict("sys.modules", {"opentelemetry": mock.MagicMock(), "opentelemetry.trace": mock_trace_module}):
            result = facade.get_trace_context()

            assert result == {"trace_id": None, "span_id": None}
