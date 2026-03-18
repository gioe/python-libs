# Unified Observability Library

A unified observability abstraction for AIQ services that routes errors, metrics, and traces to the appropriate backend systems. This library provides a single API for all observability needs while hiding the complexity of individual SDK integrations.

## Table of Contents

- [Overview and Architecture](#overview-and-architecture)
- [Installation and Setup](#installation-and-setup)
- [API Reference](#api-reference)
- [Configuration Guide](#configuration-guide)
- [Backend-Specific Notes](#backend-specific-notes)
- [Troubleshooting](#troubleshooting)
- [Migration Guide](#migration-guide)

---

## Overview and Architecture

The observability library provides a **facade pattern** that abstracts away the complexity of integrating multiple observability backends. Application code interacts with a single API, and the library routes signals to the appropriate backend based on configuration.

### Design Philosophy

- **Single API**: One import, one interface for all observability needs
- **Backend Isolation**: Application code is decoupled from SDK-specific APIs
- **Graceful Degradation**: If a backend is unavailable, operations are no-ops with warnings
- **Configuration-Driven**: All behavior is controlled via YAML config with environment variable substitution

### Signal Routing

| Signal | Default Backend | Rationale |
|--------|-----------------|-----------|
| **Errors** | Sentry | Superior alerting, error grouping, stack traces, and debugging UX |
| **Metrics** | OpenTelemetry/Prometheus | Standard format, Grafana integration, cardinality control |
| **Traces** | OpenTelemetry | Infrastructure correlation, Grafana Tempo integration |

### Architecture Diagram

```
Application Code
       │
       │ observability.capture_error()
       │ observability.record_metric()
       │ observability.start_span()
       │
       ▼
┌──────────────────────────────────────┐
│        libs/observability/           │
│                                      │
│  ┌────────────────────────────────┐  │
│  │   ObservabilityFacade          │  │
│  │   (Unified Public API)         │  │
│  └────────────┬───────────────────┘  │
│               │                      │
│       ┌───────┴───────┐              │
│       ▼               ▼              │
│  ┌────────────┐  ┌──────────────┐    │
│  │   Sentry   │  │ OpenTelemetry│    │
│  │  Backend   │  │   Backend    │    │
│  └─────┬──────┘  └──────┬───────┘    │
└────────┼────────────────┼────────────┘
         │                │
         ▼                ▼
   ┌──────────┐    ┌───────────────┐
   │  Sentry  │    │ Grafana Cloud │
   │   SaaS   │    │  (via OTLP)   │
   └──────────┘    └───────────────┘
```

### Package Structure

```
libs/observability/
├── __init__.py          # Public exports: observability, ObservabilityFacade, SpanContext
├── facade.py            # Unified API facade and SpanContext
├── sentry_backend.py    # Sentry SDK wrapper
├── otel_backend.py      # OpenTelemetry wrapper
├── config.py            # Configuration loading with env var substitution
├── config/
├── requirements.txt     # Python dependencies
├── tests/               # Unit and integration tests
└── docs/                # Troubleshooting and guides
```

---

## Installation and Setup

### Dependencies

Add the observability library dependencies to your service:

```txt
# requirements.txt
sentry-sdk[opentelemetry]>=2.0.0
opentelemetry-api>=1.20.0
opentelemetry-sdk>=1.20.0
opentelemetry-exporter-otlp>=1.20.0
opentelemetry-exporter-prometheus>=0.41b0
pyyaml>=6.0
```

Or install directly:

```bash
pip install "sentry-sdk[opentelemetry]>=2.0.0" \
    opentelemetry-api opentelemetry-sdk \
    opentelemetry-exporter-otlp opentelemetry-exporter-prometheus \
    pyyaml
```

### Python Path Setup

The library is in the monorepo under `libs/`. Ensure it's importable:

```python
# Option 1: Add to PYTHONPATH in your shell or Dockerfile
export PYTHONPATH="${PYTHONPATH}:/path/to/aiq"

# Option 2: Add programmatically (not recommended for production)
import sys
sys.path.insert(0, "/path/to/aiq")
```

### Basic Initialization

```python
from libs.observability import observability

# Initialize at application startup (once)
observability.init(
    config_path="config/observability.yaml",  # Optional, uses default if not provided
    service_name="my-service",
    environment="production",
)
```

### FastAPI Integration Example

```python
# app/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from libs.observability import observability

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    observability.init(
        service_name="aiq-backend",
        environment=settings.ENV,
        sentry_dsn=settings.SENTRY_DSN,
        otel_endpoint=settings.OTEL_ENDPOINT,
    )
    yield
    # Shutdown
    observability.flush(timeout=5.0)
    observability.shutdown()

app = FastAPI(lifespan=lifespan)
```

---

## API Reference

### Initialization

#### `observability.init()`

Initialize observability backends. Must be called before using any other methods. Idempotent - calling multiple times logs a warning and returns True.

```python
observability.init(
    config_path: str | None = None,    # Path to YAML config file
    service_name: str | None = None,   # Override service name
    environment: str | None = None,    # Override environment
    **overrides                         # Additional config overrides
) -> bool
```

**Config Override Prefixes:**
- `sentry_*` - Override Sentry config (e.g., `sentry_dsn="..."`)
- `otel_*` - Override OTEL config (e.g., `otel_endpoint="..."`)
- `routing_*` - Override routing config (e.g., `routing_traces="both"`)

**Returns:** `True` if initialization succeeded, `False` on error.

**Example:**
```python
# With YAML config file
observability.init(
    config_path="config/observability.yaml",
    service_name="aiq-backend",
    environment="production",
)

# With programmatic overrides
observability.init(
    service_name="aiq-backend",
    environment="development",
    sentry_dsn="https://...",
    otel_endpoint="http://localhost:4317",
    otel_exporter="console",  # Use console for local dev
)
```

---

### Error Capture

#### `observability.capture_error()`

Capture an exception and send to Sentry. Automatically enriches with service metadata and OTEL trace context.

```python
observability.capture_error(
    exception: BaseException,
    *,
    context: dict[str, Any] | None = None,  # Additional context data
    level: str = "error",                    # "debug" | "info" | "warning" | "error" | "fatal"
    user: dict[str, Any] | None = None,      # User info: {"id": "...", "email": "..."}
    tags: dict[str, str] | None = None,      # Low-cardinality tags for filtering
    fingerprint: list[str] | None = None,    # Custom error grouping
) -> str | None
```

**Returns:** Sentry event ID if captured, `None` if skipped.

**Example:**
```python
try:
    result = process_payment(order)
except PaymentError as e:
    observability.capture_error(
        e,
        context={
            "order_id": str(order.id),
            "amount": order.total,
            "payment_provider": "stripe",
        },
        level="error",
        tags={"domain": "payments"},
        user={"id": str(user.id), "email": user.email},
    )
    raise
```

#### `observability.capture_message()`

Capture a message (non-exception) and send to Sentry. Use for notable events that aren't errors.

```python
observability.capture_message(
    message: str,
    *,
    level: str = "info",
    context: dict[str, Any] | None = None,
    tags: dict[str, str] | None = None,
) -> str | None
```

**Example:**
```python
# Track deprecation warnings
observability.capture_message(
    "Legacy API endpoint called - schedule for removal",
    level="warning",
    context={"endpoint": "/v1/legacy/users"},
    tags={"type": "deprecation"},
)
```

---

### Metrics

#### `observability.record_metric()`

Record a metric value to OpenTelemetry/Prometheus.

```python
observability.record_metric(
    name: str,                              # Metric name (use dot notation)
    value: float | int,                     # Metric value
    *,
    labels: dict[str, str] | None = None,   # Low-cardinality labels only!
    metric_type: str = "counter",           # "counter" | "histogram" | "gauge" | "updown_counter"
    unit: str | None = None,                # Unit: "1", "ms", "s", "bytes"
)
```

**Metric Types:**

| Type | Use Case | Example |
|------|----------|---------|
| `counter` | Cumulative counts that only increase | Requests, errors, events |
| `histogram` | Distribution of values | Latencies, sizes |
| `gauge` | Point-in-time absolute value | Temperature, active connections |
| `updown_counter` | Can increase or decrease | Queue size, active sessions |

**Naming Conventions:**
- Use lowercase with dots for hierarchy: `http.server.requests`
- Avoid spaces and special characters
- Be descriptive but concise

**Label Cardinality Warning:**
Never use high-cardinality values as labels (user IDs, request IDs, timestamps). This causes metric explosion in Prometheus.

**Examples:**
```python
# Counter: increment on each request
observability.record_metric(
    "http.server.requests",
    value=1,
    labels={"method": "POST", "route": "/v1/users", "status": "200"},
    metric_type="counter",
)

# Histogram: track latency distribution
observability.record_metric(
    "http.server.request.duration",
    value=0.123,  # seconds
    labels={"method": "GET", "route": "/v1/tests"},
    metric_type="histogram",
    unit="s",
)

# Gauge: current value (absolute)
observability.record_metric(
    "db.connections.active",
    value=15,
    labels={"database": "primary"},
    metric_type="gauge",
)

# UpDownCounter: track deltas
observability.record_metric(
    "queue.messages.pending",
    value=5,  # added 5 messages
    labels={"queue": "notifications"},
    metric_type="updown_counter",
)
```

---

### Distributed Tracing

#### `observability.start_span()`

Start a distributed tracing span. Spans are automatically nested based on call stack.

```python
@contextmanager
observability.start_span(
    name: str,
    *,
    kind: str = "internal",    # "internal" | "server" | "client" | "producer" | "consumer"
    attributes: dict[str, Any] | None = None,
) -> Iterator[SpanContext]
```

**Span Kinds:**

| Kind | Use Case |
|------|----------|
| `internal` | Default, internal operations |
| `server` | Handling incoming requests |
| `client` | Making outgoing requests |
| `producer` | Producing messages to a queue |
| `consumer` | Consuming messages from a queue |

**Example:**
```python
with observability.start_span("process_request", kind="server") as span:
    span.set_attribute("user_id", str(user.id))

    # Nested span for database operation
    with observability.start_span("db_query", kind="client") as db_span:
        result = db.execute(query)
        db_span.set_db_attributes(
            operation="SELECT",
            table="users",
            duration_ms=elapsed_ms,
            db_system="postgresql",
        )

    span.set_attribute("result_count", len(result))
```

#### `SpanContext` Methods

The span context provides methods for adding attributes and handling errors:

```python
# Set individual attributes
span.set_attribute(key: str, value: Any)

# Set span status
span.set_status(status: "ok" | "error", description: str = "")

# Record exception (also sets status to error)
span.record_exception(exception: BaseException)

# Add timestamped event
span.add_event(name: str, attributes: dict[str, Any] | None = None)

# Semantic conventions helpers
span.set_http_attributes(method, url, status_code, route, request_size, response_size)
span.set_db_attributes(operation, table, duration_ms, db_system, db_name, statement)
span.set_user_attributes(user_id, username, role, scope)
span.set_error_attributes(exception, escaped=True)
```

**HTTP Span Example:**
```python
with observability.start_span("http_request", kind="client") as span:
    response = await client.get(url)
    span.set_http_attributes(
        method="GET",
        url=url,
        status_code=response.status_code,
        response_size=len(response.content),
    )
```

**Database Span Example:**
```python
with observability.start_span("db_query") as span:
    start = time.perf_counter()
    result = await db.execute("SELECT * FROM users WHERE id = $1", user_id)
    elapsed_ms = (time.perf_counter() - start) * 1000

    span.set_db_attributes(
        operation="SELECT",
        table="users",
        duration_ms=elapsed_ms,
        db_system="postgresql",
        statement="SELECT * FROM users WHERE id = $1",  # Use parameterized query
    )
```

---

### User Context

#### `observability.set_user()`

Set user context for error tracking. Call after authentication.

```python
observability.set_user(
    user_id: str | None,           # Pass None to clear user context
    *,
    username: str | None = None,
    email: str | None = None,
    **extra,                        # Additional user attributes
)
```

**Example:**
```python
@app.middleware("http")
async def auth_middleware(request, call_next):
    user = await authenticate(request)
    if user:
        observability.set_user(
            str(user.id),
            username=user.username,
            email=user.email,
            subscription="premium",  # Custom attribute
        )
    response = await call_next(request)
    return response
```

---

### Tags and Context

#### `observability.set_tag()`

Set a tag on the current scope. Tags are indexed and searchable in Sentry.

```python
observability.set_tag(key: str, value: str)
```

**Example:**
```python
observability.set_tag("api_version", "v2")
observability.set_tag("feature_flag", "new_scoring_enabled")
```

#### `observability.set_context()`

Set a context block for detailed error reports. Unlike tags, context values are not indexed.

```python
observability.set_context(name: str, context: dict[str, Any])
```

**Example:**
```python
observability.set_context("request", {
    "url": request.url.path,
    "method": request.method,
    "headers": dict(request.headers),
})

observability.set_context("question_generation", {
    "batch_size": 10,
    "question_types": ["math", "verbal"],
    "target_difficulty": "medium",
})
```

---

### Events

#### `observability.record_event()`

Record a structured event for analytics. Similar to capture_message but emphasizes structured data.

```python
observability.record_event(
    name: str,                              # Event name (e.g., "user.signup")
    data: dict[str, Any] | None = None,     # Structured event data
    *,
    level: str = "info",
    tags: dict[str, str] | None = None,
) -> str | None
```

**Example:**
```python
observability.record_event(
    "test.completed",
    data={
        "test_session_id": str(session.id),
        "user_id": str(user.id),
        "score": score.aiq_score,
        "duration_seconds": elapsed,
        "questions_answered": len(responses),
    },
    tags={"test_type": "adaptive"},
)
```

---

### Trace Context

#### `observability.get_trace_context()`

Get current trace context for manual correlation.

```python
observability.get_trace_context() -> dict[str, str | None]
```

**Returns:**
```python
{
    "trace_id": "abc123...",  # 32-char hex or None
    "span_id": "def456...",   # 16-char hex or None
}
```

**Example:**
```python
# Include trace context in custom error reports
trace_ctx = observability.get_trace_context()
logger.error(
    "Operation failed",
    extra={
        "trace_id": trace_ctx["trace_id"],
        "span_id": trace_ctx["span_id"],
    }
)
```

---

### Lifecycle

#### `observability.flush()`

Flush pending events to all backends. Call before shutdown.

```python
observability.flush(timeout: float = 2.0)
```

#### `observability.shutdown()`

Shutdown all backends gracefully. Call on application exit.

```python
observability.shutdown()
```

**Example:**
```python
# FastAPI lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    observability.init(...)
    yield
    observability.flush(timeout=5.0)
    observability.shutdown()
```

---

## Configuration Guide

### YAML Configuration

Configuration files support environment variable substitution with `${VAR}` or `${VAR:default}` syntax.

**Full Configuration Reference:**

```yaml
# config/observability.yaml

sentry:
  enabled: true                           # Enable/disable Sentry backend
  dsn: ${SENTRY_DSN}                      # Sentry Data Source Name (required if enabled)
  environment: ${ENV:development}          # Environment name
  release: ${RELEASE}                      # Release/version identifier
  traces_sample_rate: 0.1                  # Sentry trace sampling (0.0 to 1.0)
  profiles_sample_rate: 0.0                # Sentry profiling sampling
  send_default_pii: false                  # Send personally identifiable information

otel:
  enabled: true                            # Enable/disable OpenTelemetry backend
  service_name: ${SERVICE_NAME:my-service} # Service name for metrics/traces
  service_version: ${RELEASE}              # Service version
  endpoint: ${OTEL_EXPORTER_OTLP_ENDPOINT} # OTLP collector endpoint
  exporter: otlp                           # "console" | "otlp" | "none"
  otlp_headers: ${OTEL_EXPORTER_OTLP_HEADERS}  # Auth headers (key=value,key2=value2)
  metrics_enabled: true                    # Enable OTEL metrics
  metrics_export_interval_millis: 60000    # Export interval (60 seconds)
  traces_enabled: true                     # Enable OTEL traces
  traces_sample_rate: 1.0                  # OTEL trace sampling (0.0 to 1.0)
  logs_enabled: false                      # Forward logs to OTEL (experimental)
  prometheus_enabled: true                 # Enable Prometheus metric reader
  insecure: false                          # Use insecure connection (dev only)

routing:
  errors: sentry      # Where to send errors: "sentry" | "otel" | "both"
  metrics: otel       # Where to send metrics: "sentry" | "otel" | "both"
  traces: otel        # Where to send traces: "sentry" | "otel" | "both"
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SENTRY_DSN` | Sentry Data Source Name | (required for Sentry) |
| `ENV` | Environment name | `development` |
| `SERVICE_NAME` | Service name for OTEL | `aiq-service` |
| `RELEASE` | Release version/git SHA | (none) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP collector endpoint | (none) |
| `OTEL_EXPORTER_OTLP_HEADERS` | OTLP auth headers | (none) |

### Configuration Precedence

Configuration is loaded in this order (later overrides earlier):

1. Default values in config dataclasses
2. Default YAML (`libs/observability/config/default.yaml`)
3. Specified YAML config file
4. Environment variables (via `${VAR}` substitution)
5. Explicit overrides passed to `init()`

### Environment-Specific Examples

**Development:**
```yaml
sentry:
  enabled: false  # Skip Sentry in dev

otel:
  exporter: console  # Log to stdout
  traces_sample_rate: 1.0  # Trace everything
```

**Production:**
```yaml
sentry:
  enabled: true
  dsn: ${SENTRY_DSN}
  environment: production
  traces_sample_rate: 0.1  # Sample 10%

otel:
  enabled: true
  exporter: otlp
  endpoint: ${OTEL_EXPORTER_OTLP_ENDPOINT}
  traces_sample_rate: 0.1
```

---

## Backend-Specific Notes

### Sentry Backend

The Sentry backend handles error capture, message capture, and optional tracing.

**Integrations:**
The backend automatically enables these Sentry integrations when available:
- `LoggingIntegration` - Breadcrumbs from logs
- `FastApiIntegration` - Auto-instrument FastAPI routes
- `StarletteIntegration` - Auto-instrument Starlette
- `OpenTelemetryIntegration` - Correlate with OTEL traces

**Context Serialization:**
Non-JSON types in context are automatically serialized:
- `datetime` → ISO format string
- `UUID` → string
- `bytes` → UTF-8 string or `<bytes: N bytes>`
- Objects with `__dict__` → dict of public attributes
- Circular references → `<circular reference: TypeName>`

**Best Practices:**
- Use `tags` for low-cardinality data you want to filter by
- Use `context` for high-cardinality debugging data
- Use `fingerprint` to customize error grouping
- Set `user` to enable user impact analysis

### OpenTelemetry Backend

The OTEL backend handles metrics, tracing, and optional log forwarding.

**Exporters:**
- `console` - Outputs to stdout (development)
- `otlp` - Sends to OTLP-compatible endpoints (Grafana Cloud, Jaeger, etc.)
- `none` - Disables export (testing)

**Metric Validation:**
The backend validates metric names and warns about:
- Invalid characters (must be lowercase alphanumeric with dots/underscores)
- High-cardinality label patterns (user_id, request_id, timestamp)

**Trace Correlation:**
If both Sentry and OTEL are enabled, the backend adds a `SentrySpanProcessor` to correlate OTEL traces with Sentry errors.

**Prometheus Integration:**
When `prometheus_enabled: true`, metrics are exposed for Prometheus scraping. Configure your Prometheus scraper to hit the `/metrics` endpoint.

---

## Troubleshooting

For comprehensive troubleshooting, see the full **[Troubleshooting Guide](docs/TROUBLESHOOTING.md)** which covers:

- Sentry not receiving errors
- OTEL metrics not appearing in Grafana
- Trace correlation not working
- Configuration validation errors
- Import path issues
- Performance issues

### Quick Diagnostics

```python
from libs.observability import observability
import os

# Check environment variables
print(f"SENTRY_DSN: {'SET' if os.environ.get('SENTRY_DSN') else 'NOT SET'}")
print(f"OTEL_ENDPOINT: {'SET' if os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT') else 'NOT SET'}")

# Check initialization
print(f"Initialized: {observability.is_initialized}")
print(f"Sentry backend: {observability._sentry_backend is not None}")
print(f"OTEL backend: {observability._otel_backend is not None}")

# Check config
if observability._config:
    print(f"Sentry enabled: {observability._config.sentry.enabled}")
    print(f"OTEL enabled: {observability._config.otel.enabled}")
    print(f"Traces routing: {observability._config.routing.traces}")
```

### Common Issues Quick Reference

| Issue | First Check | Quick Fix |
|-------|-------------|-----------|
| No Sentry errors | `echo $SENTRY_DSN` | Export `SENTRY_DSN` |
| No OTEL metrics | `echo $OTEL_EXPORTER_OTLP_ENDPOINT` | Export endpoint and headers |
| Import errors | `echo $PYTHONPATH` | Add project root to PYTHONPATH |
| Trace correlation | Check routing config | Set `routing.traces: both` |

### Debug Logging

Enable debug logging to see detailed initialization and operation logs:

```python
import logging
logging.getLogger("libs.observability").setLevel(logging.DEBUG)
```

---

## Migration Guide

### From Direct Sentry SDK Usage

**Before:**
```python
import sentry_sdk
from sentry_sdk import capture_exception, set_user

sentry_sdk.init(dsn="...", environment="production")

try:
    do_work()
except Exception as e:
    capture_exception(e)
    raise

set_user({"id": user_id})
```

**After:**
```python
from libs.observability import observability

observability.init(
    sentry_dsn="...",
    environment="production",
)

try:
    do_work()
except Exception as e:
    observability.capture_error(e)
    raise

observability.set_user(user_id)
```

### From Direct OpenTelemetry Usage

**Before:**
```python
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.metrics import MeterProvider

trace.set_tracer_provider(TracerProvider())
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)
counter = meter.create_counter("requests")

with tracer.start_as_current_span("operation"):
    counter.add(1, {"status": "success"})
```

**After:**
```python
from libs.observability import observability

observability.init(service_name="my-service")

with observability.start_span("operation") as span:
    observability.record_metric(
        "requests",
        value=1,
        labels={"status": "success"},
        metric_type="counter",
    )
```

### From Mixed SDK Usage

If you were using both SDKs independently, the library handles:

- **Unified initialization** - One `init()` call configures both
- **Trace correlation** - OTEL spans are automatically linked to Sentry errors
- **Consistent configuration** - Environment, service name, and sample rates apply to both
- **Graceful shutdown** - One `shutdown()` call cleans up both

### Gradual Migration

You can migrate incrementally:

1. Initialize the observability library alongside existing SDK usage
2. Gradually replace direct SDK calls with facade methods
3. Remove direct SDK initialization once all calls are migrated

The library is designed to coexist with direct SDK usage during migration.

---

## Testing

### Running Tests

```bash
# From repo root
pytest libs/observability/tests/ -v

# With coverage
pytest libs/observability/tests/ --cov=libs/observability --cov-report=term-missing
```

### Test Categories

- `test_config.py` - Configuration loading and validation
- `test_backends.py` - Backend initialization and operations
- `test_facade.py` - Facade API and routing
- `test_sentry_backend.py` - Sentry-specific behavior
- `test_otel_backend.py` - OpenTelemetry-specific behavior
- `test_graceful_degradation.py` - Behavior when backends unavailable
- `test_integration.py` - End-to-end integration tests

### Mocking in Tests

```python
from unittest.mock import patch

# Mock observability for unit tests
with patch("libs.observability.observability") as mock_obs:
    mock_obs.is_initialized = True
    mock_obs.capture_error.return_value = "event-123"

    # Your test code
    result = function_under_test()

    mock_obs.capture_error.assert_called_once()
```

---

## Contributing

When adding features to the observability library:

1. **Maintain the facade pattern** - Application code should only import from `__init__.py`
2. **Keep backends isolated** - Backend-specific logic stays in backend modules
3. **Graceful degradation** - Operations should be no-ops when backends unavailable
4. **Configuration-driven** - New features should be controllable via config
5. **Add tests** - Cover happy path, edge cases, and graceful degradation

---

## License

Internal use only. Part of the AIQ monorepo.
