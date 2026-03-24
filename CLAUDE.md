# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

`gioe-libs` is a Python shared library (`gioe_libs`) providing cross-cutting infrastructure concerns for microservices: structured logging, alerting, cron job harness, domain enums, and an observability facade.

Install from source: `pip install -e .`
Install from GitHub: `pip install "git+https://github.com/gioe/python-libs.git[observability]"`

Python requirement: >=3.11

## Commands

```bash
# Run all tests
PYTHONPATH=. pytest

# Run a single test file
PYTHONPATH=. pytest observability/tests/test_facade.py

# Run a specific test
PYTHONPATH=. pytest observability/tests/test_facade.py::TestClassName::test_method_name

# Skip slow/integration tests
PYTHONPATH=. pytest -m "not integration and not slow"

# Install with optional observability dependencies
pip install -e ".[observability]"
```

## Architecture

The library is organized into independent modules under `gioe_libs/`:

### `observability/` — Unified Observability Facade (largest, most complex)
Implements the **facade pattern**: application code imports a single `observability` singleton and calls a unified API. Internally routes signals to Sentry and/or OpenTelemetry.

- **Signal routing**: errors → Sentry, metrics → OpenTelemetry, traces → OpenTelemetry
- **`facade.py`**: `ObservabilityFacade` (1300+ lines) is the main public API
- **`config.py`**: YAML loading with `${ENV_VAR:default}` substitution
- **`otel_backend.py` / `sentry_backend.py`**: SDK wrappers behind the facade
- **Graceful degradation**: missing/disabled backends silently no-op instead of crashing
- Configuration lives in `config/default.yaml`; backends are all optional

Public API surface: `observability.init()`, `capture_error()`, `record_metric()`, `start_span()`, `record_event()`, `set_user()`, `set_context()`

### `structured_logging/` — Structured Logging
JSON-formatted logging with request correlation. Uses `contextvars.ContextVar` for async-safe request ID propagation. Automatically injects OTel `trace_id`/`span_id` into log records.

### `alerting/` — Alert Manager
Routes error alerts via email/Slack with deduplication and rate-limiting. Supports an `AlertableError` duck-typed protocol (no inheritance required). Configurable via YAML with error categories, severities, and routing rules.

### `cron_runner/` — Cron Job Harness
Thin wrapper (`CronJob`) that wires logging, observability, alerting, and heartbeat for scheduled jobs. Accepts a `work_fn` callback. Supports external schedulers (Railway, cron, EventBridge) or embedded scheduling via `timedelta`. Writes heartbeat JSON for uptime monitoring.

### `domain_types/` — Shared Domain Enums
Generic, project-agnostic enums reusable across unrelated services: `DifficultyLevel`, `SessionStatus`, `GenerationRunStatus`, `EducationLevel`, `FeedbackCategory`, `FeedbackStatus`. No business logic. Application-specific enums (e.g. `QuestionType`, `TestStatus`, `NotificationType`) belong in the consuming application, not here.

## Key Patterns

- **Configuration-driven**: YAML configs with `${VAR:default}` env substitution used across observability, alerting, and cron modules
- **Duck-typed protocols**: `AlertableError` avoids requiring inheritance
- **Singleton observability**: `observability = ObservabilityFacade()` in `__init__.py` — one instance manages all backends
- **Optional extras**: The `[observability]` pip extra adds Sentry SDK, OpenTelemetry, OTLP/Prometheus exporters

## Tests

Test files live in `*/tests/` within each module. The `observability/` module has its own `pytest.ini` defining `integration` and `slow` markers. No root-level conftest.

<!-- tusk-task-tools -->
## Tusk Task Lookup

**Do NOT use Claude Code's built-in `TaskList`, `TaskGet`, or `TaskUpdate` tools to look up or manage tasks.** Those tools manage background agent subprocesses, not tusk tasks.

Use the tusk CLI instead:
- `tusk task-list` — list tasks
- `tusk task-get <id>` — get a task by ID (accepts `506` or `TASK-506`)
- `tusk task-update <id>` — update a task
