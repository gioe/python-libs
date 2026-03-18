"""Centralized logging configuration for AIQ services.

This module provides structured logging setup with support for console,
file, and JSON-formatted logging for monitoring systems.

This module has no dependencies on any AIQ service package — it can be
imported by any service that sets PYTHONPATH to include the repo root.
"""

import json
import logging
import logging.handlers
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Optional

# Context variable for request ID correlation across async tasks.
# Set by request-logging middleware; propagates to all log entries within
# an async request context, enabling log correlation across services.
request_id_context: ContextVar[Optional[str]] = ContextVar("request_id", default=None)

# Cache OpenTelemetry trace module at import time to avoid per-log-line overhead.
# Set to None if the package is not installed, so we skip the OTel path entirely.
_otel_trace: Optional[ModuleType]
try:
    from opentelemetry import trace as _otel_trace
except ImportError:
    _otel_trace = None


class JSONFormatter(logging.Formatter):
    """Custom formatter that outputs log records as JSON.

    Produces structured log entries with consistent fields for log aggregation.
    Includes optional request_id correlation, OpenTelemetry trace context,
    and HTTP-specific fields set by request-logging middleware.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON string."""
        log_data: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add request_id from context if available
        request_id = request_id_context.get()
        if request_id:
            log_data["request_id"] = request_id

        # Add OpenTelemetry trace_id and span_id if available
        if _otel_trace is not None:
            span = _otel_trace.get_current_span()
            span_context = span.get_span_context()
            if span_context.is_valid:
                log_data["trace_id"] = format(span_context.trace_id, "032x")
                log_data["span_id"] = format(span_context.span_id, "016x")

        # Add HTTP-specific fields set by request-logging middleware
        for field in ("method", "path", "status_code", "duration_ms", "client_host", "user_identifier"):
            if hasattr(record, field):
                log_data[field] = getattr(record, field)

        # Add source location for error-level logs
        if record.levelno >= logging.ERROR:
            log_data["source"] = f"{record.pathname}:{record.lineno}"

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # Add extra fields if present (legacy LogContext support)
        if hasattr(record, "extra"):
            log_data["extra"] = record.extra

        return json.dumps(log_data, default=str)


class ColoredFormatter(logging.Formatter):
    """Custom formatter that adds colors to console output."""

    # ANSI color codes
    COLORS = {
        "DEBUG": "\033[36m",  # Cyan
        "INFO": "\033[32m",  # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",  # Red
        "CRITICAL": "\033[35m",  # Magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        """Format log record with colors."""
        levelname = record.levelname
        if levelname in self.COLORS:
            record.levelname = f"{self.COLORS[levelname]}{levelname}{self.RESET}"

        formatted = super().format(record)

        # Reset levelname for subsequent formatters
        record.levelname = levelname

        return formatted


def setup_logging(
    log_level: Optional[str] = "INFO",
    log_file: Optional[str] = "./logs/app.log",
    json_format: bool = False,
    enable_file_logging: bool = True,
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
) -> None:
    """Configure logging for an AIQ service.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
            Defaults to "INFO".
        log_file: Path to log file. Defaults to "./logs/app.log".
        json_format: Whether to use JSON format for logs
        enable_file_logging: Whether to enable file logging
        max_bytes: Maximum size of log file before rotation
        backup_count: Number of backup log files to keep

    Raises:
        ValueError: If log_level is invalid
    """
    # Validate log level
    numeric_level = getattr(logging, (log_level or "INFO").upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {log_level}")

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Remove existing handlers
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)

    console_formatter: logging.Formatter
    if json_format:
        console_formatter = JSONFormatter()
    else:
        console_format = (
            "%(asctime)s - %(name)s - %(levelname)s - "
            "%(module)s:%(funcName)s:%(lineno)d - %(message)s"
        )
        console_formatter = ColoredFormatter(console_format)

    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # File handler (if enabled)
    if enable_file_logging:
        resolved_log_file = log_file or "./logs/app.log"

        # Create logs directory if it doesn't exist
        log_path = Path(resolved_log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Rotating file handler
        file_handler = logging.handlers.RotatingFileHandler(
            resolved_log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
        )
        file_handler.setLevel(numeric_level)

        # Always use JSON format for file logs (better for parsing)
        file_formatter = JSONFormatter()
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)

        root_logger.info(f"File logging enabled: {resolved_log_file}")

    # Clamp third-party loggers to WARNING; relax only when the root level is
    # explicitly DEBUG (e.g. via --verbose) so low-level networking traces are
    # surfaced without introducing INFO noise in normal runs.
    third_party_level = (
        numeric_level if numeric_level <= logging.DEBUG else logging.WARNING
    )
    for noisy_logger in (
        "httpcore",
        "httpx",
        "anthropic",
        "openai",
        "opentelemetry",
        "urllib3",
    ):
        logging.getLogger(noisy_logger).setLevel(third_party_level)

    # Hard-clamp HTTP internals to WARNING unless the root level is DEBUG.
    # These sub-loggers emit full exc_info stack traces at DEBUG for every 429
    # retry; the information is already captured by our own loggers. When the
    # root level is DEBUG (e.g. --verbose), the clamp is skipped so low-level
    # HTTP traces remain available for diagnosing retries across all call sites
    # (embeddings, generation, judge LLM calls, etc.).
    if numeric_level > logging.DEBUG:
        always_warning = (
            "openai._base_client",
            "httpcore.http11",
            "httpcore.connection",
        )
        for noisy_sub_logger in always_warning:
            logging.getLogger(noisy_sub_logger).setLevel(logging.WARNING)

    root_logger.info(
        f"Logging configured: level={log_level}, "
        f"file_logging={enable_file_logging}, "
        f"json_format={json_format}"
    )


def get_logger(name: str) -> logging.Logger:
    """Get a configured logger instance.

    Args:
        name: Logger name (typically __name__ from calling module)

    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)


class LogContext:
    """Context manager for adding extra context to log records.

    Example:
        with LogContext(request_id="123", user_id="456"):
            logger.info("Processing request")
    """

    def __init__(self, **kwargs: Any):
        self.extra = kwargs
        self.old_factory = logging.getLogRecordFactory()

    def __enter__(self) -> "LogContext":
        def record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = self.old_factory(*args, **kwargs)
            record.extra = self.extra
            return record

        logging.setLogRecordFactory(record_factory)
        return self

    def __exit__(self, *args: Any) -> None:
        logging.setLogRecordFactory(self.old_factory)
