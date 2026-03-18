"""Unified observability abstraction for AIQ services.

This package provides a single API for application-level instrumentation
that routes to the appropriate backend systems:
- Errors → Sentry (superior alerting, grouping, and debugging UX)
- Metrics → OpenTelemetry/Prometheus (for Grafana dashboards)
- Traces → Configurable (OTEL, Sentry Performance, or both)

Usage:
    from libs.observability import observability

    # Initialize at application startup
    observability.init(
        config_path="config/observability.yaml",
        service_name="my-service",
    )

    # Capture errors (routed to Sentry)
    try:
        risky_operation()
    except Exception as e:
        observability.capture_error(e, context={"operation": "risky"})
        raise

    # Record metrics (routed to OTEL/Prometheus)
    observability.record_metric(
        name="requests.processed",
        value=1,
        labels={"endpoint": "/api/test"},
        metric_type="counter",
    )

    # Distributed tracing
    with observability.start_span("process_request") as span:
        span.set_attribute("user_id", user.id)
        # ... do work

    # Record structured events
    observability.record_event(
        "user.signup",
        data={"user_id": "123", "method": "oauth"},
        tags={"source": "web"},
    )

    # Set user context for error tracking
    observability.set_user("user-123", username="alice", email="alice@example.com")

    # Set additional context
    observability.set_context("request", {"url": "/api/test", "method": "POST"})

Security:
    **Warning: Debug logging may expose sensitive data.**

    When Python's logging level is set to DEBUG, this module logs internal
    operations that may include context data, event payloads, and user
    information. Never enable DEBUG logging in production environments.

    See ``libs.observability.facade`` module docstring for detailed security
    guidance on:

    - PII exposure risks in event data and context
    - Sensitivity of debug logs
    - Best practices for log sanitization

    Key recommendations:

    1. Use INFO or WARNING log level in production
    2. Sanitize PII before passing to observability methods
    3. Use opaque identifiers (user_id) instead of PII (email, name)
    4. Audit observability calls during code review
"""

from .facade import ObservabilityFacade, SpanContext

# Singleton instance for application use
observability = ObservabilityFacade()

__all__ = ["observability", "ObservabilityFacade", "SpanContext"]
