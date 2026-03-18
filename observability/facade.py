"""Public API facade for observability.

This module provides the unified interface that application code uses.
It routes calls to the appropriate backend (Sentry, OpenTelemetry, or both)
based on configuration.

Example:
    Basic usage with error capture and metrics::

        from libs.observability import observability

        # Initialize at application startup
        observability.init(
            config_path="config/observability.yaml",
            service_name="my-service",
            environment="production",
        )

        # Capture an error
        try:
            risky_operation()
        except Exception as e:
            observability.capture_error(e, context={"operation": "risky"})
            raise

        # Record a metric
        observability.record_metric(
            "requests.processed",
            value=1,
            labels={"endpoint": "/api/users"},
            metric_type="counter",
        )

        # Use distributed tracing
        with observability.start_span("process_request") as span:
            span.set_attribute("user_id", "123")
            result = do_work()

Security Considerations:
    **Debug Logging and PII Exposure**

    This module uses Python's logging framework for internal diagnostics.
    When debug logging is enabled (e.g., ``logging.DEBUG`` level), sensitive
    information may be written to log files or stdout.

    **Risks:**

    - **PII in event data**: The ``context``, ``data``, and ``attributes``
      parameters passed to methods like ``capture_error()``, ``record_event()``,
      and ``set_attribute()`` may contain personally identifiable information
      (PII) such as user IDs, email addresses, IP addresses, or session tokens.
      This data can appear in debug logs when observability is not initialized
      or when tracing internal operations.

    - **Sensitive context exposure**: The ``set_context()`` and ``set_user()``
      methods accept arbitrary data that could include sensitive user details.
      Debug logs may reference this context when diagnosing issues.

    - **Exception details**: Error messages and stack traces captured via
      ``capture_error()`` may contain sensitive data embedded in exception
      messages or local variables.

    **Best Practices:**

    1. **Never enable DEBUG logging in production.** Use INFO or WARNING level
       in production environments to prevent accidental PII exposure::

           import logging
           logging.getLogger("libs.observability").setLevel(logging.INFO)

    2. **Sanitize data before passing to observability methods.** Remove or
       mask PII fields before including them in context or attributes::

           # Bad: exposes email in logs and Sentry
           observability.capture_error(e, context={"email": user.email})

           # Good: use anonymized identifiers
           observability.capture_error(e, context={"user_id": user.id})

    3. **Use structured identifiers instead of PII.** Prefer opaque user IDs
       over emails, usernames, or other identifying information::

           # Prefer this
           span.set_attribute("user_id", str(user.id))

           # Over this
           span.set_attribute("user_email", user.email)

    4. **Review log aggregation security.** Ensure log storage systems
       (CloudWatch, Datadog, etc.) have appropriate access controls and
       retention policies for data that may inadvertently contain PII.

    5. **Audit observability calls during code review.** Check that sensitive
       data is not being passed to ``context``, ``data``, ``attributes``,
       or ``tags`` parameters.
"""

from __future__ import annotations

import atexit
import logging
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Any, Iterator, Literal

from .validation import validate_json_serializable, validate_tag

if TYPE_CHECKING:
    from .config import ObservabilityConfig

logger = logging.getLogger(__name__)

MetricType = Literal["counter", "histogram", "gauge", "updown_counter"]
ErrorLevel = Literal["debug", "info", "warning", "error", "fatal"]


class SpanContext:
    """Context manager wrapper for tracing spans.

    Provides a unified interface for interacting with spans from both
    OTEL and Sentry backends. Automatically handles exception recording
    and status setting when exceptions occur within the span.

    Attributes:
        _name: The span name.
        _otel_span: The underlying OTEL span (if OTEL tracing is enabled).
        _sentry_span: The underlying Sentry span (if Sentry tracing is enabled).

    Example:
        Using SpanContext within start_span::

            with observability.start_span("operation") as span:
                span.set_attribute("key", "value")
                try:
                    result = do_work()
                except Exception:
                    span.set_status("error", "Operation failed")
                    raise
    """

    def __init__(self, name: str, otel_span: Any = None, sentry_span: Any = None):
        """Initialize SpanContext.

        Args:
            name: The span name.
            otel_span: Optional OTEL span object.
            sentry_span: Optional Sentry span object.
        """
        self._name = name
        self._otel_span = otel_span
        self._sentry_span = sentry_span

    def set_attribute(self, key: str, value: Any) -> None:
        """Set an attribute on the span.

        Attributes provide additional context about the operation being traced.
        They appear in both OTEL traces and Sentry transaction details.

        Args:
            key: Attribute key. Use lowercase with underscores (e.g., "user_id").
            value: Attribute value. Should be a primitive type (str, int, float, bool).

        Example:
            span.set_attribute("question_type", "math")
            span.set_attribute("difficulty", 3)
            span.set_attribute("is_adaptive", True)
        """
        if self._otel_span is not None:
            self._otel_span.set_attribute(key, value)
        if self._sentry_span is not None:
            self._sentry_span.set_data(key, value)

    def set_status(self, status: Literal["ok", "error"], description: str = "") -> None:
        """Set the span status.

        Call this to explicitly mark a span as successful or failed.
        If an exception is raised within the span's context, the status
        is automatically set to error.

        Args:
            status: Either "ok" for success or "error" for failure.
            description: Optional description, typically the error message.

        Example:
            span.set_status("ok")
            span.set_status("error", "Connection timeout after 30s")
        """
        if self._otel_span is not None:
            from opentelemetry.trace import StatusCode

            code = StatusCode.OK if status == "ok" else StatusCode.ERROR
            self._otel_span.set_status(code, description)

    def record_exception(self, exception: BaseException) -> None:
        """Record an exception on the span.

        This is called automatically when an exception is raised within
        the span's context. You can also call it manually to record
        exceptions that are caught and handled.

        Args:
            exception: The exception to record.

        Example:
            try:
                result = risky_operation()
            except ValueError as e:
                span.record_exception(e)
                result = fallback_value  # Handle gracefully
        """
        if self._otel_span is not None:
            self._otel_span.record_exception(exception)

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        """Add an event to the span.

        Events are time-stamped annotations that can include attributes.
        Use them to record significant occurrences during the span's lifetime.

        Note:
            Events are only recorded to the OTEL span. Sentry does not have an equivalent
            span event API, so add_event() calls are not forwarded to Sentry. If you need
            to record significant events for Sentry, use observability.capture_message()
            or observability.record_event() instead.

        Args:
            name: The event name (e.g., "cache_hit", "retry_attempt").
            attributes: Optional attributes to attach to the event.

        Example:
            with observability.start_span("process_request") as span:
                if cache.has(key):
                    span.add_event("cache_hit", {"key": key})
                else:
                    span.add_event("cache_miss", {"key": key})
                    result = compute(key)
        """
        if self._otel_span is not None:
            self._otel_span.add_event(name, attributes=attributes)

    def set_http_attributes(
        self,
        method: str,
        url: str,
        status_code: int | None = None,
        *,
        route: str | None = None,
        request_size: int | None = None,
        response_size: int | None = None,
    ) -> None:
        """Set HTTP-related span attributes following OpenTelemetry semantic conventions.

        Sets common HTTP attributes for tracking HTTP requests. Useful for
        instrumenting HTTP clients or when adding custom context to HTTP spans.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE, etc.).
            url: Full URL of the request.
            status_code: HTTP response status code (200, 404, 500, etc.).
                         Optional because the response may not be available yet.
            route: HTTP route template (e.g., "/users/{id}"). Optional.
            request_size: Request body size in bytes. Optional.
            response_size: Response body size in bytes. Optional.

        Example:
            with observability.start_span("http_request", kind="client") as span:
                response = await client.get(url)
                span.set_http_attributes(
                    method="GET",
                    url=url,
                    status_code=response.status_code,
                )

        Note:
            Attribute names follow OpenTelemetry semantic conventions:
            - http.request.method
            - url.full
            - http.response.status_code
            - http.route
            - http.request.body.size
            - http.response.body.size
        """
        self.set_attribute("http.request.method", method.upper())
        self.set_attribute("url.full", url)

        if status_code is not None:
            self.set_attribute("http.response.status_code", status_code)
        if route is not None:
            self.set_attribute("http.route", route)
        if request_size is not None:
            self.set_attribute("http.request.body.size", request_size)
        if response_size is not None:
            self.set_attribute("http.response.body.size", response_size)

    def set_db_attributes(
        self,
        operation: str,
        table: str | None = None,
        duration_ms: float | None = None,
        *,
        db_system: str | None = None,
        db_name: str | None = None,
        statement: str | None = None,
    ) -> None:
        """Set database-related span attributes following OpenTelemetry semantic conventions.

        Sets common database attributes for tracking database operations. Useful for
        instrumenting database queries or when adding custom context to DB spans.

        Args:
            operation: Database operation (SELECT, INSERT, UPDATE, DELETE, etc.).
            table: Database table name. Optional.
            duration_ms: Query duration in milliseconds. Optional.
            db_system: Database system identifier (e.g., "postgresql", "mysql").
                       Optional.
            db_name: Database name. Optional.
            statement: Database statement (query). Optional.

        Example:
            with observability.start_span("db_query") as span:
                start = time.perf_counter()
                result = await db.execute(query)
                elapsed_ms = (time.perf_counter() - start) * 1000
                span.set_db_attributes(
                    operation="SELECT",
                    table="users",
                    duration_ms=elapsed_ms,
                    db_system="postgresql",
                )

        Warning:
            The ``statement`` parameter should be sanitized before use in production.
            Never include raw user input, passwords, or sensitive data in statements.

            **Safe:**
            ``statement="SELECT * FROM users WHERE id = $1"``

            **Unsafe:**
            ``statement=f"SELECT * FROM users WHERE id = {user_input}"``

            If you must include dynamic values, use placeholders (``$1``, ``?``)
            and avoid interpolating sensitive data like passwords or PII.

        Note:
            Attribute names follow OpenTelemetry semantic conventions:
            - db.operation
            - db.sql.table
            - db.system
            - db.name
            - db.statement
            - db.query.duration (custom, not in semconv)
        """
        self.set_attribute("db.operation", operation.upper())

        if table is not None:
            self.set_attribute("db.sql.table", table)
        if duration_ms is not None:
            self.set_attribute("db.query.duration", duration_ms)
        if db_system is not None:
            self.set_attribute("db.system", db_system)
        if db_name is not None:
            self.set_attribute("db.name", db_name)
        if statement is not None:
            self.set_attribute("db.statement", statement)

    def set_user_attributes(
        self,
        user_id: str | None,
        username: str | None = None,
        *,
        role: str | None = None,
        scope: str | None = None,
    ) -> None:
        """Set user-related span attributes following OpenTelemetry semantic conventions.

        Sets user context attributes for tracking which user performed an operation.
        Useful for correlating spans with specific users during debugging.

        Args:
            user_id: Unique user identifier. Pass None for unauthenticated/anonymous
                     requests, or when user context is not yet available (e.g., early
                     in request processing before authentication).
            username: User display name or handle. Optional.
            role: User role (e.g., "admin", "member"). Optional.
            scope: User scope for access control context. Optional.

        Example:
            Authenticated request::

                with observability.start_span("process_request") as span:
                    if current_user:
                        span.set_user_attributes(
                            user_id=str(current_user.id),
                            username=current_user.username,
                            role=current_user.role,
                        )

            Anonymous/unauthenticated request::

                with observability.start_span("public_endpoint") as span:
                    span.set_user_attributes(user_id=None, username="anonymous")

        Note:
            Attribute names follow OpenTelemetry semantic conventions:
            - enduser.id
            - enduser.username (custom extension of semconv)
            - enduser.role
            - enduser.scope
        """
        if user_id is not None:
            self.set_attribute("enduser.id", user_id)
        if username is not None:
            self.set_attribute("enduser.username", username)
        if role is not None:
            self.set_attribute("enduser.role", role)
        if scope is not None:
            self.set_attribute("enduser.scope", scope)

    def set_error_attributes(
        self,
        exception: BaseException,
        *,
        escaped: bool = True,
    ) -> None:
        """Set error-related span attributes following OpenTelemetry semantic conventions.

        Sets exception attributes for detailed error tracking. This complements
        record_exception() by providing searchable attributes on the span itself.

        Args:
            exception: The exception to record attributes for.
            escaped: Whether the exception escaped the span's scope (was not caught).
                     Defaults to True.

        Example:
            with observability.start_span("process_request") as span:
                try:
                    result = risky_operation()
                except ValueError as e:
                    span.set_error_attributes(e, escaped=False)
                    span.set_status("error", str(e))
                    result = fallback_value

        Note:
            Attribute names follow OpenTelemetry semantic conventions:
            - exception.type
            - exception.message
            - exception.escaped

            For full exception recording with stacktrace, use record_exception()
            in addition to this method.
        """
        exc_type = type(exception).__qualname__
        exc_module = type(exception).__module__
        if exc_module and exc_module != "builtins":
            exc_type = f"{exc_module}.{exc_type}"

        self.set_attribute("exception.type", exc_type)
        self.set_attribute("exception.message", str(exception))
        self.set_attribute("exception.escaped", escaped)

    def __enter__(self) -> SpanContext:
        """Enter the span context."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the span context, recording any exception that occurred."""
        if exc_val is not None:
            self.record_exception(exc_val)
            self.set_status("error", str(exc_val))


class ObservabilityFacade:
    """Unified facade for observability operations.

    Provides a single API for error capture, metrics, and tracing that
    routes to the configured backend systems.
    """

    def __init__(self) -> None:
        self._initialized = False
        self._config: ObservabilityConfig | None = None
        self._sentry_backend: Any = None
        self._otel_backend: Any = None
        self._atexit_registered = False

    @property
    def is_initialized(self) -> bool:
        """Check if observability has been initialized."""
        return self._initialized

    def init(
        self,
        config_path: str | None = None,
        service_name: str | None = None,
        environment: str | None = None,
        **overrides: Any,
    ) -> bool:
        """Initialize observability backends.

        This must be called before using any other observability methods.
        Typically called once at application startup.

        This method is idempotent - calling it multiple times is safe.
        Subsequent calls will log a warning and return True without
        reinitializing backends.

        Args:
            config_path: Path to YAML configuration file. If not provided,
                uses default configuration from libs/observability/config/default.yaml.
            service_name: Override service name from config. Used to identify
                this service in metrics and traces.
            environment: Override environment from config (e.g., "production",
                "staging", "development").
            **overrides: Additional config overrides. Prefix with sentry_,
                otel_, or routing_ to target specific config sections.

        Returns:
            True if initialization succeeded or was already initialized.
            False if initialization failed due to configuration errors.

        Example:
            Initialize with YAML config::

                observability.init(
                    config_path="config/observability.yaml",
                    service_name="aiq-backend",
                    environment="production",
                )

            Initialize with overrides only::

                observability.init(
                    service_name="aiq-backend",
                    environment="development",
                    sentry_dsn="https://...",
                    otel_endpoint="http://localhost:4317",
                )
        """
        # Idempotency: if already initialized, warn and return early
        if self._initialized:
            logger.warning(
                "Observability already initialized. Skipping reinitialization. "
                "Call shutdown() first if you need to reconfigure."
            )
            return True

        try:
            from .config import ConfigurationError, load_config

            self._config = load_config(
                config_path=config_path,
                service_name=service_name,
                environment=environment,
                **overrides,
            )
        except ConfigurationError as e:
            logger.error("Failed to load observability configuration: %s", e)
            return False
        except Exception as e:
            logger.error("Unexpected error loading observability configuration: %s", e)
            return False

        # Track which backends were initialized
        sentry_initialized = False
        otel_initialized = False

        # Initialize Sentry backend if enabled
        if self._config.sentry.enabled:
            try:
                from .sentry_backend import SentryBackend

                self._sentry_backend = SentryBackend(self._config.sentry)
                sentry_initialized = self._sentry_backend.init()
            except Exception as e:
                logger.error("Failed to initialize Sentry backend: %s", e)
                # Continue - other backends may still work

        # Initialize OTEL backend if enabled
        if self._config.otel.enabled:
            try:
                from .otel_backend import OTELBackend

                self._otel_backend = OTELBackend(self._config.otel)
                otel_initialized = self._otel_backend.init()
            except Exception as e:
                logger.error("Failed to initialize OTEL backend: %s", e)
                # Continue - other backends may still work

        self._initialized = True

        # Register atexit handler for graceful shutdown
        if not self._atexit_registered:
            atexit.register(self._atexit_shutdown)
            self._atexit_registered = True

        # Log summary of what was initialized
        initialized_backends = []
        if sentry_initialized:
            initialized_backends.append("Sentry")
        if otel_initialized:
            initialized_backends.append("OpenTelemetry")

        if initialized_backends:
            logger.info(
                "Observability initialized: %s (service=%s, environment=%s)",
                ", ".join(initialized_backends),
                self._config.otel.service_name,
                self._config.sentry.environment,
            )
        else:
            logger.warning(
                "Observability initialized but no backends are active. "
                "Check your configuration."
            )

        return True

    def _atexit_shutdown(self) -> None:
        """Shutdown handler registered with atexit.

        Called automatically when the Python interpreter exits.
        Flushes pending data and shuts down backends gracefully.
        """
        if self._initialized:
            logger.debug("Observability atexit shutdown triggered")
            try:
                self.flush(timeout=2.0)
            except Exception as e:
                logger.debug("Error flushing during atexit: %s", e)
            try:
                self.shutdown()
            except Exception as e:
                logger.debug("Error during atexit shutdown: %s", e)

    def capture_error(
        self,
        exception: BaseException,
        *,
        context: dict[str, Any] | None = None,
        level: ErrorLevel = "error",
        user: dict[str, Any] | None = None,
        tags: dict[str, str] | None = None,
        fingerprint: list[str] | None = None,
    ) -> str | None:
        """Capture an error and send to error tracking backend (Sentry).

        Use this to report exceptions that should be tracked and alerted on.
        The error will be sent to Sentry with full context for debugging.

        Automatically enriches errors with:
        - Service metadata (name, version, environment) from configuration
        - OTEL trace context (trace_id, span_id) if a span is active

        Handles failures gracefully:
        - Logs warning if Sentry backend is disabled/unavailable
        - Logs error and returns None if capture fails

        Args:
            exception: The exception to capture. Can be any exception instance.
            context: Additional context data to attach. This appears as
                "additional" context in Sentry's UI. Merged with automatic
                service and trace context.
            level: Error severity level. One of "debug", "info", "warning",
                "error", or "fatal". Defaults to "error".
            user: User information dict with keys like "id", "email", "username".
                Helps identify affected users in Sentry.
            tags: Tags for categorization and filtering in Sentry.
                Use low-cardinality values (e.g., endpoint names, not user IDs).
            fingerprint: Custom grouping fingerprint. Override Sentry's automatic
                error grouping when needed.

        Note:
            The following context keys are reserved and auto-injected:
            - "service": dict with name, version, and environment from config
            - "trace": dict with trace_id and span_id from the active OTEL span
            User-provided context is merged with these reserved keys. If you
            provide "service" or "trace" keys, they will overwrite the auto-injected
            values.

        Returns:
            Event ID if captured, None if skipped (not initialized, backend
            disabled, or capture failed).

        Example:
            Basic error capture::

                try:
                    result = process_payment(order)
                except PaymentError as e:
                    observability.capture_error(e)
                    raise

            With full context::

                try:
                    question = generate_question(question_type)
                except GenerationError as e:
                    observability.capture_error(
                        e,
                        context={
                            "question_type": question_type,
                            "provider": "openai",
                            "attempt": attempt_number,
                        },
                        level="error",
                        tags={"domain": "question-generation"},
                        user={"id": str(user.id)},
                    )
                    raise
        """
        if not self._initialized:
            logger.debug("capture_error called but observability not initialized")
            return None

        if self._sentry_backend is None:
            logger.warning(
                "capture_error called but Sentry backend not available. "
                "Error will not be captured: %s",
                type(exception).__name__,
            )
            return None

        # Enrich context with facade-level metadata
        enriched_context = dict(context) if context else {}
        if self._config is not None:
            enriched_context["service"] = {
                "name": self._config.otel.service_name,
                "version": self._config.otel.service_version,
                "environment": self._config.sentry.environment,
            }

        # Attach OTEL trace context if a span is active (only when both values present)
        trace_ctx = self.get_trace_context()
        if trace_ctx.get("trace_id") and trace_ctx.get("span_id"):
            enriched_context["trace"] = trace_ctx

        try:
            return self._sentry_backend.capture_error(
                exception=exception,
                context=enriched_context,
                level=level,
                user=user,
                tags=tags,
                fingerprint=fingerprint,
            )
        except Exception as e:
            logger.error(
                "Failed to capture error to Sentry: %s. Original error: %s: %s",
                e,
                type(exception).__name__,
                str(exception),
            )
            return None

    def capture_message(
        self,
        message: str,
        *,
        level: ErrorLevel = "info",
        context: dict[str, Any] | None = None,
        tags: dict[str, str] | None = None,
    ) -> str | None:
        """Capture a message and send to error tracking backend.

        Use this for notable events that aren't exceptions but should still
        be tracked in Sentry (e.g., unexpected states, deprecation warnings).

        Args:
            message: The message to capture. Should be descriptive and
                include relevant context.
            level: Message severity level. One of "debug", "info", "warning",
                "error", or "fatal". Defaults to "info".
            context: Additional context data to attach.
            tags: Tags for categorization and filtering.

        Returns:
            Event ID if captured, None if skipped.

        Example:
            Track a deprecation warning::

                observability.capture_message(
                    "Legacy API endpoint called - schedule for removal",
                    level="warning",
                    context={"endpoint": "/v1/legacy/users"},
                    tags={"type": "deprecation"},
                )

            Track an unexpected but handled state::

                observability.capture_message(
                    f"User {user_id} has invalid subscription state",
                    level="warning",
                    context={"user_id": user_id, "state": subscription.state},
                )
        """
        if not self._initialized:
            logger.debug("capture_message called but observability not initialized")
            return None

        if self._sentry_backend is not None:
            return self._sentry_backend.capture_message(
                message=message,
                level=level,
                context=context,
                tags=tags,
            )
        return None

    def get_prometheus_registry(self) -> Any:
        """Return the Prometheus CollectorRegistry from the OTEL backend, or None."""
        if self._otel_backend is not None:
            return self._otel_backend.prometheus_registry
        return None

    def record_metric(
        self,
        name: str,
        value: float | int,
        *,
        labels: dict[str, str] | None = None,
        metric_type: MetricType = "counter",
        unit: str | None = None,
    ) -> None:
        """Record a metric value to OpenTelemetry/Prometheus.

        Metrics are routed to OTEL and exposed via Prometheus for Grafana
        dashboards. Use appropriate metric types for your use case.

        Args:
            name: Metric name using dot notation (e.g., "requests.processed",
                "questions.generated"). Follow Prometheus naming conventions.
            value: Metric value. For counters, this is the increment amount.
                For histograms, the observed value. For gauges, the current value.
            labels: Labels/dimensions for the metric. Use low-cardinality values
                only (e.g., endpoint names, question types - NOT user IDs).
            metric_type: Type of metric:
                - "counter": Monotonically increasing count (e.g., requests, errors)
                - "histogram": Distribution of values (e.g., latencies, sizes)
                - "gauge": Current point-in-time value (e.g., active connections)
                - "updown_counter": Can increase/decrease (e.g., queue_size, active_sessions)
            unit: Optional unit of measurement (e.g., "ms", "bytes", "1").
                Defaults to "1" for counters, "ms" for histograms.

        Example:
            Record a counter::

                observability.record_metric(
                    "questions.generated",
                    value=1,
                    labels={"question_type": "math", "difficulty": "hard"},
                    metric_type="counter",
                )

            Record a latency histogram::

                observability.record_metric(
                    "generation.duration",
                    value=elapsed_ms,
                    labels={"provider": "openai"},
                    metric_type="histogram",
                    unit="ms",
                )

            Record a gauge for current value::

                observability.record_metric(
                    "active_sessions",
                    value=len(active_sessions),
                    metric_type="gauge",
                )
        """
        if not self._initialized:
            logger.debug("record_metric called but observability not initialized: %s", name)
            return

        if self._otel_backend is None:
            logger.warning(
                "record_metric called but OTEL backend not available. "
                "Metric will not be recorded: %s",
                name,
            )
            return

        self._otel_backend.record_metric(
            name=name,
            value=value,
            labels=labels,
            metric_type=metric_type,
            unit=unit,
        )

    @contextmanager
    def start_span(
        self,
        name: str,
        *,
        kind: Literal["internal", "server", "client", "producer", "consumer"] = "internal",
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[SpanContext]:
        """Start a new tracing span for distributed tracing.

        Creates a span that tracks the duration and context of an operation.
        Spans are nested automatically based on the call stack and can be
        routed to OTEL, Sentry, or both based on configuration.

        Args:
            name: Span name. Should be descriptive and consistent for the
                same operation (e.g., "generate_question", "evaluate_response").
            kind: Span kind for categorization:
                - "internal": Default for internal operations
                - "server": For handling incoming requests
                - "client": For outgoing requests to other services
                - "producer": For producing messages
                - "consumer": For consuming messages
            attributes: Initial span attributes. Use for data known at span start.
                Additional attributes can be added via span.set_attribute().

        Yields:
            SpanContext for setting attributes, status, and recording exceptions.
            The span automatically ends when the context manager exits.

        Example:
            Basic span::

                with observability.start_span("process_request") as span:
                    result = do_work()

            With attributes and error handling::

                with observability.start_span(
                    "generate_question",
                    kind="internal",
                    attributes={"question_type": qtype},
                ) as span:
                    try:
                        question = generator.generate()
                        span.set_attribute("question_id", question.id)
                    except GenerationError:
                        span.set_status("error", "Generation failed")
                        raise

            Nested spans for pipeline stages::

                with observability.start_span("pipeline") as pipeline_span:
                    with observability.start_span("generate") as gen_span:
                        question = generate()
                        gen_span.set_attribute("provider", "openai")

                    with observability.start_span("evaluate") as eval_span:
                        result = evaluate(question)
                        eval_span.set_attribute("score", result.score)
        """
        if not self._initialized:
            logger.debug("start_span called but observability not initialized: %s", name)
            yield SpanContext(name)
            return

        routing = self._config.routing.traces if self._config else "otel"

        with ExitStack() as stack:
            otel_span = None
            sentry_span = None

            # Start OTEL span if configured
            if routing in ("otel", "both") and self._otel_backend is not None:
                otel_context = self._otel_backend.start_span(
                    name, kind=kind, attributes=attributes
                )
                otel_span = stack.enter_context(otel_context)

            # Start Sentry span if configured
            if routing in ("sentry", "both") and self._sentry_backend is not None:
                sentry_context = self._sentry_backend.start_span(name, attributes=attributes)
                if sentry_context is not None:
                    sentry_span = stack.enter_context(sentry_context)

            yield SpanContext(name, otel_span=otel_span, sentry_span=sentry_span)

    def set_user(
        self,
        user_id: str | None,
        *,
        username: str | None = None,
        email: str | None = None,
        **extra: Any,
    ) -> None:
        """Set the current user context for error tracking.

        Associates subsequent errors with this user in Sentry. Call this
        after user authentication to enable user-based error grouping and
        impact analysis.

        Args:
            user_id: User identifier. Pass None to clear user context.
            username: Optional display name.
            email: Optional email address for notifications.
            **extra: Additional user data to include.

        Example:
            Set user after authentication::

                @app.middleware
                async def auth_middleware(request, call_next):
                    user = await authenticate(request)
                    if user:
                        observability.set_user(
                            str(user.id),
                            username=user.name,
                            email=user.email,
                        )
                    response = await call_next(request)
                    return response

            Clear user context on logout::

                observability.set_user(None)
        """
        if not self._initialized:
            logger.debug("set_user called but observability not initialized")
            return

        if self._sentry_backend is not None:
            user_data: dict[str, Any] = {}
            if username:
                user_data["username"] = username
            if email:
                user_data["email"] = email
            user_data.update(extra)
            self._sentry_backend.set_user(user_id, **user_data)

    def set_tag(self, key: str, value: str) -> None:
        """Set a tag on the current scope for categorization.

        Tags are indexed and searchable in Sentry. Use for low-cardinality
        data that's useful for filtering errors (e.g., service version,
        environment, feature flags).

        Args:
            key: Tag key. Use lowercase with underscores.
            value: Tag value. Must be a string with low cardinality.

        Raises:
            ValueError: If key or value fail validation (non-string, too long).

        Example:
            Set version and environment tags::

                observability.set_tag("api_version", "v2")
                observability.set_tag("feature_flag", "new_scoring")
        """
        # Validate tag before checking initialization to ensure
        # validation errors are raised even when not initialized
        validate_tag(key, value)

        if not self._initialized:
            logger.debug("set_tag called but observability not initialized")
            return

        if self._sentry_backend is not None:
            self._sentry_backend.set_tag(key, value)

    def set_context(self, name: str, context: dict[str, Any]) -> None:
        """Set a context block on the current scope.

        Context blocks provide structured additional data that appears in
        Sentry error reports. Unlike tags, context values can be any type
        and are not indexed/searchable.

        Args:
            name: Context block name (e.g., "request", "response", "user_preferences").
            context: Dictionary of context data. Can contain any JSON-serializable values.

        Example:
            Add request context::

                observability.set_context("request", {
                    "url": request.url,
                    "method": request.method,
                    "headers": dict(request.headers),
                })

            Add application-specific context::

                observability.set_context("question_generation", {
                    "batch_size": 10,
                    "question_types": ["math", "verbal"],
                    "target_difficulty": "medium",
                })
        """
        if not self._initialized:
            logger.debug("set_context called but observability not initialized")
            return

        if self._sentry_backend is not None:
            self._sentry_backend.set_context(name, context)

    def get_trace_context(self) -> dict[str, str | None]:
        """Get the current trace context for correlation.

        Returns a dict with trace_id and span_id from the current OTEL span,
        or None values if no active span or OTEL is not initialized.

        This is useful for including trace context in Sentry errors to enable
        navigation from Sentry to Grafana traces.

        Returns:
            Dict with 'trace_id' and 'span_id' keys. Values are hex-formatted
            strings (32 chars for trace_id, 16 chars for span_id) or None
            if no active trace.

        Example:
            Include trace context in Sentry errors::

                trace_ctx = observability.get_trace_context()
                observability.capture_error(
                    exception,
                    context={"trace": trace_ctx},
                )
        """
        result: dict[str, str | None] = {"trace_id": None, "span_id": None}

        if not self._initialized:
            return result

        try:
            from opentelemetry import trace

            span = trace.get_current_span()
            span_context = span.get_span_context()
            if span_context.is_valid:
                result["trace_id"] = format(span_context.trace_id, "032x")
                result["span_id"] = format(span_context.span_id, "016x")
        except ImportError:
            # OTEL not installed, this is expected in some deployments
            pass
        except Exception as e:
            logger.debug("Could not get trace context: %s", e)

        return result

    def record_event(
        self,
        name: str,
        data: dict[str, Any] | None = None,
        *,
        level: ErrorLevel = "info",
        tags: dict[str, str] | None = None,
    ) -> str | None:
        """Record a structured event for tracking.

        Use this to track significant application events that aren't errors
        but are worth recording for analytics and debugging. Events are
        sent to Sentry and can be viewed in the Issues stream.

        This is similar to capture_message but emphasizes structured data
        and is intended for application events rather than warnings.

        Args:
            name: Event name. Use a consistent naming scheme (e.g.,
                "user.signup", "test.completed", "generation.batch_finished").
            data: Structured event data. Can contain any JSON-serializable values.
            level: Event severity level. Defaults to "info".
            tags: Tags for categorization and filtering.

        Returns:
            Event ID if recorded, None if skipped.

        Raises:
            ValueError: If data is not JSON-serializable.

        Example:
            Track user signup::

                observability.record_event(
                    "user.signup",
                    data={
                        "user_id": str(user.id),
                        "signup_method": "oauth",
                        "provider": "google",
                    },
                    tags={"signup_source": "web"},
                )

            Track batch completion::

                observability.record_event(
                    "generation.batch_completed",
                    data={
                        "batch_id": batch_id,
                        "questions_generated": 50,
                        "questions_passed": 45,
                        "duration_seconds": elapsed,
                    },
                    tags={"question_type": "math"},
                )

            Track feature usage::

                observability.record_event(
                    "feature.used",
                    data={"feature": "adaptive_testing", "user_id": str(user.id)},
                    level="info",
                )
        """
        # Validate data is JSON-serializable before checking initialization
        # to ensure validation errors are raised even when not initialized
        if data is not None:
            validate_json_serializable(data)

        if not self._initialized:
            logger.debug("record_event called but observability not initialized: %s", name)
            return None

        # Build message with event name
        message = f"Event: {name}"

        if self._sentry_backend is not None:
            return self._sentry_backend.capture_message(
                message=message,
                level=level,
                context=data,
                tags=tags,
            )
        return None

    def flush(self, timeout: float = 2.0) -> None:
        """Flush pending events to backends.

        Ensures all buffered data is sent to Sentry and OTEL. Call this
        before application shutdown to prevent data loss.

        Args:
            timeout: Maximum time to wait for flush in seconds. Defaults to 2.0.

        Example:
            Flush on application shutdown::

                @app.on_event("shutdown")
                async def shutdown():
                    observability.flush(timeout=5.0)
                    observability.shutdown()
        """
        if not self._initialized:
            logger.debug("flush called but observability not initialized")
            return

        # Wrap each backend flush in try-except to ensure both are attempted
        # even if one fails. This provides proper backend isolation during flush.
        if self._sentry_backend is not None:
            try:
                self._sentry_backend.flush(timeout)
            except Exception as e:
                logger.warning("Sentry backend flush failed: %s", e)

        if self._otel_backend is not None:
            try:
                self._otel_backend.flush(timeout)
            except Exception as e:
                logger.warning("OTEL backend flush failed: %s", e)

    def shutdown(self) -> None:
        """Shutdown observability backends gracefully.

        Closes connections to Sentry and OTEL backends. Call this during
        application shutdown, after flush().

        This method is idempotent - calling it multiple times is safe.
        Subsequent calls will return immediately without error.

        Example:
            Clean shutdown sequence::

                observability.flush(timeout=5.0)
                observability.shutdown()
        """
        if not self._initialized:
            logger.debug("shutdown called but observability not initialized")
            return

        logger.info("Shutting down observability backends")

        # Wrap each backend shutdown in try-except to ensure both are attempted
        # even if one fails. This provides proper backend isolation during shutdown.
        if self._sentry_backend is not None:
            try:
                self._sentry_backend.shutdown()
            except Exception as e:
                logger.warning("Sentry backend shutdown failed: %s", e)
            finally:
                self._sentry_backend = None

        if self._otel_backend is not None:
            try:
                self._otel_backend.shutdown()
            except Exception as e:
                logger.warning("OTEL backend shutdown failed: %s", e)
            finally:
                self._otel_backend = None

        self._initialized = False
        self._config = None
        logger.debug("Observability shutdown complete")
