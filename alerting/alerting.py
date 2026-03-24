"""Alert notification system for critical errors and inventory monitoring.

This module provides functionality to send alerts via email and other channels
when critical errors occur in a pipeline, including low inventory alerts.

This module has no service-specific dependencies — it can be
imported by any service that sets PYTHONPATH to include the repo root.
"""

import html as html_module
import json
import logging
import re
import smtplib
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

import yaml

# Basic email format validation pattern (RFC 5322 simplified)
_EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


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
    """Categories of API errors."""

    BILLING_QUOTA = "billing_quota"  # Insufficient funds, quota exceeded
    RATE_LIMIT = "rate_limit"  # Rate limit/throttling errors
    AUTHENTICATION = "authentication"  # API key invalid or expired
    INVALID_REQUEST = "invalid_request"  # Malformed request or invalid parameters
    SERVER_ERROR = "server_error"  # Provider server errors (5xx)
    NETWORK_ERROR = "network_error"  # Connection/timeout errors
    MODEL_ERROR = "model_error"  # Model not found or unavailable
    INVENTORY_LOW = "inventory_low"  # Question inventory below threshold
    SCRIPT_FAILURE = "script_failure"  # Multiple question types failed in bootstrap
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


class AlertManager:
    """Manages alert notifications for critical errors."""

    # Maximum number of alerts to retain in memory
    MAX_ALERTS_RETENTION = 1000

    # Default SMTP timeout in seconds
    SMTP_TIMEOUT_SECONDS = 30

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
        email_enabled: bool = False,
        smtp_host: Optional[str] = None,
        smtp_port: int = 587,
        smtp_username: Optional[str] = None,
        smtp_password: Optional[str] = None,
        from_email: Optional[str] = None,
        to_emails: Optional[List[str]] = None,
        alert_file_path: Optional[str] = None,
        discord_webhook_url: Optional[str] = None,
        service_name: str = "Alerting Service",
    ):
        """Initialize alert manager.

        Args:
            email_enabled: Enable email alerts
            smtp_host: SMTP server host
            smtp_port: SMTP server port (default: 587 for TLS)
            smtp_username: SMTP username
            smtp_password: SMTP password
            from_email: Sender email address
            to_emails: List of recipient email addresses
            alert_file_path: Path to file for logging critical alerts
            discord_webhook_url: Discord webhook URL for circuit breaker / quota alerts
            service_name: Name of the service shown in alert templates
        """
        self.email_enabled = email_enabled
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_username = smtp_username
        self.smtp_password = smtp_password
        self.from_email = from_email
        self.to_emails = to_emails or []
        self.alert_file_path = alert_file_path
        self.discord_webhook_url = discord_webhook_url
        self.service_name = service_name

        # Track last Discord alert time per provider for cooldown enforcement
        self._discord_cooldowns: Dict[str, float] = {}

        # Track alerts sent (capped to prevent unbounded memory growth)
        self.alerts_sent: List[dict] = []

        if self.email_enabled:
            _missing = []
            if not smtp_host:
                _missing.append("SMTP_HOST")
            if not smtp_username:
                _missing.append("SMTP_USERNAME")
            if not smtp_password:
                _missing.append("SMTP_PASSWORD")
            if not from_email:
                _missing.append("ALERT_FROM_EMAIL")
            if _missing:
                logger.warning(
                    "Email alerts enabled but required variable(s) not set: %s. "
                    "Email alerts will not be sent.",
                    ", ".join(_missing),
                )
                self.email_enabled = False
            elif not self.to_emails:
                logger.warning(
                    "Email alerts enabled but no recipients configured. "
                    "Email alerts will not be sent."
                )
                self.email_enabled = False
            else:
                # Validate email formats
                if from_email and not _EMAIL_PATTERN.match(from_email):
                    logger.warning(
                        f"Invalid from_email format: {from_email}. "
                        "Email alerts will not be sent."
                    )
                    self.email_enabled = False
                invalid_recipients = [
                    e for e in self.to_emails if not _EMAIL_PATTERN.match(e)
                ]
                if invalid_recipients:
                    logger.warning(
                        f"Invalid recipient email format(s): {invalid_recipients}. "
                        "Email alerts will not be sent."
                    )
                    self.email_enabled = False

        logger.info(
            f"AlertManager initialized: email_enabled={self.email_enabled}, "
            f"alert_file={bool(self.alert_file_path)}, "
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

    def _send_billing_quota_discord_alert(
        self,
        classified_error: "AlertableError",
        context: Optional[str] = None,
    ) -> bool:
        """Send a Discord alert for a BILLING_QUOTA classified error.

        Uses the same 10-minute per-provider cooldown as circuit breaker alerts.

        Args:
            classified_error: The classified BILLING_QUOTA error
            context: Additional context string

        Returns:
            True if sent, False if suppressed or Discord not configured
        """
        if not self.discord_webhook_url:
            return False

        cooldown_key = f"billing:{classified_error.provider}"
        if self._is_discord_cooldown_active(cooldown_key):
            logger.debug(
                f"Discord billing quota alert suppressed for {classified_error.provider} (cooldown active)"
            )
            return False

        title = f"\U0001f6a8 Billing Quota Exhausted: {classified_error.provider}"
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
                f"Discord billing quota alert sent for {classified_error.provider}"
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

        # Send email alert if enabled
        if self.email_enabled:
            try:
                self._send_email_alert(classified_error, alert_message)
                category_str = (
                    classified_error.category.value
                    if hasattr(classified_error.category, "value")
                    else str(classified_error.category)
                )
                logger.info(f"Email alert sent for {category_str}")
            except Exception as e:
                logger.error(f"Failed to send email alert: {e}")
                success = False

        # Write to alert file if configured
        if self.alert_file_path:
            try:
                self._write_alert_file(classified_error, alert_message)
                logger.info(f"Alert written to file: {self.alert_file_path}")
            except Exception as e:
                logger.error(f"Failed to write alert file: {e}")
                success = False

        # Send Discord alert for BILLING_QUOTA errors
        category_val = (
            classified_error.category.value
            if hasattr(classified_error.category, "value")
            else str(classified_error.category)
        )
        # Safe to compare raw string to ErrorCategory member: ErrorCategory inherits from
        # str, so its members equal their .value strings (e.g. "billing_quota").
        if category_val == ErrorCategory.BILLING_QUOTA:
            self._send_billing_quota_discord_alert(classified_error, context)

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
        """Send a generic notification via email and Discord.

        SMTP and Discord errors are caught and logged; they never raise to the
        caller.

        Args:
            title: Notification title (used as email subject and Discord embed title).
            fields: List of (label, value) tuples rendered as the main content table.
                Values may be strings, numbers, dicts, or lists — all are formatted
                as human-readable strings automatically.
            severity: Controls color styling. One of ``"info"`` (green),
                ``"warning"`` (yellow), or ``"critical"`` (red).
            metadata: Optional dict rendered as a secondary section at the end of
                both the email body and the Discord embed.
        """
        _VALID_SEVERITIES = {"info", "warning", "critical"}
        if severity.lower() not in _VALID_SEVERITIES:
            logger.warning(
                "send_notification() received unknown severity %r; defaulting to 'info'. "
                "Valid values: %s",
                severity,
                ", ".join(sorted(_VALID_SEVERITIES)),
            )

        if self.email_enabled:
            try:
                text_body = self._build_notification_text(title, fields, severity, metadata)
                html_body = self._build_notification_html(title, fields, severity, metadata)

                msg = MIMEMultipart("alternative")
                msg["Subject"] = title
                msg["From"] = self.from_email or ""
                msg["To"] = ", ".join(self.to_emails)
                msg.attach(MIMEText(text_body, "plain"))
                msg.attach(MIMEText(html_body, "html"))

                if self.smtp_host is None:
                    raise ValueError("SMTP host must be configured for email alerts")
                if self.smtp_username is None:
                    raise ValueError("SMTP username must be configured for email alerts")
                if self.smtp_password is None:
                    raise ValueError("SMTP password must be configured for email alerts")

                with smtplib.SMTP(
                    self.smtp_host, self.smtp_port, timeout=self.SMTP_TIMEOUT_SECONDS
                ) as server:
                    server.starttls()
                    server.login(self.smtp_username, self.smtp_password)
                    server.send_message(msg)

                logger.info(f"Notification email sent: {title!r}")
            except Exception as e:
                logger.error(f"Failed to send notification email: {e}")

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

    def _build_notification_text(
        self,
        title: str,
        fields: List[Tuple[str, Any]],
        severity: str,
        metadata: Optional[Dict[str, Any]],
    ) -> str:
        """Build plain-text notification body."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = [
            title,
            f"Time: {timestamp}",
            f"Severity: {severity.upper()}",
            "",
        ]
        for label, value in fields:
            lines.append(f"  {label}: {_format_value(value)}")
        if metadata:
            lines += ["", "Details:"]
            for k, v in metadata.items():
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    def _build_notification_html(
        self,
        title: str,
        fields: List[Tuple[str, Any]],
        severity: str,
        metadata: Optional[Dict[str, Any]],
    ) -> str:
        """Build HTML notification email body."""
        color = {
            "info": "#28a745",
            "warning": "#ffc107",
            "critical": "#dc3545",
        }.get(severity.lower(), "#28a745")

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        rows = "".join(
            f"<tr><td>{html_module.escape(str(label))}</td>"
            f"<td>{html_module.escape(_format_value(value))}</td></tr>"
            for label, value in fields
        )

        metadata_section = ""
        if metadata:
            meta_rows = "".join(
                f"<tr><td>{html_module.escape(str(k))}</td>"
                f"<td>{html_module.escape(str(v))}</td></tr>"
                for k, v in metadata.items()
            )
            metadata_section = f"""
            <h3>Details</h3>
            <table>
                <tr><th>Key</th><th>Value</th></tr>
                {meta_rows}
            </table>"""

        return f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .status-box {{
                    border-left: 4px solid {color};
                    padding: 15px;
                    background-color: #f8f9fa;
                    margin: 20px 0;
                }}
                .title {{ color: {color}; font-weight: bold; font-size: 18px; }}
                table {{
                    border-collapse: collapse;
                    width: 100%;
                    max-width: 400px;
                    margin-top: 15px;
                }}
                th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #dee2e6; }}
                th {{ background-color: #e9ecef; font-weight: bold; }}
                h3 {{
                    margin-top: 25px;
                    margin-bottom: 5px;
                    font-size: 14px;
                    color: #495057;
                    text-transform: uppercase;
                    letter-spacing: 0.05em;
                }}
                .footer {{ margin-top: 30px; font-size: 12px; color: #6c757d; }}
            </style>
        </head>
        <body>
            <div class="status-box">
                <div class="title">{html_module.escape(title)}</div>
                <div>Time: {timestamp}</div>
            </div>
            <table>
                <tr><th>Field</th><th>Value</th></tr>
                {rows}
            </table>
            {metadata_section}
            <div class="footer"><p>This is an automated notification.</p></div>
        </body>
        </html>
        """

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
        elif category_val == ErrorCategory.BILLING_QUOTA:
            lines.extend(
                [
                    f"1. Check your {classified_error.provider} account balance",
                    "2. Review usage quotas and limits",
                    "3. Add funds or upgrade plan if needed",
                    "4. Verify billing information is up to date",
                ]
            )
        elif category_val == ErrorCategory.AUTHENTICATION:
            lines.extend(
                [
                    f"1. Verify {classified_error.provider} API key is correct",
                    "2. Check if API key has expired",
                    "3. Regenerate API key if necessary",
                    "4. Update environment variables with new key",
                ]
            )
        elif category_val == ErrorCategory.RATE_LIMIT:
            lines.extend(
                [
                    "1. Reduce request frequency",
                    "2. Implement exponential backoff",
                    "3. Consider upgrading API tier for higher limits",
                ]
            )
        elif category_val == ErrorCategory.INVENTORY_LOW:
            lines.extend(
                [
                    "1. Review generation logs for recent failures",
                    "2. Check LLM provider API quotas and billing",
                    "3. Review application logs for more context",
                ]
            )
        elif category_val == ErrorCategory.SCRIPT_FAILURE:
            lines.extend(
                [
                    "1. Check script logs for detailed error messages",
                    "2. Review LLM provider status pages for outages",
                    "3. Verify API keys are valid and have sufficient quota",
                    "4. Check network connectivity to LLM providers",
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

    def _send_email_alert(
        self,
        classified_error: "AlertableError",
        alert_message: str,
    ) -> None:
        """Send email alert."""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = self._get_email_subject(classified_error)
        msg["From"] = self.from_email or ""
        msg["To"] = ", ".join(self.to_emails)

        text_body = alert_message
        html_body = self._create_html_alert(classified_error, alert_message)

        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        if self.smtp_host is None:
            raise ValueError("SMTP host must be configured for email alerts")
        if self.smtp_username is None:
            raise ValueError("SMTP username must be configured for email alerts")
        if self.smtp_password is None:
            raise ValueError("SMTP password must be configured for email alerts")

        with smtplib.SMTP(
            self.smtp_host, self.smtp_port, timeout=self.SMTP_TIMEOUT_SECONDS
        ) as server:
            server.starttls()
            server.login(self.smtp_username, self.smtp_password)
            server.send_message(msg)

        logger.debug(f"Email sent to {len(self.to_emails)} recipients")

    def _get_email_subject(self, classified_error: "AlertableError") -> str:
        """Generate email subject line."""
        severity_val = (
            classified_error.severity.value
            if hasattr(classified_error.severity, "value")
            else str(classified_error.severity)
        )
        category_val = (
            classified_error.category.value
            if hasattr(classified_error.category, "value")
            else str(classified_error.category)
        )
        emoji = "🚨" if severity_val == ErrorSeverity.CRITICAL else "⚠️"

        return (
            f"{emoji} {self.service_name} Alert: {category_val.title()} "
            f"({classified_error.provider})"
        )

    def _create_html_alert(
        self,
        classified_error: "AlertableError",
        alert_message: str,
    ) -> str:
        """Create HTML version of alert email."""
        severity_val = (
            classified_error.severity.value
            if hasattr(classified_error.severity, "value")
            else str(classified_error.severity)
        )
        category_val = (
            classified_error.category.value
            if hasattr(classified_error.category, "value")
            else str(classified_error.category)
        )
        original_error = getattr(classified_error, "original_error", "")

        color_map = {
            ErrorSeverity.CRITICAL: "#dc3545",  # Red
            ErrorSeverity.HIGH: "#fd7e14",  # Orange
            ErrorSeverity.MEDIUM: "#ffc107",  # Yellow
            ErrorSeverity.LOW: "#17a2b8",  # Cyan
        }
        color = color_map.get(severity_val, "#6c757d")  # type: ignore[call-overload]

        html = f"""
        <html>
        <head>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    line-height: 1.6;
                    color: #333;
                }}
                .alert-box {{
                    border-left: 4px solid {color};
                    padding: 15px;
                    background-color: #f8f9fa;
                    margin: 20px 0;
                }}
                .severity {{
                    color: {color};
                    font-weight: bold;
                    font-size: 18px;
                }}
                .detail {{
                    margin: 10px 0;
                }}
                .label {{
                    font-weight: bold;
                }}
                .actions {{
                    background-color: #e9ecef;
                    padding: 15px;
                    margin-top: 20px;
                    border-radius: 4px;
                }}
                .footer {{
                    margin-top: 30px;
                    font-size: 12px;
                    color: #6c757d;
                }}
            </style>
        </head>
        <body>
            <div class="alert-box">
                <div class="severity">{severity_val.upper()} Alert</div>
                <div class="detail">
                    <span class="label">Category:</span> {category_val.title()}
                </div>
                <div class="detail">
                    <span class="label">Provider:</span> {html_module.escape(str(classified_error.provider))}
                </div>
                <div class="detail">
                    <span class="label">Time:</span> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}
                </div>
            </div>

            <div class="detail">
                <span class="label">Message:</span><br>
                {html_module.escape(str(classified_error.message))}
            </div>

            <div class="actions">
                <div class="label">Recommended Actions:</div>
                <pre style="white-space: pre-wrap; font-family: Arial, sans-serif;">{alert_message.split('Recommended Actions:')[1] if 'Recommended Actions:' in alert_message else ''}</pre>
            </div>

            <div class="footer">
                <p>This is an automated alert from {html_module.escape(self.service_name)}.</p>
                <p>Original error: {html_module.escape(str(original_error))}</p>
            </div>
        </body>
        </html>
        """
        return html

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
    """Configuration for inventory alerting loaded from YAML."""

    # Inventory thresholds
    healthy_min: int = 50
    warning_min: int = 20
    critical_min: int = 5

    # Cooldown settings (in minutes)
    per_stratum_cooldown_minutes: int = 60
    global_cooldown_minutes: int = 15
    max_alerts_per_hour: int = 10

    # Content settings
    include_affected_strata: bool = True
    max_strata_detail: int = 5
    include_recommendations: bool = True

    # Service identification
    service_name: str = "Alerting Service"

    # Email settings — None means "derive from service_name" (resolved in __post_init__)
    subject_prefix_warning: Optional[str] = None
    subject_prefix_critical: Optional[str] = None

    def __post_init__(self) -> None:
        if self.subject_prefix_warning is None:
            self.subject_prefix_warning = f"[{self.service_name}] Inventory Warning"
        if self.subject_prefix_critical is None:
            self.subject_prefix_critical = f"[{self.service_name}] CRITICAL: Inventory Alert"

    # File logging
    inventory_alert_file: str = "./logs/inventory_alerts.log"
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
        inventory = data.get("inventory", {})
        thresholds = inventory.get("thresholds", {})
        cooldown = inventory.get("cooldown", {})
        content = inventory.get("content", {})
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
            per_stratum_cooldown_minutes=cooldown.get("per_stratum_minutes", 60),
            global_cooldown_minutes=cooldown.get("global_minutes", 15),
            max_alerts_per_hour=cooldown.get("max_alerts_per_hour", 10),
            include_affected_strata=content.get("include_affected_strata", True),
            max_strata_detail=content.get("max_strata_detail", 5),
            include_recommendations=content.get("include_recommendations", True),
            subject_prefix_warning=email.get("subject_prefix_warning") or None,
            subject_prefix_critical=email.get("subject_prefix_critical") or None,
            inventory_alert_file=file_logging.get(
                "inventory_alert_file", "./logs/inventory_alerts.log"
            ),
            log_all_checks=file_logging.get("log_all_checks", False),
        )


@dataclass
class StratumAlert:
    """Alert information for a single stratum."""

    question_type: str
    difficulty: str
    current_count: int
    threshold: int
    severity: ErrorSeverity


@dataclass
class InventoryAlertResult:
    """Result of an inventory alert check."""

    # Number of strata that triggered alerts (not number of emails sent —
    # all alertable strata are consolidated into one email per check).
    alerts_sent: int = 0
    alerts_suppressed: int = 0
    strata_checked: int = 0
    critical_strata: List[StratumAlert] = field(default_factory=list)
    warning_strata: List[StratumAlert] = field(default_factory=list)
    healthy_strata: int = 0


class InventoryAlertManager:
    """Manages inventory-specific alerting with cooldown tracking.

    This class extends the base AlertManager with inventory-specific functionality:
    - Threshold-based alerts (critical, warning, healthy)
    - Per-stratum cooldowns to prevent alert spam
    - Global cooldowns for overall alert rate limiting
    - Actionable alert content with affected strata details

    Note: This class is NOT thread-safe. It must only be called from a single
    thread. For concurrent access, use external synchronization.
    """

    # Buffer time (in minutes) after cooldown to keep entries before cleanup
    COOLDOWN_CLEANUP_BUFFER_MINUTES = 60

    def __init__(
        self,
        alert_manager: AlertManager,
        config: Optional[AlertingConfig] = None,
    ):
        """Initialize inventory alert manager."""
        self.alert_manager = alert_manager
        self.config = config or AlertingConfig()
        # Propagate service_name from config so all alert templates are consistent
        self.alert_manager.service_name = self.config.service_name

        self._stratum_last_alert: Dict[Tuple[str, str], datetime] = {}
        self._global_last_alert: Optional[datetime] = None
        self._alerts_this_hour: List[datetime] = []

        logger.info(
            f"InventoryAlertManager initialized: "
            f"critical_min={self.config.critical_min}, "
            f"warning_min={self.config.warning_min}, "
            f"cooldown={self.config.per_stratum_cooldown_minutes}min"
        )

    def _cleanup_old_cooldowns(self, now: datetime) -> None:
        """Remove cooldown entries older than cooldown period plus buffer."""
        cutoff = now - timedelta(
            minutes=self.config.per_stratum_cooldown_minutes
            + self.COOLDOWN_CLEANUP_BUFFER_MINUTES
        )

        keys_to_remove = [
            key
            for key, last_alert in self._stratum_last_alert.items()
            if last_alert < cutoff
        ]

        for key in keys_to_remove:
            del self._stratum_last_alert[key]

        if keys_to_remove:
            logger.debug(f"Cleaned up {len(keys_to_remove)} old cooldown entries")

    def check_and_alert(
        self,
        strata_inventory: List[Any],
    ) -> InventoryAlertResult:
        """Check inventory levels and send alerts for strata below thresholds."""
        result = InventoryAlertResult(strata_checked=len(strata_inventory))
        now = datetime.now(timezone.utc)

        self._cleanup_old_cooldowns(now)

        critical_strata: List[StratumAlert] = []
        warning_strata: List[StratumAlert] = []

        for stratum in strata_inventory:
            q_type = stratum.question_type.value
            difficulty = stratum.difficulty.value
            count = stratum.current_count

            if count < self.config.critical_min:
                critical_strata.append(
                    StratumAlert(
                        question_type=q_type,
                        difficulty=difficulty,
                        current_count=count,
                        threshold=self.config.critical_min,
                        severity=ErrorSeverity.CRITICAL,
                    )
                )
            elif count < self.config.warning_min:
                warning_strata.append(
                    StratumAlert(
                        question_type=q_type,
                        difficulty=difficulty,
                        current_count=count,
                        threshold=self.config.warning_min,
                        severity=ErrorSeverity.HIGH,
                    )
                )
            else:
                result.healthy_strata += 1

        result.critical_strata = critical_strata
        result.warning_strata = warning_strata

        if self.config.log_all_checks:
            self._log_inventory_check(result)

        if critical_strata:
            alerts_sent, alerts_suppressed = self._send_inventory_alerts(
                strata=critical_strata,
                severity=ErrorSeverity.CRITICAL,
                now=now,
            )
            result.alerts_sent += alerts_sent
            result.alerts_suppressed += alerts_suppressed

        if warning_strata:
            alerts_sent, alerts_suppressed = self._send_inventory_alerts(
                strata=warning_strata,
                severity=ErrorSeverity.HIGH,
                now=now,
            )
            result.alerts_sent += alerts_sent
            result.alerts_suppressed += alerts_suppressed

        logger.info(
            f"Inventory alert check complete: "
            f"{result.alerts_sent} alerts sent, "
            f"{result.alerts_suppressed} suppressed by cooldown"
        )

        return result

    def _send_inventory_alerts(
        self,
        strata: List[StratumAlert],
        severity: ErrorSeverity,
        now: datetime,
    ) -> Tuple[int, int]:
        """Send alerts for a list of strata with the given severity."""
        alerts_sent = 0
        alerts_suppressed = 0

        alertable_strata: List[StratumAlert] = []
        for stratum in strata:
            key = (stratum.question_type, stratum.difficulty)
            if self._is_in_cooldown(key, now):
                alerts_suppressed += 1
                logger.debug(f"Alert suppressed for {key[0]}/{key[1]} (in cooldown)")
            else:
                alertable_strata.append(stratum)

        if not alertable_strata:
            return alerts_sent, alerts_suppressed

        if self._is_global_cooldown_active(now):
            logger.info(
                f"Global cooldown active, suppressing {len(alertable_strata)} alerts"
            )
            return alerts_sent, alerts_suppressed + len(alertable_strata)

        if not self._check_hourly_rate_limit(now):
            logger.warning(
                f"Hourly alert limit reached ({self.config.max_alerts_per_hour}), "
                f"suppressing {len(alertable_strata)} alerts"
            )
            return alerts_sent, alerts_suppressed + len(alertable_strata)

        alert_error = self._build_inventory_error(
            strata=alertable_strata,
            severity=severity,
        )
        context = self._build_inventory_context(alertable_strata)

        success = self.alert_manager.send_alert(alert_error, context)

        if success:
            alerts_sent = len(alertable_strata)
            for stratum in alertable_strata:
                key = (stratum.question_type, stratum.difficulty)
                self._stratum_last_alert[key] = now
            self._global_last_alert = now
            self._alerts_this_hour.append(now)

            self._write_inventory_alert_file(alertable_strata, severity)
        else:
            alerts_suppressed += len(alertable_strata)

        return alerts_sent, alerts_suppressed

    def _is_in_cooldown(self, key: Tuple[str, str], now: datetime) -> bool:
        """Check if a stratum is in cooldown period."""
        last_alert = self._stratum_last_alert.get(key)
        if last_alert is None:
            return False

        cooldown_delta = timedelta(minutes=self.config.per_stratum_cooldown_minutes)
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

    def _build_inventory_error(
        self,
        strata: List[StratumAlert],
        severity: ErrorSeverity,
    ) -> AlertError:
        """Build an AlertError for inventory alerts."""
        severity_word = "critical" if severity == ErrorSeverity.CRITICAL else "low"
        threshold = strata[0].threshold if strata else 0

        message = (
            f"{len(strata)} question strata have {severity_word} inventory levels "
            f"(below {threshold} questions). "
            f"Question generation may be needed to replenish inventory."
        )

        recommended_actions: List[str] = []
        if self.config.include_recommendations:
            recommended_actions = [
                "Review generation logs for any failures",
                "Check LLM provider API quotas and billing",
                "Review application logs for more context",
            ]

        return AlertError(
            category=ErrorCategory.INVENTORY_LOW,
            severity=severity,
            provider="inventory",
            original_error="LowInventory",
            message=message,
            is_retryable=True,
            recommended_actions=recommended_actions,
        )

    def _build_inventory_context(self, strata: List[StratumAlert]) -> str:
        """Build context string with affected strata details."""
        lines = ["Affected strata:"]

        sorted_strata = sorted(strata, key=lambda s: s.current_count)

        for stratum in sorted_strata[: self.config.max_strata_detail]:
            lines.append(
                f"  - {stratum.question_type}/{stratum.difficulty}: "
                f"{stratum.current_count} questions (threshold: {stratum.threshold})"
            )

        remaining = len(strata) - self.config.max_strata_detail
        if remaining > 0:
            lines.append(f"  ... and {remaining} more strata")

        return "\n".join(lines)

    def _log_inventory_check(self, result: InventoryAlertResult) -> None:
        """Log inventory check results to file."""
        try:
            log_path = Path(self.config.inventory_alert_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now(timezone.utc).isoformat()
            entry = {
                "timestamp": timestamp,
                "type": "inventory_check",
                "strata_checked": result.strata_checked,
                "healthy_strata": result.healthy_strata,
                "warning_strata": len(result.warning_strata),
                "critical_strata": len(result.critical_strata),
            }

            with open(log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except (IOError, OSError) as e:
            logger.error(f"Failed to log inventory check: {e}")

    def _write_inventory_alert_file(
        self,
        strata: List[StratumAlert],
        severity: ErrorSeverity,
    ) -> None:
        """Write inventory alert to dedicated alert file."""
        try:
            log_path = Path(self.config.inventory_alert_file)
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
                "TYPE: INVENTORY_LOW",
                f"AFFECTED_STRATA: {len(strata)}",
                "",
            ]

            for stratum in strata:
                lines.append(
                    f"  {stratum.question_type}/{stratum.difficulty}: "
                    f"{stratum.current_count} (threshold: {stratum.threshold})"
                )

            lines.extend(["", "=" * 80, ""])

            with open(log_path, "a") as f:
                f.write("\n".join(lines))
        except (IOError, OSError) as e:
            logger.error(f"Failed to write inventory alert file: {e}")

    def get_cooldown_status(self) -> Dict[str, Any]:
        """Get current cooldown status for debugging/monitoring."""
        now = datetime.now(timezone.utc)
        one_hour_ago = now - timedelta(hours=1)

        active_cooldowns = {}
        for key, last_alert in self._stratum_last_alert.items():
            cooldown_delta = timedelta(minutes=self.config.per_stratum_cooldown_minutes)
            if now < last_alert + cooldown_delta:
                remaining = (last_alert + cooldown_delta - now).total_seconds() / 60
                active_cooldowns[f"{key[0]}/{key[1]}"] = (
                    f"{remaining:.1f} min remaining"
                )

        return {
            "global_cooldown_active": self._is_global_cooldown_active(now),
            "alerts_this_hour": len(
                [ts for ts in self._alerts_this_hour if ts > one_hour_ago]
            ),
            "max_alerts_per_hour": self.config.max_alerts_per_hour,
            "active_stratum_cooldowns": active_cooldowns,
        }
