# gioe-libs

Shared Python libraries used across projects.

## Installation

```bash
pip install git+https://github.com/gioe/python-libs.git
```

With optional observability dependencies:

```bash
pip install "git+https://github.com/gioe/python-libs.git#egg=gioe-libs[observability]"
```

With product-specific domain type definitions (AIQ enums):

```bash
pip install "git+https://github.com/gioe/python-libs.git#egg=gioe-libs[domain-types]"
```

## Modules

### `gioe_libs.aiq_logging`

Structured logging configuration with JSON output support.

### `gioe_libs.alerting`

Alerting utilities for sending notifications (e.g., Slack).

### `gioe_libs.cron_runner`

Cron job runner abstraction for scheduled tasks.

### `gioe_libs.domain_types` *(optional extra)*

Product-specific domain type definitions for AIQ services (QuestionType, TestStatus, etc.).
Not installed by default — install via `gioe-libs[domain-types]`.

### `gioe_libs.observability`

Facade-pattern observability library wrapping OpenTelemetry and Sentry backends.

- Supports OTLP and Prometheus exporters
- Graceful degradation when backends are unavailable
- Config-driven via YAML

See [`observability/README.md`](observability/README.md) for detailed usage.

## Development

```bash
pip install -e .
```

Run tests:

```bash
PYTHONPATH=. pytest
```
