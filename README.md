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

## Modules

### `gioe_libs.structured_logging`

Structured logging configuration with JSON output support.

### `gioe_libs.alerting`

Alerting utilities for sending notifications (e.g., Slack).

### `gioe_libs.cron_runner`

Cron job runner abstraction for scheduled tasks.

### `gioe_libs.domain_types`

Shared domain type definitions used across services.

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
