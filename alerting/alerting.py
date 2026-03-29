"""Alert notification system for critical errors and inventory monitoring.

This module provides functionality to send alerts via email and other channels
when critical errors occur in a pipeline, including low inventory alerts.

This module has no service-specific dependencies — it can be
imported by any service that sets PYTHONPATH to include the repo root.
"""

import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, runtime_checkable

import yaml


def _format_value(value: Any) -> str:
    """Format a notification field value as a human-readable string."""
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error category / severity enums (canonical definitions for all services)
# ---------------------------------------------------------------------------


class ErrorCategory(str, Enum):
    """Categories of errors. Generic values reusable across services.

    Clients may also pass arbitrary strings for categories not listed here —
    ``AlertableError.category`` accepts any ``str``-compatible value.
    """

    AUTHENTICATION = "authentication"  # API key invalid or expired
    INVALID_REQUEST = "invalid_request"  # Malformed request or invalid parameters
    SERVER_ERROR = "server_error"  # Provider server errors (5xx)
    NETWORK_ERROR = "network_error"  # Connection/timeout errors
    RESOURCE_LOW = "resource_low"  # A managed resource (inventory, quota, etc.) is low
    INVENTORY_LOW = "inventory_low"  # Specific resource inventory below threshold
    BILLING_QUOTA = "billing_quota"  # Provider billing quota or funds exhausted
    JOB_FAILURE = "job_failure"  # A scheduled or background job failed
    SCRIPT_FAILURE = "script_failure"  # A script or batch process failed
    UNKNOWN = "unknown"  # Unclassified errors


class ErrorSeverity(str, Enum):
    """Severity levels for errors."""

    CRITICAL = "critical"  # Requires immediate attention (e.g., billing)
    HIGH = "high"  # Important but not blocking (e.g., rate limits)
    MEDIUM = "medium"  # Should be addressed (e.g., invalid requests)
    LOW = "low"  # Informational (e.g., temporary network issues)


# ---------------------------------------------------------------------------
# AlertableError Protocol — duck-typed error accepted by AlertManager
# ---------------------------------------------------------------------------


@runtime_checkable
class AlertableError(Protocol):
    """Protocol for errors that can be passed to AlertManager.send_alert().

    Any object with these attributes satisfies the protocol without needing
    to inherit from a specific class.
    """

    category: Any  # ErrorCategory or string-compatible value
    severity: Any  # ErrorSeverity or string-compatible value
    provider: str
    original_error: str
    message: str
    is_retryable: bool

    def to_dict(self) -> Dict[str, Any]:
        """Return a dict representation of the error."""
        ...


# ---------------------------------------------------------------------------
# AlertError — simple concrete error type for use within libs
# ---------------------------------------------------------------------------


@dataclass
class AlertError:
    """Concrete error model for use within libs (no external dependencies)."""

    category: ErrorCategory
    severity: ErrorSeverity
    provider: str
    original_error: str
    message: str
    is_retryable: bool = False
    status_code: Optional[int] = None
    quota_details: Optional[Dict[str, Any]] = None
    recommended_actions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Return a dict representation of the error."""
        result: Dict[str, Any] = {
            "category": str(self.category),
            "severity": str(self.severity),
            "provider": self.provider,
            "original_error": self.original_error,
            "message": self.message,
            "is_retryable": self.is_retryable,
            "status_code": self.status_code,
        }
        if self.quota_details:
            result["quota_details"] = self.quota_details
        if self.recommended_actions:
            result["recommended_actions"] = self.recommended_actions
        return result


# ---------------------------------------------------------------------------
# RunSummary — generic dict returned by CronJob work functions
# ---------------------------------------------------------------------------

# RunSummary is a plain dict.  Any keys may be present; CronJob converts
# them to (label, value) tuples and passes them to send_notification().
RunSummary = Dict[str, Any]


@dataclass
class ResourceStatus:
    """Generic resource status returned by a ResourceMonitor check_fn callback."""

    name: str
    count: int
    metadata: Dict[str, Any] = field(default_factory=dict)


class AlertManager:
    """Manages alert notifications for critical errors."""

    # Maximum number of alerts to retain in memory
    MAX_ALERTS_RETENTION = 1000

    # Discord color codes (decimal)
    DISCORD_COLOR_SUCCESS = 0x28A745   # Green
    DISCORD_COLOR_WARNING = 0xFFC107   # Yellow
    DISCORD_COLOR_CRITICAL = 0xDC3545  # Red

    # Cooldown between Discord alerts for the same provider (seconds)
    DISCORD_COOLDOWN_SECONDS = 600  # 10 minutes

    # Timeout for Discord webhook HTTP requests (seconds)
    DISCORD_HTTP_TIMEOUT = 10

    def __init__(
        self,
        alert_file_path: Optional[str] = None,
        discord_webhook_url: Optional[str] = None,
        service_name: str = "Alerting Service",
    ):
        """Initialize alert manager.

        Args:
            alert_file_path: Path to file for logging critical alerts
            discord_webhook_url: Discord webhook URL for circuit breaker / quota alerts
            service_name: Name of the service shown in alert templates
        """
        self.alert_file_path = alert_file_path
        self.discord_webhook_url = discord_webhook_url
        self.service_name = service_name

        # Track last Discord alert time per provider for cooldown enforcement
        self._discord_cooldowns: Dict[str, float] = {}

        # Track alerts sent (capped to prevent unbounded memory growth)
        self.alerts_sent: List[dict] = []

        logger.info(
            f"AlertManager initialized: alert_file={bool(self.alert_file_path)}, "
            f"discord_enabled={bool(self.discord_webhook_url)}"
        )

    # ------------------------------------------------------------------
    # Discord alerting
    # ------------------------------------------------------------------

    def _is_discord_cooldown_active(self, provider: str) -> bool:
        """Return True if a Discord alert was sent for this provider within the cooldown window."""
        last_sent = self._discord_cooldowns.get(provider)
        return last_sent is not None and (
            time.time() - last_sent < self.DISCORD_COOLDOWN_SECONDS
        )

    def _send_discord_alert(
        self,
        title: str,
        description: str,
        color: int,
        fields: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """Send a Discord embed via webhook using stdlib urllib.

        Args:
            title: Embed title
            description: Embed description
            color: Embed color as decimal integer
            fields: Optional list of embed fields ({name, value, inline})

        Returns:
            True if the request succeeded (HTTP 2xx)
        """
        if not self.discord_webhook_url:
            return False

        embed: Dict[str, Any] = {
            "title": title,
            "description": description,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if fields:
            embed["fields"] = fields

        payload = json.dumps({"embeds": [embed]}).encode("utf-8")
        req = urllib.request.Request(
            url=self.discord_webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.DISCORD_HTTP_TIMEOUT):
                pass  # Discord returns 204 No Content on success
            return True
        except Exception as exc:
            logger.warning(f"Discord webhook request failed: {exc}")
            return False

    def send_circuit_breaker_alert(self, provider_name: str, reason: str) -> bool:
        """Send a Discord alert when a circuit breaker opens (CLOSED→OPEN).

        Alerts are suppressed if a Discord alert for the same provider was sent
        within the last DISCORD_COOLDOWN_SECONDS (10 minutes).

        Args:
            provider_name: Provider whose circuit breaker opened
            reason: Human-readable reason for the transition

        Returns:
            True if the alert was sent, False if suppressed or disabled
        """
        if not self.discord_webhook_url:
            logger.debug(
                f"Discord webhook not configured; skipping circuit breaker alert for {provider_name}"
            )
            return False

        cooldown_key = f"cb:{provider_name}"
        if self._is_discord_cooldown_active(cooldown_key):
            logger.debug(
                f"Discord circuit breaker alert suppressed for {provider_name} (cooldown active)"
            )
            return False

        title = f"\u26a0\ufe0f Circuit Breaker OPEN: {provider_name}"
        description = (
            f"The circuit breaker for **{provider_name}** has opened. "
            "Requests to this provider are now failing fast until the recovery timeout elapses."
        )
        fields = [
            {"name": "Provider", "value": provider_name, "inline": True},
            {"name": "Reason", "value": reason, "inline": False},
        ]
        try:
            sent = self._send_discord_alert(
                title=title,
                description=description,
                color=self.DISCORD_COLOR_CRITICAL,
                fields=fields,
            )
        except Exception:
            logger.warning(
                f"Unexpected error sending Discord circuit breaker alert for {provider_name}",
                exc_info=True,
            )
            return False
        if sent:
            self._discord_cooldowns[cooldown_key] = time.time()
            logger.info(f"Discord circuit breaker alert sent for {provider_name}")
        return sent

    def _send_critical_discord_alert(
        self,
        classified_error: "AlertableError",
        context: Optional[str] = None,
    ) -> bool:
        """Send a Discord alert for a CRITICAL severity error.

        Uses a 10-minute per-provider cooldown (shared with circuit breaker alerts).

        Args:
            classified_error: The CRITICAL error to alert on
            context: Additional context string

        Returns:
            True if sent, False if suppressed or Discord not configured
        """
        if not self.discord_webhook_url:
            return False

        cooldown_key = f"critical:{classified_error.provider}"
        if self._is_discord_cooldown_active(cooldown_key):
            logger.debug(
                f"Discord critical alert suppressed for {classified_error.provider} (cooldown active)"
            )
            return False

        category_val = (
            classified_error.category.value
            if hasattr(classified_error.category, "value")
            else str(classified_error.category)
        )
        title = f"\U0001f6a8 Critical Alert: {category_val.replace('_', ' ').title()} ({classified_error.provider})"
        description = classified_error.message
        if context:
            description += f"\n\n{context}"
        severity_str = (
            classified_error.severity.value.upper()
            if hasattr(classified_error.severity, "value")
            else str(classified_error.severity).upper()
        )
        fields: List[Dict[str, Any]] = [
            {"name": "Provider", "value": classified_error.provider, "inline": True},
            {
                "name": "Severity",
                "value": severity_str,
                "inline": True,
            },
        ]
        sent = self._send_discord_alert(
            title=title,
            description=description,
            color=self.DISCORD_COLOR_CRITICAL,
            fields=fields,
        )
        if sent:
            self._discord_cooldowns[cooldown_key] = time.time()
            logger.info(
                f"Discord critical alert sent for {classified_error.provider}"
            )
        return sent

    def send_alert(
        self,
        classified_error: "AlertableError",
        context: Optional[str] = None,
    ) -> bool:
        """Send an alert for a classified error.

        Accepts any object satisfying the AlertableError protocol (duck typing).

        Args:
            classified_error: The classified error to alert on
            context: Additional context about the error

        Returns:
            True if alert was sent successfully
        """
        # Build alert message
        alert_message = self._build_alert_message(classified_error, context)

        success = True

        # Write to alert file if configured
        if self.alert_file_path:
            try:
                self._write_alert_file(classified_error, alert_message)
                logger.info(f"Alert written to file: {self.alert_file_path}")
            except Exception as e:
                logger.error(f"Failed to write alert file: {e}")
                success = False

        # Send Discord alert for CRITICAL severity errors
        severity_val = (
            classified_error.severity.value
            if hasattr(classified_error.severity, "value")
            else str(classified_error.severity)
        )
        if severity_val == ErrorSeverity.CRITICAL:
            self._send_critical_discord_alert(classified_error, context)

        # Track alert (with bounded memory)
        self.alerts_sent.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "error": classified_error.to_dict(),
                "context": context,
                "success": success,
            }
        )

        # Trim to max retention to prevent unbounded memory growth
        if len(self.alerts_sent) > self.MAX_ALERTS_RETENTION:
            self.alerts_sent = self.alerts_sent[-self.MAX_ALERTS_RETENTION :]

        return success

    def send_notification(
        self,
        title: str,
        fields: List[Tuple[str, Any]],
        severity: str = "info",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Send a generic notification via Discord.

        Discord errors are caught and logged; they never raise to the caller.

        Args:
            title: Notification title (used as Discord embed title).
            fields: List of (label, value) tuples rendered as the main content table.
                Values may be strings, numbers, dicts, or lists — all are formatted
                as human-readable strings automatically.
            severity: Controls color styling. One of ``"info"`` (green),
                ``"warning"`` (yellow), or ``"critical"`` (red).
            metadata: Optional dict rendered as a secondary section at the end of
                the Discord embed.
        """
        _VALID_SEVERITIES = {"info", "warning", "critical"}
        if severity.lower() not in _VALID_SEVERITIES:
            logger.warning(
                "send_notification() received unknown severity %r; defaulting to 'info'. "
                "Valid values: %s",
                severity,
                ", ".join(sorted(_VALID_SEVERITIES)),
            )

        if self.discord_webhook_url:
            color = {
                "info": self.DISCORD_COLOR_SUCCESS,
                "warning": self.DISCORD_COLOR_WARNING,
                "critical": self.DISCORD_COLOR_CRITICAL,
            }.get(severity.lower(), self.DISCORD_COLOR_SUCCESS)

            discord_fields: List[Dict[str, Any]] = [
                {"name": str(label), "value": _format_value(value), "inline": True}
                for label, value in fields
            ]
            if metadata:
                meta_lines = "\n".join(f"{k}: {v}" for k, v in metadata.items())
                discord_fields.append(
                    {"name": "Details", "value": meta_lines, "inline": False}
                )

            try:
                self._send_discord_alert(
                    title=title,
                    description="",
                    color=color,
                    fields=discord_fields or None,
                )
            except Exception:
                logger.warning("Discord notification failed", exc_info=True)

    def send_run_completion(
        self,
        exit_code: int,
        run_summary: Optional[RunSummary] = None,
    ) -> None:
        """Send a run-completion notification via Discord.

        Discord is sent whenever ``discord_webhook_url`` is configured.
        Errors are caught and logged at WARNING/ERROR level — they never
        propagate to the caller or affect the job's exit code.

        Args:
            exit_code: Run exit code (0=success, 1=partial failure, 2+=failure)
            run_summary: Optional generic run summary dict with standard keys:
                generated (int), inserted (int), errors (int),
                duration_seconds (float), details (dict of extra fields).
        """
        run_summary = run_summary or {}

        if self.discord_webhook_url:
            title, description, color, fields = self._format_run_summary_embed(
                exit_code, run_summary
            )
            try:
                self._send_discord_alert(title, description, color, fields)
            except Exception:
                logger.warning("Discord run-completion notification failed", exc_info=True)

    def _format_run_summary_embed(
        self,
        exit_code: int,
        run_summary: RunSummary,
    ) -> tuple:
        """Return (title, description, color, fields) for a run-completion Discord embed.

        Args:
            exit_code: Run exit code (0=success, 1=partial failure, 2+=failure)
            run_summary: Generic run summary dict (standard keys + details)
        """
        if exit_code == 0:
            title = "\u2705 Question Generation: Success"
            color = self.DISCORD_COLOR_SUCCESS
        elif exit_code == 1:
            title = "\u26a0\ufe0f Question Generation: Partial Failure"
            color = self.DISCORD_COLOR_WARNING
        else:
            title = f"\u274c Question Generation: Failed (exit {exit_code})"
            color = self.DISCORD_COLOR_CRITICAL

        details: Dict[str, Any] = run_summary.get("details", {})
        generated = run_summary.get("generated")
        inserted = run_summary.get("inserted")
        duration_seconds = run_summary.get("duration_seconds")
        questions_requested = details.get("questions_requested")
        duplicates_found = details.get("duplicates_found")
        approval_rate = details.get("approval_rate")
        by_type: Dict[str, int] = details.get("by_type", {})
        by_difficulty: Dict[str, int] = details.get("by_difficulty", {})

        description = (
            f"Run completed in {duration_seconds:.1f}s"
            if duration_seconds is not None
            else "Run completed"
        )

        fields: List[Dict[str, Any]] = []

        if generated is not None:
            if questions_requested:
                pct = int(generated / questions_requested * 100)
                fields.append(
                    {"name": "Generated", "value": f"{generated} / {questions_requested} ({pct}%)", "inline": True}
                )
            else:
                fields.append({"name": "Generated", "value": str(generated), "inline": True})

        if inserted is not None:
            fields.append({"name": "Inserted", "value": str(inserted), "inline": True})

        if approval_rate is not None and generated is not None:
            approved = round(generated * approval_rate / 100) if approval_rate else 0
            fields.append(
                {"name": "Approved", "value": f"{approved} / {generated} ({approval_rate:.1f}%)", "inline": True}
            )
        elif approval_rate is not None:
            fields.append({"name": "Approval Rate", "value": f"{approval_rate:.1f}%", "inline": True})

        if duplicates_found is not None:
            fields.append({"name": "Duplicates", "value": f"{duplicates_found} found", "inline": True})

        if by_type:
            fields.append(
                {"name": "By Type", "value": ", ".join(f"{k}: {v}" for k, v in sorted(by_type.items())), "inline": False}
            )

        if by_difficulty:
            fields.append(
                {"name": "By Difficulty", "value": " \u00b7 ".join(f"{k}: {v}" for k, v in sorted(by_difficulty.items())), "inline": False}
            )

        return title, description, color, fields

    def _build_alert_message(
        self,
        classified_error: "AlertableError",
        context: Optional[str] = None,
    ) -> str:
        """Build formatted alert message."""
        category_val = (
            classified_error.category.value
            if hasattr(classified_error.category, "value")
            else str(classified_error.category)
        )
        severity_val = (
            classified_error.severity.value
            if hasattr(classified_error.severity, "value")
            else str(classified_error.severity)
        )
        original_error = getattr(classified_error, "original_error", "")

        lines = [
            f"ALERT: {category_val.upper()}",
            f"Severity: {severity_val.upper()}",
            f"Provider: {classified_error.provider}",
            f"Time: {datetime.now(timezone.utc).isoformat()}",
            "",
            f"Message: {classified_error.message}",
            "",
            f"Original Error: {original_error}",
        ]

        if context:
            lines.extend(["", f"Context: {context}"])

        if classified_error.is_retryable:
            lines.extend(["", "Note: This error may be transient and retryable."])
        else:
            lines.extend(["", "Note: This error requires manual intervention."])

        # Add action items: use caller-supplied actions if present, else category defaults
        caller_actions = getattr(classified_error, "recommended_actions", [])
        lines.extend(["", "Recommended Actions:"])

        if caller_actions:
            for i, action in enumerate(caller_actions, start=1):
                lines.append(f"{i}. {action}")
        elif category_val == ErrorCategory.AUTHENTICATION:
            lines.extend(
                [
                    f"1. Verify {classified_error.provider} API key is correct",
                    "2. Check if API key has expired",
                    "3. Regenerate API key if necessary",
                    "4. Update environment variables with new key",
                ]
            )
        elif category_val == ErrorCategory.BILLING_QUOTA:
            lines.extend(
                [
                    f"1. Check your {classified_error.provider} account balance and update billing information",
                    "2. Verify payment method is current",
                    "3. Contact provider support if billing issue persists",
                ]
            )
        elif category_val == ErrorCategory.INVENTORY_LOW:
            lines.extend(
                [
                    "1. Review resource inventory levels and replenish as needed",
                    "2. Investigate root cause of inventory depletion",
                    "3. Adjust thresholds or generation parameters if needed",
                ]
            )
        elif category_val == ErrorCategory.SCRIPT_FAILURE:
            lines.extend(
                [
                    f"1. Check {classified_error.provider} script logs for error details",
                    "2. Review recent changes that may have caused the failure",
                    "3. Re-run individual failed components if applicable",
                ]
            )
        else:
            lines.extend(
                [
                    "1. Review error details above",
                    "2. Check provider status page",
                    "3. Review application logs for more context",
                ]
            )

        return "\n".join(lines)

    def _write_alert_file(
        self,
        classified_error: "AlertableError",
        alert_message: str,
    ) -> None:
        """Write alert to file."""
        timestamp = datetime.now(timezone.utc).isoformat()

        alert_entry = f"""
{'=' * 80}
TIMESTAMP: {timestamp}
{alert_message}
{'=' * 80}

"""

        if self.alert_file_path is None:
            raise ValueError("Alert file path must be configured")

        alert_path = Path(self.alert_file_path)
        alert_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.alert_file_path, "a") as f:
            f.write(alert_entry)

    def get_alerts_summary(self) -> dict:
        """Get summary of alerts sent."""
        if not self.alerts_sent:
            return {
                "total_alerts": 0,
                "successful": 0,
                "failed": 0,
                "by_category": {},
                "by_severity": {},
            }

        successful = sum(1 for a in self.alerts_sent if a["success"])
        failed = len(self.alerts_sent) - successful

        by_category: Dict[str, int] = {}
        by_severity: Dict[str, int] = {}

        for alert in self.alerts_sent:
            category = alert["error"]["category"]
            severity = alert["error"]["severity"]

            by_category[category] = by_category.get(category, 0) + 1
            by_severity[severity] = by_severity.get(severity, 0) + 1

        return {
            "total_alerts": len(self.alerts_sent),
            "successful": successful,
            "failed": failed,
            "by_category": by_category,
            "by_severity": by_severity,
            "alerts": self.alerts_sent,
        }


@dataclass
class AlertingConfig:
    """Configuration for resource alerting loaded from YAML."""

    # Resource thresholds
    healthy_min: int = 50
    warning_min: int = 20
    critical_min: int = 5

    # Cooldown settings (in minutes)
    per_resource_cooldown_minutes: int = 60
    global_cooldown_minutes: int = 15
    max_alerts_per_hour: int = 10

    # Content settings
    include_affected_resources: bool = True
    max_resource_detail: int = 5
    include_recommendations: bool = True

    # Service identification
    service_name: str = "Alerting Service"

    # Email settings — None means "derive from service_name" (resolved in __post_init__)
    subject_prefix_warning: Optional[str] = None
    subject_prefix_critical: Optional[str] = None

    def __post_init__(self) -> None:
        if self.subject_prefix_warning is None:
            self.subject_prefix_warning = f"[{self.service_name}] Resource Warning"
        if self.subject_prefix_critical is None:
            self.subject_prefix_critical = f"[{self.service_name}] CRITICAL: Resource Alert"

    # File logging
    resource_alert_file: str = "./logs/resource_alerts.log"
    log_all_checks: bool = False

    @classmethod
    def from_yaml(cls, config_path: str) -> "AlertingConfig":
        """Load alerting configuration from YAML file."""
        path = Path(config_path)
        if not path.exists():
            logger.warning(
                f"Alerting config not found at {config_path}, using defaults"
            )
            return cls()

        try:
            with open(path) as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            logger.error(f"Invalid YAML in alerting config: {e}")
            logger.warning("Using default alerting configuration")
            return cls()
        except (IOError, OSError) as e:
            logger.error(f"Failed to read alerting config: {e}")
            logger.warning("Using default alerting configuration")
            return cls()

        if not data:
            logger.warning("Empty alerting config, using defaults")
            return cls()

        service_name = data.get("service_name", "Alerting Service")
        resources = data.get("resources", data.get("inventory", {}))
        thresholds = resources.get("thresholds", {})
        cooldown = resources.get("cooldown", {})
        content = resources.get("content", {})
        email = data.get("email", {})
        file_logging = data.get("file_logging", {})

        healthy = thresholds.get("healthy_min", 50)
        warning = thresholds.get("warning_min", 20)
        critical = thresholds.get("critical_min", 5)

        if not (critical < warning < healthy):
            logger.error(
                f"Invalid threshold configuration: critical_min ({critical}) < "
                f"warning_min ({warning}) < healthy_min ({healthy}) required"
            )
            logger.warning("Using default thresholds")
            healthy, warning, critical = 50, 20, 5

        return cls(
            service_name=service_name,
            healthy_min=healthy,
            warning_min=warning,
            critical_min=critical,
            per_resource_cooldown_minutes=cooldown.get("per_resource_minutes", 60),
            global_cooldown_minutes=cooldown.get("global_minutes", 15),
            max_alerts_per_hour=cooldown.get("max_alerts_per_hour", 10),
            include_affected_resources=content.get("include_affected_resources", True),
            max_resource_detail=content.get("max_resource_detail", 5),
            include_recommendations=content.get("include_recommendations", True),
            subject_prefix_warning=email.get("subject_prefix_warning") or None,
            subject_prefix_critical=email.get("subject_prefix_critical") or None,
            resource_alert_file=file_logging.get(
                "resource_alert_file", "./logs/resource_alerts.log"
            ),
            log_all_checks=file_logging.get("log_all_checks", False),
        )


@dataclass
class ResourceMonitorResult:
    """Result of a ResourceMonitor check."""

    # Number of resources that triggered alerts (not number of emails sent —
    # all alertable resources are consolidated into one email per check).
    alerts_sent: int = 0
    alerts_suppressed: int = 0
    resources_checked: int = 0
    critical_resources: List[ResourceStatus] = field(default_factory=list)
    warning_resources: List[ResourceStatus] = field(default_factory=list)
    healthy_resources: int = 0


class ResourceMonitor:
    """Monitors resources using a caller-supplied check_fn callback.

    The client provides a ``check_fn: Callable[[], list[ResourceStatus]]``; the
    library owns cooldown tracking, rate-limiting, threshold evaluation, and
    channel routing.

    Note: This class is NOT thread-safe. It must only be called from a single
    thread. For concurrent access, use external synchronization.
    """

    # Buffer time (in minutes) after cooldown to keep entries before cleanup
    COOLDOWN_CLEANUP_BUFFER_MINUTES = 60

    def __init__(
        self,
        check_fn: Callable[[], List[ResourceStatus]],
        alert_manager: AlertManager,
        config: Optional[AlertingConfig] = None,
    ):
        """Initialize resource monitor.

        Args:
            check_fn: Callable that returns the current list of ResourceStatus objects.
            alert_manager: AlertManager used to send alerts via configured channels.
            config: AlertingConfig with thresholds and cooldown settings.
        """
        self.check_fn = check_fn
        self.alert_manager = alert_manager
        self.config = config or AlertingConfig()
        # Propagate service_name from config so all alert templates are consistent
        self.alert_manager.service_name = self.config.service_name

        self._resource_last_alert: Dict[str, datetime] = {}
        self._global_last_alert: Optional[datetime] = None
        self._alerts_this_hour: List[datetime] = []

        logger.info(
            f"ResourceMonitor initialized: "
            f"critical_min={self.config.critical_min}, "
            f"warning_min={self.config.warning_min}, "
            f"cooldown={self.config.per_resource_cooldown_minutes}min"
        )

    def _cleanup_old_cooldowns(self, now: datetime) -> None:
        """Remove cooldown entries older than cooldown period plus buffer."""
        cutoff = now - timedelta(
            minutes=self.config.per_resource_cooldown_minutes
            + self.COOLDOWN_CLEANUP_BUFFER_MINUTES
        )

        keys_to_remove = [
            key
            for key, last_alert in self._resource_last_alert.items()
            if last_alert < cutoff
        ]

        for key in keys_to_remove:
            del self._resource_last_alert[key]

        if keys_to_remove:
            logger.debug(f"Cleaned up {len(keys_to_remove)} old cooldown entries")

    def check_and_alert(self) -> ResourceMonitorResult:
        """Invoke check_fn and send alerts for resources below thresholds."""
        try:
            resources = self.check_fn()
        except Exception as exc:
            logger.error(f"ResourceMonitor check_fn raised an exception: {exc}", exc_info=True)
            return ResourceMonitorResult()
        result = ResourceMonitorResult(resources_checked=len(resources))
        now = datetime.now(timezone.utc)

        # Warn about duplicate names — cooldown tracking is keyed on name, so
        # duplicates cause last-write-wins suppression that can mask alert bugs.
        seen_names: set = set()
        for resource in resources:
            if resource.name in seen_names:
                logger.warning(
                    f"ResourceMonitor: duplicate resource name {resource.name!r} "
                    "returned by check_fn — cooldown tracking requires unique names."
                )
            seen_names.add(resource.name)

        self._cleanup_old_cooldowns(now)

        critical_resources: List[ResourceStatus] = []
        warning_resources: List[ResourceStatus] = []

        for resource in resources:
            if resource.count < self.config.critical_min:
                critical_resources.append(resource)
            elif resource.count < self.config.warning_min:
                warning_resources.append(resource)
            else:
                result.healthy_resources += 1

        result.critical_resources = critical_resources
        result.warning_resources = warning_resources

        if self.config.log_all_checks:
            self._log_resource_check(result)

        if critical_resources:
            alerts_sent, alerts_suppressed = self._send_resource_alerts(
                resources=critical_resources,
                severity=ErrorSeverity.CRITICAL,
                now=now,
            )
            result.alerts_sent += alerts_sent
            result.alerts_suppressed += alerts_suppressed

        if warning_resources:
            alerts_sent, alerts_suppressed = self._send_resource_alerts(
                resources=warning_resources,
                severity=ErrorSeverity.HIGH,
                now=now,
            )
            result.alerts_sent += alerts_sent
            result.alerts_suppressed += alerts_suppressed

        logger.info(
            f"Resource monitor check complete: "
            f"{result.alerts_sent} alerts sent, "
            f"{result.alerts_suppressed} suppressed by cooldown"
        )

        return result

    def _send_resource_alerts(
        self,
        resources: List[ResourceStatus],
        severity: ErrorSeverity,
        now: datetime,
    ) -> Tuple[int, int]:
        """Send alerts for a list of resources with the given severity."""
        alerts_sent = 0
        alerts_suppressed = 0

        alertable: List[ResourceStatus] = []
        for resource in resources:
            if self._is_in_cooldown(resource.name, now):
                alerts_suppressed += 1
                logger.debug(f"Alert suppressed for {resource.name!r} (in cooldown)")
            else:
                alertable.append(resource)

        if not alertable:
            return alerts_sent, alerts_suppressed

        if self._is_global_cooldown_active(now):
            logger.info(
                f"Global cooldown active, suppressing {len(alertable)} alerts"
            )
            return alerts_sent, alerts_suppressed + len(alertable)

        if not self._check_hourly_rate_limit(now):
            logger.warning(
                f"Hourly alert limit reached ({self.config.max_alerts_per_hour}), "
                f"suppressing {len(alertable)} alerts"
            )
            return alerts_sent, alerts_suppressed + len(alertable)

        threshold = (
            self.config.critical_min
            if severity == ErrorSeverity.CRITICAL
            else self.config.warning_min
        )
        alert_error = self._build_resource_error(alertable, severity, threshold)
        context = self._build_resource_context(alertable, threshold)

        success = self.alert_manager.send_alert(alert_error, context)

        if success:
            alerts_sent = len(alertable)
            for resource in alertable:
                self._resource_last_alert[resource.name] = now
            self._global_last_alert = now
            self._alerts_this_hour.append(now)

            self._write_resource_alert_file(alertable, severity)
        else:
            alerts_suppressed += len(alertable)

        return alerts_sent, alerts_suppressed

    def _is_in_cooldown(self, name: str, now: datetime) -> bool:
        """Check if a resource is in cooldown period."""
        last_alert = self._resource_last_alert.get(name)
        if last_alert is None:
            return False

        cooldown_delta = timedelta(minutes=self.config.per_resource_cooldown_minutes)
        return now < last_alert + cooldown_delta

    def _is_global_cooldown_active(self, now: datetime) -> bool:
        """Check if global cooldown is active."""
        if self._global_last_alert is None:
            return False

        cooldown_delta = timedelta(minutes=self.config.global_cooldown_minutes)
        return now < self._global_last_alert + cooldown_delta

    def _check_hourly_rate_limit(self, now: datetime) -> bool:
        """Check if we're under the hourly rate limit."""
        one_hour_ago = now - timedelta(hours=1)
        self._alerts_this_hour = [
            ts for ts in self._alerts_this_hour if ts > one_hour_ago
        ]

        return len(self._alerts_this_hour) < self.config.max_alerts_per_hour

    def _build_resource_error(
        self,
        resources: List[ResourceStatus],
        severity: ErrorSeverity,
        threshold: int,
    ) -> AlertError:
        """Build an AlertError for resource alerts."""
        severity_word = "critical" if severity == ErrorSeverity.CRITICAL else "low"

        message = (
            f"{len(resources)} resources have {severity_word} levels "
            f"(below {threshold}). Replenishment may be needed."
        )

        recommended_actions: List[str] = []
        if self.config.include_recommendations:
            recommended_actions = [
                "Review relevant logs for any failures",
                "Check provider API quotas and billing",
                "Review application logs for more context",
            ]

        return AlertError(
            category=ErrorCategory.RESOURCE_LOW,
            severity=severity,
            provider="resource-monitor",
            original_error="LowResourceLevel",
            message=message,
            is_retryable=True,
            recommended_actions=recommended_actions,
        )

    def _build_resource_context(
        self, resources: List[ResourceStatus], threshold: int
    ) -> str:
        """Build context string with affected resource details."""
        if not self.config.include_affected_resources:
            return ""

        lines = ["Affected resources:"]

        sorted_resources = sorted(resources, key=lambda r: r.count)

        for resource in sorted_resources[: self.config.max_resource_detail]:
            lines.append(
                f"  - {resource.name}: {resource.count} (threshold: {threshold})"
            )

        remaining = len(resources) - self.config.max_resource_detail
        if remaining > 0:
            lines.append(f"  ... and {remaining} more resources")

        return "\n".join(lines)

    def _log_resource_check(self, result: ResourceMonitorResult) -> None:
        """Log resource check results to file."""
        try:
            log_path = Path(self.config.resource_alert_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now(timezone.utc).isoformat()
            entry = {
                "timestamp": timestamp,
                "type": "resource_check",
                "resources_checked": result.resources_checked,
                "healthy_resources": result.healthy_resources,
                "warning_resources": len(result.warning_resources),
                "critical_resources": len(result.critical_resources),
            }

            with open(log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except (IOError, OSError) as e:
            logger.error(f"Failed to log resource check: {e}")

    def _write_resource_alert_file(
        self,
        resources: List[ResourceStatus],
        severity: ErrorSeverity,
    ) -> None:
        """Write resource alert to dedicated alert file."""
        try:
            log_path = Path(self.config.resource_alert_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now(timezone.utc).isoformat()
            severity_str = (
                severity.value.upper()
                if hasattr(severity, "value")
                else str(severity).upper()
            )

            lines = [
                "=" * 80,
                f"TIMESTAMP: {timestamp}",
                f"SEVERITY: {severity_str}",
                "TYPE: RESOURCE_LOW",
                f"AFFECTED_RESOURCES: {len(resources)}",
                "",
            ]

            for resource in resources:
                lines.append(f"  {resource.name}: {resource.count}")

            lines.extend(["", "=" * 80, ""])

            with open(log_path, "a") as f:
                f.write("\n".join(lines))
        except (IOError, OSError) as e:
            logger.error(f"Failed to write resource alert file: {e}")

    def get_cooldown_status(self) -> Dict[str, Any]:
        """Get current cooldown status for debugging/monitoring."""
        now = datetime.now(timezone.utc)
        one_hour_ago = now - timedelta(hours=1)

        active_cooldowns = {}
        for name, last_alert in self._resource_last_alert.items():
            cooldown_delta = timedelta(minutes=self.config.per_resource_cooldown_minutes)
            if now < last_alert + cooldown_delta:
                remaining = (last_alert + cooldown_delta - now).total_seconds() / 60
                active_cooldowns[name] = f"{remaining:.1f} min remaining"

        return {
            "global_cooldown_active": self._is_global_cooldown_active(now),
            "alerts_this_hour": len(
                [ts for ts in self._alerts_this_hour if ts > one_hour_ago]
            ),
            "max_alerts_per_hour": self.config.max_alerts_per_hour,
            "active_resource_cooldowns": active_cooldowns,
        }
