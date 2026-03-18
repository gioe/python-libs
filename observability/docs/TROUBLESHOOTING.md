# Observability Troubleshooting Guide

This guide covers common issues with the AIQ observability library, their symptoms, root causes, solutions, and prevention strategies.

## Table of Contents

- [Sentry Issues](#sentry-issues)
  - [Sentry Not Receiving Errors](#sentry-not-receiving-errors)
  - [Sentry Initialization Failed](#sentry-initialization-failed)
  - [Errors Missing Context](#errors-missing-context)
- [OpenTelemetry Issues](#opentelemetry-issues)
  - [OTEL Metrics Not Appearing in Grafana](#otel-metrics-not-appearing-in-grafana)
  - [OTEL Initialization Failed](#otel-initialization-failed)
  - [High Metric Cardinality Warnings](#high-metric-cardinality-warnings)
- [Trace Correlation Issues](#trace-correlation-issues)
  - [Trace Correlation Not Working](#trace-correlation-not-working)
  - [Missing Trace Context in Errors](#missing-trace-context-in-errors)
- [Configuration Issues](#configuration-issues)
  - [Configuration Validation Errors](#configuration-validation-errors)
  - [Environment Variable Substitution Not Working](#environment-variable-substitution-not-working)
- [Import and Path Issues](#import-and-path-issues)
  - [ModuleNotFoundError for libs.observability](#modulenotfounderror-for-libsobservability)
  - [Missing OpenTelemetry Packages](#missing-opentelemetry-packages)
- [Performance Issues](#performance-issues)
  - [High Memory Usage from Metrics](#high-memory-usage-from-metrics)
  - [Slow Application Startup](#slow-application-startup)
  - [Flush Timeouts on Shutdown](#flush-timeouts-on-shutdown)

---

## Sentry Issues

### Sentry Not Receiving Errors

**Symptoms:**
- No errors appearing in Sentry dashboard
- `capture_error()` returns `None`
- Warning logs: "Sentry backend not available"

**Diagnostic Steps:**

```python
# Check if Sentry DSN is configured
import os
print(f"SENTRY_DSN: {os.environ.get('SENTRY_DSN', 'NOT SET')}")

# Check initialization status
from libs.observability import observability
print(f"Initialized: {observability.is_initialized}")
print(f"Sentry backend: {observability._sentry_backend is not None}")

# Check config if initialized
if observability._config:
    print(f"Sentry enabled: {observability._config.sentry.enabled}")
    print(f"Sentry DSN set: {bool(observability._config.sentry.dsn)}")
```

**Root Causes:**

1. **DSN not configured:**
   - `SENTRY_DSN` environment variable is empty or not set
   - YAML config has `sentry.dsn: ${SENTRY_DSN}` but env var is missing

2. **Sentry explicitly disabled:**
   - Config has `sentry.enabled: false`
   - `SENTRY_ENABLED=False` in environment

3. **Observability not initialized:**
   - `observability.init()` was never called
   - Application crashed before initialization

4. **Events filtered by Sentry:**
   - Sample rate is 0% (`traces_sample_rate: 0.0`)
   - Environment filter in Sentry project settings
   - Inbound filter blocking events

5. **Network issues:**
   - Firewall blocking `*.sentry.io`
   - DNS resolution failure

**Solutions:**

```bash
# 1. Set the DSN environment variable
export SENTRY_DSN="https://your-key@sentry.io/your-project-id"

# 2. Verify DSN format (should be a URL starting with https://)
# Format: https://<public_key>@<organization>.sentry.io/<project_id>

# 3. For Railway deployments, add to Variables tab:
SENTRY_DSN=https://your-key@sentry.io/your-project-id
```

```python
# Ensure init() is called at application startup
from libs.observability import observability

# In FastAPI lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    success = observability.init(
        service_name="my-service",
        environment="production",
    )
    if not success:
        logger.warning("Observability initialization failed")
    yield
    observability.flush(timeout=5.0)
    observability.shutdown()
```

**Prevention:**
- Add DSN validation in CI/CD pipeline
- Use health checks that verify observability initialization
- Set up Sentry alerts for missing events from expected services

---

### Sentry Initialization Failed

**Symptoms:**
- Log message: "Failed to initialize Sentry: ..."
- `observability.init()` returns `True` but Sentry features don't work
- `observability._sentry_backend` is `None`

**Diagnostic Steps:**

```python
# Enable debug logging to see detailed initialization
import logging
logging.getLogger("libs.observability").setLevel(logging.DEBUG)

# Re-initialize (will log detailed errors)
from libs.observability import observability
observability.shutdown()  # Reset state
observability.init(service_name="debug-test")
```

**Root Causes:**

1. **Invalid DSN format:**
   - Malformed URL
   - Wrong project ID or key

2. **Missing Sentry SDK:**
   - `sentry-sdk` not installed
   - Version incompatibility

3. **Integration import errors:**
   - FastAPI/Starlette integrations require those packages

**Solutions:**

```bash
# Install or upgrade Sentry SDK
pip install "sentry-sdk[opentelemetry]>=2.0.0"

# For FastAPI integration
pip install "sentry-sdk[fastapi]>=2.0.0"
```

```python
# Verify DSN programmatically before init
import os
dsn = os.environ.get("SENTRY_DSN", "")
if not dsn.startswith("https://"):
    raise ValueError(f"Invalid SENTRY_DSN format: {dsn[:20]}...")
```

**Prevention:**
- Pin Sentry SDK version in requirements.txt
- Test initialization in CI before deployment

---

### Errors Missing Context

**Symptoms:**
- Errors appear in Sentry but lack useful debugging info
- Missing user information, tags, or custom context

**Root Causes:**

1. **Context not passed to `capture_error()`:**
   - Not using `context={}` parameter
   - Not setting user with `set_user()`

2. **Non-serializable context values:**
   - Objects, functions, or circular references in context

**Solutions:**

```python
# Always include relevant context
try:
    result = process_order(order_id)
except ProcessingError as e:
    observability.capture_error(
        e,
        context={
            "order_id": str(order_id),  # Convert UUIDs to strings
            "step": "payment_processing",
            "retry_count": retry_count,
        },
        tags={"domain": "orders"},
        user={"id": str(current_user.id)},
    )
    raise
```

```python
# Set user context at authentication time
@app.middleware("http")
async def auth_middleware(request, call_next):
    user = await authenticate(request)
    if user:
        observability.set_user(
            str(user.id),
            username=user.username,
            email=user.email,
        )
    return await call_next(request)
```

**Prevention:**
- Create wrapper functions that always include standard context
- Use structured logging that automatically enriches errors

---

## OpenTelemetry Issues

### OTEL Metrics Not Appearing in Grafana

**Symptoms:**
- Metrics not visible in Grafana Cloud Prometheus explorer
- No data points for custom metrics
- Warning: "OTEL backend not available"

**Diagnostic Steps:**

```python
import os

# Check OTEL configuration
print(f"OTEL_EXPORTER_OTLP_ENDPOINT: {os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT', 'NOT SET')}")
print(f"OTEL_EXPORTER_OTLP_HEADERS: {'SET' if os.environ.get('OTEL_EXPORTER_OTLP_HEADERS') else 'NOT SET'}")

# Check observability state
from libs.observability import observability
print(f"OTEL backend: {observability._otel_backend is not None}")
if observability._config:
    print(f"OTEL enabled: {observability._config.otel.enabled}")
    print(f"Metrics enabled: {observability._config.otel.metrics_enabled}")
    print(f"Exporter: {observability._config.otel.exporter}")
    print(f"Endpoint: {observability._config.otel.endpoint}")
```

**Root Causes:**

1. **Missing or incorrect OTLP endpoint:**
   - `OTEL_EXPORTER_OTLP_ENDPOINT` not set
   - Wrong region for Grafana Cloud

2. **Authentication failure:**
   - Missing `OTEL_EXPORTER_OTLP_HEADERS`
   - Invalid API token
   - Token without MetricsPublisher permission

3. **OTEL explicitly disabled:**
   - `otel.enabled: false` in config
   - `otel.metrics_enabled: false`

4. **Export interval not elapsed:**
   - Default is 60 seconds
   - Metrics only appear after first export

5. **Invalid metric names:**
   - Names with spaces or special characters
   - Names not starting with a letter

**Solutions:**

```bash
# For Grafana Cloud, set both endpoint and auth header
export OTEL_EXPORTER_OTLP_ENDPOINT="https://otlp-gateway-prod-us-central-0.grafana.net/otlp"
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer YOUR_API_TOKEN"

# Verify your Grafana Cloud region (us-central-0, eu-west-0, etc.)
```

```yaml
# config/observability.yaml
otel:
  enabled: true
  service_name: ${SERVICE_NAME:my-service}
  endpoint: ${OTEL_EXPORTER_OTLP_ENDPOINT}
  otlp_headers: ${OTEL_EXPORTER_OTLP_HEADERS}
  metrics_enabled: true
  metrics_export_interval_millis: 60000
  exporter: otlp  # Not 'console' or 'none'
```

```python
# For local development, use console exporter to debug
observability.init(
    service_name="my-service",
    otel_exporter="console",  # Logs metrics to stdout
)
```

**Prevention:**
- Add OTEL connectivity test to health checks
- Monitor metric export success/failure in logs
- Use shorter export intervals in development

---

### OTEL Initialization Failed

**Symptoms:**
- Log message: "Failed to initialize OpenTelemetry: ..."
- Log message: "OTLP exporter not available"
- Metrics and traces not working

**Diagnostic Steps:**

```python
# Check installed packages
import subprocess
result = subprocess.run(
    ["pip", "list", "--format=columns"],
    capture_output=True, text=True
)
for line in result.stdout.split("\n"):
    if "opentelemetry" in line.lower():
        print(line)
```

**Root Causes:**

1. **Missing OTEL packages:**
   - Core SDK not installed
   - Exporter packages missing

2. **Version conflicts:**
   - Incompatible versions between OTEL packages
   - API/SDK version mismatch

**Solutions:**

```bash
# Install all required OTEL packages
pip install \
    opentelemetry-api>=1.20.0 \
    opentelemetry-sdk>=1.20.0 \
    opentelemetry-exporter-otlp>=1.20.0 \
    opentelemetry-exporter-prometheus>=0.41b0

# For gRPC OTLP exporter (default)
pip install opentelemetry-exporter-otlp-proto-grpc>=1.20.0
```

```bash
# Pin compatible versions in requirements.txt
opentelemetry-api==1.21.0
opentelemetry-sdk==1.21.0
opentelemetry-exporter-otlp==1.21.0
opentelemetry-exporter-prometheus==0.42b0
```

**Prevention:**
- Use a requirements.txt with pinned, tested versions
- Test OTEL initialization in CI

---

### High Metric Cardinality Warnings

**Symptoms:**
- Warning logs: "High-cardinality label '...' detected in metric '...'"
- Grafana queries timing out
- Prometheus storage costs increasing

**Root Causes:**

1. **Using unbounded values as labels:**
   - User IDs in metric labels
   - Request IDs, session IDs
   - Timestamps as labels

**Solutions:**

```python
# BAD: High cardinality - user_id has millions of unique values
observability.record_metric(
    "requests.count",
    1,
    labels={"user_id": user_id},  # DON'T DO THIS
)

# GOOD: Low cardinality - finite set of subscription types
observability.record_metric(
    "requests.count",
    1,
    labels={"subscription_type": user.subscription_type},  # OK
)
```

```python
# GOOD label values (finite, bounded sets):
# - HTTP methods: GET, POST, PUT, DELETE, PATCH
# - Status codes: 200, 400, 404, 500
# - Question types: math, verbal, logic, pattern
# - Difficulty levels: easy, medium, hard
# - Endpoints: /v1/auth/login, /v1/test/start

# BAD label values (unbounded):
# - user_id, session_id, request_id
# - timestamps, dates
# - email addresses, IP addresses
# - UUIDs of any kind
```

**Prevention:**
- Code review for metric label usage
- Set up cardinality alerts in Grafana
- Use the library's built-in high-cardinality detection (logs warnings)

---

## Trace Correlation Issues

### Trace Correlation Not Working

**Symptoms:**
- Traces in Grafana Tempo but not linked to Sentry errors
- Sentry errors don't show trace IDs
- Can't navigate from Sentry to Grafana traces

**Diagnostic Steps:**

```python
# Check if both backends are active
from libs.observability import observability
print(f"Sentry backend: {observability._sentry_backend is not None}")
print(f"OTEL backend: {observability._otel_backend is not None}")

# Check routing config
if observability._config:
    print(f"Traces routing: {observability._config.routing.traces}")
```

**Root Causes:**

1. **OpenTelemetry integration not installed:**
   - `sentry-sdk[opentelemetry]` extra not installed

2. **Only one backend enabled:**
   - Either Sentry or OTEL is disabled

3. **Traces routing not set to "both":**
   - Config has `routing.traces: otel` instead of `routing.traces: both`

4. **No active span when error occurs:**
   - Error captured outside of a span context

**Solutions:**

```bash
# Install Sentry with OpenTelemetry integration
pip install "sentry-sdk[opentelemetry]>=2.0.0"
```

```yaml
# config/observability.yaml
routing:
  errors: sentry
  metrics: otel
  traces: both  # Send traces to BOTH Sentry and OTEL
```

```python
# Ensure errors are captured within spans
with observability.start_span("process_request") as span:
    try:
        result = risky_operation()
    except Exception as e:
        # Error captured here will include trace context
        observability.capture_error(e, context={"operation": "risky"})
        raise
```

**Prevention:**
- Always capture errors within span contexts
- Verify trace correlation in dev environment before deploying

---

### Missing Trace Context in Errors

**Symptoms:**
- Sentry errors don't show trace_id or span_id
- `get_trace_context()` returns `{"trace_id": None, "span_id": None}`

**Root Causes:**

1. **No active span:**
   - Error captured outside of `start_span()` context

2. **OTEL not initialized:**
   - OTEL backend failed to initialize or is disabled

**Solutions:**

```python
# Check for active span before capturing
trace_ctx = observability.get_trace_context()
if trace_ctx["trace_id"]:
    # Good - we have an active span
    observability.capture_error(e, context={"trace": trace_ctx})
else:
    # No active span - error won't have trace correlation
    observability.capture_error(e)
```

```python
# Wrap request handlers in spans
@app.middleware("http")
async def tracing_middleware(request, call_next):
    with observability.start_span(
        f"{request.method} {request.url.path}",
        kind="server"
    ) as span:
        span.set_http_attributes(
            method=request.method,
            url=str(request.url),
        )
        try:
            response = await call_next(request)
            span.set_http_attributes(
                method=request.method,
                url=str(request.url),
                status_code=response.status_code,
            )
            return response
        except Exception as e:
            observability.capture_error(e)  # Captured with trace context
            raise
```

---

## Configuration Issues

### Configuration Validation Errors

**Symptoms:**
- Error: "Configuration validation failed: ..."
- Specific validation errors like:
  - "Sentry DSN is required when sentry.enabled=True"
  - "Invalid sentry.traces_sample_rate: ..."
  - "Invalid routing.errors: ..."

**Diagnostic Steps:**

```python
# Try loading config manually to see errors
from libs.observability.config import load_config, ConfigurationError

try:
    config = load_config(config_path="config/observability.yaml")
    print("Config loaded successfully")
except ConfigurationError as e:
    print(f"Validation failed:\n{e}")
```

**Root Causes:**

1. **Missing required values:**
   - Sentry enabled but DSN not set
   - Environment variable not defined

2. **Invalid value ranges:**
   - Sample rates outside 0.0-1.0
   - Negative export intervals

3. **Invalid routing values:**
   - Routing set to something other than "sentry", "otel", or "both"

**Solutions:**

```yaml
# Valid configuration example
sentry:
  enabled: true
  dsn: ${SENTRY_DSN}  # Must be set in environment
  environment: ${ENV:development}
  traces_sample_rate: 0.1  # Must be 0.0-1.0

otel:
  enabled: true
  service_name: ${SERVICE_NAME:my-service}
  traces_sample_rate: 0.1  # Must be 0.0-1.0
  metrics_export_interval_millis: 60000  # Must be positive

routing:
  errors: sentry  # Must be: sentry, otel, or both
  metrics: otel   # Must be: sentry, otel, or both
  traces: both    # Must be: sentry, otel, or both
```

```bash
# Ensure required environment variables are set
export SENTRY_DSN="https://..."
export SERVICE_NAME="my-service"
export ENV="production"
```

**Prevention:**
- Validate config in CI/CD before deployment
- Use config validation in application health checks

---

### Environment Variable Substitution Not Working

**Symptoms:**
- Config values contain literal `${VAR}` instead of substituted values
- Empty strings where values should be

**Root Causes:**

1. **Environment variable not set:**
   - Variable referenced but not exported

2. **Wrong syntax:**
   - Using `$VAR` instead of `${VAR}`

3. **Config loaded before env vars set:**
   - Init called before environment is configured

**Solutions:**

```bash
# Verify environment variable is set
echo $SENTRY_DSN

# Export if needed
export SENTRY_DSN="https://..."

# For default values, use colon syntax
# ${VAR:default_value}
```

```yaml
# Using defaults in YAML
otel:
  service_name: ${SERVICE_NAME:my-service}  # Uses "my-service" if SERVICE_NAME not set
  environment: ${ENV:development}  # Uses "development" if ENV not set
```

```python
# Set environment before importing/initializing
import os
os.environ["SENTRY_DSN"] = "https://..."

# Now initialize
from libs.observability import observability
observability.init()
```

---

## Import and Path Issues

### ModuleNotFoundError for libs.observability

**Symptoms:**
- Error: `ModuleNotFoundError: No module named 'libs'`
- Error: `ModuleNotFoundError: No module named 'libs.observability'`

**Root Causes:**

1. **PYTHONPATH not configured:**
   - Project root not in Python path

2. **Running from wrong directory:**
   - Current working directory doesn't have `libs/`

**Solutions:**

```bash
# Option 1: Set PYTHONPATH in shell
export PYTHONPATH="${PYTHONPATH}:/path/to/aiq"

# Option 2: Set in Dockerfile
ENV PYTHONPATH="/app:${PYTHONPATH}"

# Option 3: Set in docker-compose.yml
environment:
  - PYTHONPATH=/app
```

```python
# Option 4: Add programmatically (not recommended for production)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from libs.observability import observability
```

**Prevention:**
- Configure PYTHONPATH in deployment configuration
- Use proper Python packaging for shared libraries

---

### Missing OpenTelemetry Packages

**Symptoms:**
- Error: "OpenTelemetry packages not installed: ..."
- Error: "OTLP exporter not available"
- Error: "Prometheus metric reader not available"

**Solutions:**

```bash
# Install all OTEL packages
pip install \
    opentelemetry-api \
    opentelemetry-sdk \
    opentelemetry-exporter-otlp \
    opentelemetry-exporter-otlp-proto-grpc \
    opentelemetry-exporter-prometheus

# For Sentry integration
pip install "sentry-sdk[opentelemetry]"
```

```txt
# requirements.txt
opentelemetry-api>=1.20.0
opentelemetry-sdk>=1.20.0
opentelemetry-exporter-otlp>=1.20.0
opentelemetry-exporter-prometheus>=0.41b0
sentry-sdk[opentelemetry]>=2.0.0
```

---

## Performance Issues

### High Memory Usage from Metrics

**Symptoms:**
- Application memory growing continuously
- OOM errors in production
- Prometheus cardinality explosion

**Root Causes:**

1. **High-cardinality metrics:**
   - User IDs or request IDs as labels
   - Creating new metric time series unbounded

2. **Too many unique metric names:**
   - Dynamically generated metric names

**Solutions:**

```python
# Avoid dynamic metric names
# BAD:
for endpoint in endpoints:
    observability.record_metric(f"requests.{endpoint}", 1)

# GOOD:
for endpoint in endpoints:
    observability.record_metric("requests.total", 1, labels={"endpoint": endpoint})
```

```python
# Limit label cardinality
def normalize_endpoint(path: str) -> str:
    """Convert /users/123 to /users/{id}"""
    import re
    # Replace UUIDs and numeric IDs with placeholders
    path = re.sub(r'/[0-9a-f-]{36}', '/{id}', path)
    path = re.sub(r'/\d+', '/{id}', path)
    return path

observability.record_metric(
    "requests.total",
    1,
    labels={"endpoint": normalize_endpoint(request.path)}
)
```

**Prevention:**
- Audit all metric labels for cardinality
- Set up cardinality monitoring in Grafana
- Review metrics in code review

---

### Slow Application Startup

**Symptoms:**
- Application takes too long to start
- Health checks failing due to slow init
- Timeout during container startup

**Root Causes:**

1. **OTLP endpoint unreachable:**
   - Network timeout waiting for connection
   - DNS resolution slow

2. **Loading many integrations:**
   - All Sentry integrations loading at once

**Solutions:**

```yaml
# Set shorter timeouts in development
otel:
  insecure: true  # Skip TLS for local development
```

```python
# Initialize observability in background
import threading

def init_observability():
    observability.init(service_name="my-service")

# Don't block startup
threading.Thread(target=init_observability, daemon=True).start()

# Or use FastAPI background tasks
@app.on_event("startup")
async def startup():
    # Quick startup without blocking
    observability.init(
        service_name="my-service",
        sentry_traces_sample_rate=0.0,  # Disable tracing for fast startup
    )
```

**Prevention:**
- Use async initialization where possible
- Set appropriate timeouts
- Monitor startup time in CI

---

### Flush Timeouts on Shutdown

**Symptoms:**
- Application takes too long to shutdown
- "Failed to flush" warnings in logs
- Data loss on graceful shutdown

**Root Causes:**

1. **Large backlog of events:**
   - Many events queued but not exported

2. **Network issues:**
   - Slow connection to Sentry/OTLP endpoint

3. **Timeout too short:**
   - Default 2 second timeout not enough

**Solutions:**

```python
# Increase flush timeout for graceful shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    observability.init(service_name="my-service")
    yield
    # Give more time to flush
    observability.flush(timeout=10.0)
    observability.shutdown()
```

```python
# Handle shutdown signals properly
import signal
import sys

def shutdown_handler(signum, frame):
    print("Shutting down gracefully...")
    observability.flush(timeout=5.0)
    observability.shutdown()
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)
```

**Prevention:**
- Always call `flush()` before `shutdown()`
- Set appropriate timeout based on expected event volume
- Monitor flush success rate

---

## Debug Mode

For detailed troubleshooting, enable debug logging:

```python
import logging

# Enable debug logging for observability
logging.getLogger("libs.observability").setLevel(logging.DEBUG)

# Also enable SDK debug logging if needed
logging.getLogger("sentry_sdk").setLevel(logging.DEBUG)
logging.getLogger("opentelemetry").setLevel(logging.DEBUG)
```

This will log:
- Backend initialization details
- Configuration loading steps
- Event capture attempts
- Export success/failure

---

## Quick Reference

| Issue | First Check | Quick Fix |
|-------|-------------|-----------|
| No Sentry errors | `echo $SENTRY_DSN` | Export SENTRY_DSN |
| No OTEL metrics | `echo $OTEL_EXPORTER_OTLP_ENDPOINT` | Export endpoint and headers |
| Import errors | `echo $PYTHONPATH` | Add project root to PYTHONPATH |
| Config validation | Check YAML syntax | Use `${VAR:default}` for optional vars |
| High cardinality | Review metric labels | Remove user_id, request_id from labels |
| Trace correlation | Check routing config | Set `routing.traces: both` |

---

## Getting Help

If you're still experiencing issues:

1. Enable debug logging and capture the output
2. Check the [README](../README.md) for API documentation
3. Review test files for usage examples:
   - `tests/test_graceful_degradation.py` - Error handling patterns
   - `tests/test_integration.py` - Realistic workflows
4. Check deployment docs:
   - `backend/DEPLOYMENT.md` - Railway deployment
   - `question-service/docs/RAILWAY_DEPLOYMENT.md` - Question service deployment
