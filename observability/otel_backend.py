"""OpenTelemetry backend for metrics, tracing, and logging.

This module handles all OpenTelemetry SDK interactions including
metrics recording, distributed tracing, and log forwarding.

The init() function supports multiple exporters (console, OTLP) and
handles authentication for services like Grafana Cloud via OTLP headers.

Requires opentelemetry-api>=1.20.0 and opentelemetry-sdk>=1.20.0.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from urllib.parse import unquote
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator, Literal

if TYPE_CHECKING:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.trace import Span

    from .config import OTELConfig

logger = logging.getLogger(__name__)

# Default service version when none is configured
_DEFAULT_SERVICE_VERSION = "0.0.0-unknown"


def _parse_otlp_headers(headers_str: str) -> dict[str, str]:
    """Parse OTLP headers string into a dictionary.

    Format: "key1=value1,key2=value2"
    Returns: {"key1": "value1", "key2": "value2"}

    Values are URL-decoded per the OTEL spec (e.g., %20 → space).

    Note: Empty keys or values are skipped to avoid malformed headers.
    Headers containing control characters (newlines, carriage returns, etc.)
    are rejected to prevent header injection attacks.

    Args:
        headers_str: Comma-separated key=value pairs, optionally URL-encoded.

    Returns:
        Dictionary of header key-value pairs.
    """
    if not headers_str:
        return {}

    # Control characters that could enable header injection
    CONTROL_CHARS = frozenset("\r\n\x00\x0b\x0c")

    headers: dict[str, str] = {}
    for pair in headers_str.split(","):
        if "=" in pair:
            key, value = pair.split("=", 1)
            key = unquote(key.strip())
            value = unquote(value.strip())
            if key and value:
                # Reject headers with control characters to prevent injection
                if any(c in key for c in CONTROL_CHARS) or any(
                    c in value for c in CONTROL_CHARS
                ):
                    logger.warning(
                        f"Skipping header with control characters: {key!r}"
                    )
                    continue
                headers[key] = value
    return headers


# Valid metric name pattern: lowercase alphanumeric, underscores, and dots
# Must start with a letter, dots for hierarchy (e.g., "http.server.requests")
_METRIC_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.]*$")

# Known high-cardinality label patterns to warn about
_HIGH_CARDINALITY_PATTERNS = [
    re.compile(r"^(user_?id|userid|uid)$", re.IGNORECASE),
    re.compile(r"^(request_?id|req_?id|trace_?id|span_?id)$", re.IGNORECASE),
    re.compile(r"^(session_?id|sid)$", re.IGNORECASE),
    re.compile(r"^(timestamp|time|date)$", re.IGNORECASE),
    re.compile(r"^(uuid|guid|id)$", re.IGNORECASE),
    re.compile(r"^(email|ip|ip_?address)$", re.IGNORECASE),
]


def _validate_metric_name(name: str) -> tuple[bool, str | None]:
    """Validate a metric name follows conventions.

    Metric names should:
    - Start with a lowercase letter
    - Contain only lowercase letters, numbers, underscores, and dots
    - Use dots for hierarchy (e.g., "http.server.requests")
    - Not contain spaces

    Args:
        name: The metric name to validate.

    Returns:
        Tuple of (is_valid, error_message). If valid, error_message is None.
    """
    if not name:
        return False, "Metric name cannot be empty"

    if " " in name:
        return False, f"Metric name '{name}' contains spaces"

    if not _METRIC_NAME_PATTERN.match(name):
        return False, (
            f"Metric name '{name}' does not follow conventions. "
            "Names must match ^[a-z][a-z0-9_.]*$ (e.g., 'http.server.requests'). "
            "Use lowercase letters, numbers, underscores, and dots. "
            "Must start with a letter."
        )

    return True, None


def _check_label_cardinality(labels: dict[str, str] | None, metric_name: str) -> None:
    """Check for high-cardinality labels and warn if found.

    High-cardinality labels (like user IDs, timestamps, request IDs) can cause
    metric explosion in Prometheus and should be avoided.

    Args:
        labels: The labels to check.
        metric_name: The metric name for logging context.
    """
    if not labels:
        return

    for label_key in labels:
        for pattern in _HIGH_CARDINALITY_PATTERNS:
            if pattern.match(label_key):
                logger.warning(
                    f"High-cardinality label '{label_key}' detected in metric '{metric_name}'. "
                    "Consider using a lower-cardinality alternative to avoid metric explosion."
                )
                break


class OTELBackend:
    """Backend for OpenTelemetry metrics, tracing, and logging.

    This backend provides a unified interface for recording metrics,
    creating distributed traces, and forwarding logs via OpenTelemetry.

    Supports multiple exporters:
    - "console": Outputs to stdout for local development
    - "otlp": Sends to an OTLP-compatible endpoint (e.g., Grafana Cloud)
    - "none": Disables export (useful for testing)

    Example:
        Basic usage with OTLP export::

            from libs.observability.config import OTELConfig
            from libs.observability.otel_backend import OTELBackend

            config = OTELConfig(
                service_name="my-service",
                endpoint="https://otlp-gateway.grafana.net:443",
                otlp_headers="Authorization=Basic xxx",
                traces_sample_rate=0.1,
            )
            backend = OTELBackend(config)
            backend.init()

            # Record metrics
            backend.record_metric("requests.total", 1, metric_type="counter")

            # Create spans
            with backend.start_span("process_request") as span:
                span.set_attribute("user_id", "123")
                # do work

            # Cleanup
            backend.shutdown()
    """

    def __init__(self, config: OTELConfig) -> None:
        """Initialize the OTEL backend.

        Args:
            config: OpenTelemetry configuration.
        """
        self._config = config
        self._initialized = False
        self._meter: Any = None
        self._tracer: Any = None
        self._counters: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}
        self._gauges: dict[str, Any] = {}
        self._updown_counters: dict[str, Any] = {}
        self._gauge_values: dict[str, dict[str, float]] = {}  # Track gauge values for callbacks
        self._gauge_lock = threading.Lock()  # Protects _gauge_values access
        self._meter_provider: MeterProvider | None = None
        self._tracer_provider: TracerProvider | None = None
        self._logger_provider: LoggerProvider | None = None
        self._prometheus_registry: Any = None

    @property
    def prometheus_registry(self) -> Any:
        """Return the Prometheus CollectorRegistry used by the metric reader, or None."""
        return self._prometheus_registry

    def init(self) -> bool:
        """Initialize OpenTelemetry metrics, tracing, and logging.

        Initializes TracerProvider, MeterProvider, and optionally LoggerProvider
        based on configuration. Supports console and OTLP exporters.

        Returns:
            True if initialization succeeded, False if skipped or failed.

        Note:
            Does not raise exceptions. Failures are logged and return False.
            Calling init() multiple times logs a warning and returns True
            if already initialized.

            Exception handling strategy:
            - ImportError: OTEL packages not installed (warning, graceful fallback)
            - General Exception: Unexpected error (error with traceback)
            - Never raises; always returns False on failure
        """
        if not self._config.enabled:
            logger.debug("OpenTelemetry initialization skipped (disabled)")
            return False

        if self._config.exporter == "none":
            logger.debug("OpenTelemetry initialization skipped (exporter='none')")
            return False

        if self._initialized:
            logger.warning("OpenTelemetry already initialized, skipping")
            return True

        try:
            # Create shared resource with service name and optional version
            resource = self._create_resource()

            # Parse OTLP headers for authentication (e.g., Grafana Cloud)
            otlp_headers = _parse_otlp_headers(self._config.otlp_headers)

            # Initialize tracing first (other components may reference it)
            if self._config.traces_enabled:
                if not self._init_tracing(resource, otlp_headers):
                    return False

            # Initialize metrics
            if self._config.metrics_enabled:
                self._init_metrics(resource, otlp_headers)

            # Initialize logs
            if self._config.logs_enabled:
                self._init_logs(resource, otlp_headers)

            self._initialized = True

            logger.info(
                f"OpenTelemetry initialized with {self._config.exporter} exporter "
                f"(service={self._config.service_name}, "
                f"traces={'enabled' if self._config.traces_enabled else 'disabled'}, "
                f"metrics={'enabled' if self._config.metrics_enabled else 'disabled'}, "
                f"logs={'enabled' if self._config.logs_enabled else 'disabled'})"
            )
            return True

        except ImportError as e:
            logger.warning(
                f"OpenTelemetry packages not installed: {e}. "
                "Install with: pip install opentelemetry-api opentelemetry-sdk "
                "opentelemetry-exporter-otlp"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to initialize OpenTelemetry: {e}", exc_info=True)
            return False

    def _create_resource(self) -> Resource:
        """Create OpenTelemetry Resource with service attributes.

        Returns:
            Resource with service.name and optionally service.version.
        """
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION

        attributes: dict[str, str] = {SERVICE_NAME: self._config.service_name}
        if self._config.service_version:
            attributes[SERVICE_VERSION] = self._config.service_version

        return Resource(attributes=attributes)

    def _init_tracing(self, resource: Resource, otlp_headers: dict[str, str]) -> bool:
        """Initialize OpenTelemetry tracing.

        Args:
            resource: OpenTelemetry Resource with service attributes.
            otlp_headers: Headers for OTLP exporter authentication.

        Returns:
            True if initialization succeeded, False if failed.
        """
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

        # Create sampler based on config
        sampler = TraceIdRatioBased(self._config.traces_sample_rate)
        tracer_provider = TracerProvider(resource=resource, sampler=sampler)

        # Add Sentry span processor if available (for OTEL->Sentry trace correlation)
        try:
            from sentry_sdk.integrations.opentelemetry import SentrySpanProcessor

            tracer_provider.add_span_processor(SentrySpanProcessor())
            logger.info("Sentry span processor added for OTEL trace correlation")
        except ImportError:
            logger.debug("Sentry OpenTelemetry integration not available")
        except Exception as e:
            logger.debug(f"Could not add Sentry span processor: {e}")

        # Configure exporter based on config
        if self._config.exporter == "console":
            exporter = ConsoleSpanExporter()
            tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
        elif self._config.exporter == "otlp":
            if not self._config.endpoint:
                logger.warning(
                    "OTLP exporter configured but no endpoint set. "
                    "Traces will not be exported."
                )
            else:
                try:
                    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                        OTLPSpanExporter,
                    )

                    endpoint = self._config.endpoint.rstrip("/")
                    exporter = OTLPSpanExporter(
                        endpoint=f"{endpoint}/v1/traces",
                        headers=otlp_headers if otlp_headers else None,
                    )
                    tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
                except ImportError:
                    tracer_provider.shutdown()
                    logger.warning(
                        "OTLP exporter not available. "
                        "Install with: pip install opentelemetry-exporter-otlp"
                    )
                    return False

        trace.set_tracer_provider(tracer_provider)
        self._tracer_provider = tracer_provider

        version = self._config.service_version or _DEFAULT_SERVICE_VERSION
        self._tracer = trace.get_tracer(
            self._config.service_name,
            instrumenting_library_version=version,
        )

        logger.info(
            f"OpenTelemetry tracing initialized with {self._config.exporter} exporter "
            f"(sample_rate={self._config.traces_sample_rate * 100:.0f}%)"
        )
        return True

    def _init_metrics(self, resource: Resource, otlp_headers: dict[str, str]) -> None:
        """Initialize OpenTelemetry metrics.

        Args:
            resource: OpenTelemetry Resource with service attributes.
            otlp_headers: Headers for OTLP exporter authentication.
        """
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import (
            PeriodicExportingMetricReader,
            ConsoleMetricExporter,
        )

        readers: list[Any] = []

        if self._config.exporter == "console":
            exporter = ConsoleMetricExporter()
            reader = PeriodicExportingMetricReader(
                exporter,
                export_interval_millis=self._config.metrics_export_interval_millis,
            )
            readers.append(reader)
        elif self._config.exporter == "otlp":
            if self._config.endpoint:
                try:
                    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                        OTLPMetricExporter,
                    )

                    endpoint = self._config.endpoint.rstrip("/")
                    exporter = OTLPMetricExporter(
                        endpoint=f"{endpoint}/v1/metrics",
                        headers=otlp_headers if otlp_headers else None,
                    )
                    reader = PeriodicExportingMetricReader(
                        exporter,
                        export_interval_millis=self._config.metrics_export_interval_millis,
                    )
                    readers.append(reader)
                except ImportError:
                    logger.warning(
                        "OTLP metric exporter not available. "
                        "Install with: pip install opentelemetry-exporter-otlp"
                    )

        # Add Prometheus reader for scraping if enabled
        if self._config.prometheus_enabled:
            try:
                from opentelemetry.exporter.prometheus import PrometheusMetricReader
                from prometheus_client import REGISTRY

                readers.append(PrometheusMetricReader())
                self._prometheus_registry = REGISTRY
            except ImportError:
                logger.debug("Prometheus metric reader not available")

        if readers:
            meter_provider = MeterProvider(resource=resource, metric_readers=readers)
            metrics.set_meter_provider(meter_provider)
            self._meter_provider = meter_provider

        version = self._config.service_version or _DEFAULT_SERVICE_VERSION
        self._meter = metrics.get_meter(self._config.service_name, version=version)

        logger.info(
            f"OpenTelemetry metrics initialized with {self._config.exporter} exporter "
            f"(export_interval={self._config.metrics_export_interval_millis}ms)"
        )

    def _init_logs(self, resource: Resource, otlp_headers: dict[str, str]) -> None:
        """Initialize OpenTelemetry logging.

        Sets up LoggerProvider and attaches a handler to the Python root logger
        to forward logs to the configured exporter.

        Warning:
            This method attaches a LoggingHandler to Python's root logger at NOTSET level.
            This means ALL log messages (DEBUG and above) from every logger in the process
            will be forwarded to the OTEL exporter. This is a global side effect that persists
            for the lifetime of the process. The handler is not removed on shutdown().
            To limit log forwarding, configure log levels on individual loggers rather than
            relying on the root logger level.

        Args:
            resource: OpenTelemetry Resource with service attributes.
            otlp_headers: Headers for OTLP exporter authentication.
        """
        try:
            from opentelemetry._logs import set_logger_provider
            from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        except ImportError:
            logger.warning(
                "OpenTelemetry logging packages not installed. Skipping logs setup."
            )
            return

        logger_provider = LoggerProvider(resource=resource)

        if self._config.exporter == "otlp" and self._config.endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http._log_exporter import (
                    OTLPLogExporter,
                )

                endpoint = self._config.endpoint.rstrip("/")
                exporter = OTLPLogExporter(
                    endpoint=f"{endpoint}/v1/logs",
                    headers=otlp_headers if otlp_headers else None,
                )
                processor = BatchLogRecordProcessor(exporter)
                logger_provider.add_log_record_processor(processor)
            except ImportError:
                logger.warning(
                    "OTLP log exporter not available. "
                    "Install with: pip install opentelemetry-exporter-otlp"
                )
                return

        set_logger_provider(logger_provider)
        self._logger_provider = logger_provider

        # Attach OpenTelemetry handler to root logger to forward all logs
        handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
        logging.getLogger().addHandler(handler)

        logger.info(
            f"OpenTelemetry logging initialized with {self._config.exporter} exporter"
        )

    def record_metric(
        self,
        name: str,
        value: float | int,
        *,
        labels: dict[str, str] | None = None,
        metric_type: Literal["counter", "histogram", "gauge", "updown_counter"] = "counter",
        unit: str | None = None,
    ) -> None:
        """Record a metric value.

        Args:
            name: Metric name (e.g., "requests.total").
            value: Metric value to record.
            labels: Labels/dimensions for the metric.
            metric_type: Type of metric:
                - "counter": Monotonically increasing (e.g., requests_total)
                - "histogram": Distribution of values (e.g., request_duration)
                - "gauge": Current value via observable callback (e.g., temperature)
                - "updown_counter": Can increase/decrease (e.g., queue_size)
            unit: Unit of measurement (defaults to "1" for counters, "ms" for histograms).
        """
        if not self._initialized or self._meter is None:
            return

        # Validate metric name
        is_valid, error_msg = _validate_metric_name(name)
        if not is_valid:
            logger.warning(error_msg)
            # Continue anyway for graceful degradation

        # Check for high-cardinality labels
        _check_label_cardinality(labels, name)

        attributes = labels or {}

        if metric_type == "counter":
            if name not in self._counters:
                self._counters[name] = self._meter.create_counter(
                    name=name,
                    unit=unit or "1",
                    description=f"Counter for {name}",
                )
            self._counters[name].add(value, attributes=attributes)

        elif metric_type == "histogram":
            if name not in self._histograms:
                self._histograms[name] = self._meter.create_histogram(
                    name=name,
                    unit=unit or "ms",
                    description=f"Histogram for {name}",
                )
            self._histograms[name].record(value, attributes=attributes)

        elif metric_type == "gauge":
            # OTEL gauges use ObservableGauge with callbacks for true gauge semantics.
            # We store the current value and let the callback report it during scrape.
            # Use JSON serialization for safety (no eval).
            # Thread safety: _gauge_lock protects all _gauge_values reads/writes since
            # the callback may be invoked from a different thread during metric export.
            attr_key = json.dumps(sorted(attributes.items())) if attributes else ""
            with self._gauge_lock:
                if name not in self._gauge_values:
                    self._gauge_values[name] = {}

                # Store the absolute value (gauge semantics)
                self._gauge_values[name][attr_key] = float(value)

            # Create the observable gauge with callback if not exists
            if name not in self._gauges:

                def make_callback(metric_name: str, lock: threading.Lock) -> Any:
                    def callback(options: Any) -> Any:
                        from opentelemetry.metrics import Observation

                        observations = []
                        with lock:
                            # Copy to avoid holding lock during Observation creation
                            gauge_data = dict(self._gauge_values.get(metric_name, {}))
                        for attr_str, val in gauge_data.items():
                            # Reconstruct attributes from JSON key
                            attrs = dict(json.loads(attr_str)) if attr_str else {}
                            observations.append(Observation(val, attrs))
                        return observations

                    return callback

                self._gauges[name] = self._meter.create_observable_gauge(
                    name=name,
                    callbacks=[make_callback(name, self._gauge_lock)],
                    unit=unit or "1",
                    description=f"Gauge for {name}",
                )

        elif metric_type == "updown_counter":
            if name not in self._updown_counters:
                self._updown_counters[name] = self._meter.create_up_down_counter(
                    name=name,
                    unit=unit or "1",
                    description=f"UpDownCounter for {name}",
                )
            self._updown_counters[name].add(value, attributes=attributes)

    @contextmanager
    def start_span(
        self,
        name: str,
        *,
        kind: Literal["internal", "server", "client", "producer", "consumer"] = "internal",
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[Span]:
        """Start a new tracing span.

        Args:
            name: Span name.
            kind: Span kind (internal, server, client, producer, consumer).
            attributes: Initial span attributes.

        Yields:
            OTEL Span object, or None if tracing is disabled.
        """
        if not self._initialized or self._tracer is None:
            # Return a no-op context manager
            from contextlib import nullcontext

            with nullcontext() as _:
                yield None  # type: ignore[misc]
            return

        from opentelemetry.trace import SpanKind

        kind_map = {
            "internal": SpanKind.INTERNAL,
            "server": SpanKind.SERVER,
            "client": SpanKind.CLIENT,
            "producer": SpanKind.PRODUCER,
            "consumer": SpanKind.CONSUMER,
        }

        with self._tracer.start_as_current_span(
            name,
            kind=kind_map.get(kind, SpanKind.INTERNAL),
            attributes=attributes,
        ) as span:
            yield span

    def flush(self, timeout: float = 2.0) -> None:
        """Flush pending metrics, traces, and logs.

        Args:
            timeout: Maximum time to wait for flush in seconds.
        """
        if not self._initialized:
            return

        timeout_millis = int(timeout * 1000)

        if self._meter_provider is not None:
            try:
                self._meter_provider.force_flush(timeout_millis=timeout_millis)
            except Exception as e:
                logger.warning("Failed to flush meter provider: %s", e)

        if self._tracer_provider is not None:
            try:
                self._tracer_provider.force_flush(timeout_millis=timeout_millis)
            except Exception as e:
                logger.warning("Failed to flush tracer provider: %s", e)

        if self._logger_provider is not None:
            try:
                self._logger_provider.force_flush(timeout_millis=timeout_millis)
            except Exception as e:
                logger.warning("Failed to flush logger provider: %s", e)

    def shutdown(self) -> None:
        """Shutdown OpenTelemetry providers gracefully.

        Shuts down all initialized providers (tracer, meter, logger) and
        releases resources. Should be called on application exit.
        """
        if not self._initialized:
            return

        # Shutdown tracer provider
        if self._tracer_provider is not None:
            try:
                self._tracer_provider.shutdown()
                logger.info("OpenTelemetry tracing shutdown complete")
            except Exception as e:
                logger.warning("Failed to shutdown tracer provider: %s", e)
            finally:
                self._tracer_provider = None

        # Shutdown meter provider
        if self._meter_provider is not None:
            try:
                self._meter_provider.shutdown()
                logger.info("OpenTelemetry metrics shutdown complete")
            except Exception as e:
                logger.warning("Failed to shutdown meter provider: %s", e)
            finally:
                self._meter_provider = None

        # Shutdown logger provider
        if self._logger_provider is not None:
            try:
                self._logger_provider.shutdown()
                logger.info("OpenTelemetry logging shutdown complete")
            except Exception as e:
                logger.warning("Failed to shutdown logger provider: %s", e)
            finally:
                self._logger_provider = None

        self._initialized = False
