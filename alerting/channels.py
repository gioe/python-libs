"""Alert channel abstractions for routing alerts to external services.

Provides a generic AlertChannel ABC, Alert dataclass, AlertSeverity enum,
and concrete implementations for Discord and generic webhooks. All HTTP
calls use stdlib urllib.request — no external HTTP library required.
"""

import json
import logging
import ssl
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import certifi

logger = logging.getLogger(__name__)


class AlertSeverity(str, Enum):
    """Severity levels for alerts sent via AlertChannel."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class Alert:
    """An alert message to be sent through an AlertChannel.

    Attributes:
        title: Short summary of the alert.
        message: Detailed description.
        severity: How urgent this alert is.
        metadata: Arbitrary key/value pairs for extra context.
    """

    title: str
    message: str
    severity: AlertSeverity = AlertSeverity.MEDIUM
    metadata: dict[str, Any] = field(default_factory=dict)


class AlertChannel(ABC):
    """Abstract base class for alert delivery channels.

    Subclasses implement :meth:`send` to deliver an :class:`Alert` to a
    specific destination (Discord webhook, generic HTTP endpoint, etc.).
    """

    @abstractmethod
    def send(self, alert: Alert) -> bool:
        """Send the alert.

        Returns:
            True if the alert was delivered successfully, False otherwise.
            Implementations should log errors and return False rather than
            raising exceptions so callers can continue processing.
        """


# ---------------------------------------------------------------------------
# Discord embed color map
# ---------------------------------------------------------------------------

_DISCORD_SEVERITY_COLORS: dict[AlertSeverity, int] = {
    AlertSeverity.LOW: 0x00FF00,      # green
    AlertSeverity.MEDIUM: 0xFFFF00,   # yellow
    AlertSeverity.HIGH: 0xFF8800,     # orange
    AlertSeverity.CRITICAL: 0xFF0000, # red
}

_DEFAULT_HTTP_TIMEOUT = 10


class DiscordAlertChannel(AlertChannel):
    """Sends alerts as Discord embeds via a webhook URL.

    Uses stdlib ``urllib.request`` — no external dependencies.

    Args:
        webhook_url: The Discord webhook URL to POST to.
        timeout: HTTP request timeout in seconds (default: 10).
    """

    def __init__(self, webhook_url: str, timeout: int = _DEFAULT_HTTP_TIMEOUT) -> None:
        self.webhook_url = webhook_url
        self.timeout = timeout

    def send(self, alert: Alert) -> bool:
        color = _DISCORD_SEVERITY_COLORS.get(alert.severity, 0x808080)
        embed: dict[str, Any] = {
            "title": alert.title,
            "description": alert.message,
            "color": color,
        }
        if alert.metadata:
            embed["fields"] = [
                {"name": str(k), "value": str(v), "inline": False}
                for k, v in alert.metadata.items()
            ]

        payload = json.dumps({"embeds": [embed]}).encode("utf-8")
        req = urllib.request.Request(
            url=self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ssl.create_default_context(cafile=certifi.where())):
                pass  # Discord returns 204 No Content on success
            return True
        except urllib.error.HTTPError as exc:
            logger.warning(
                "DiscordAlertChannel: HTTP %s %s", exc.code, exc.reason, exc_info=True
            )
            return False
        except Exception:
            logger.warning("DiscordAlertChannel: failed to send alert", exc_info=True)
            return False


class WebhookAlertChannel(AlertChannel):
    """Sends alerts as a generic JSON POST to an arbitrary HTTP endpoint.

    Uses stdlib ``urllib.request`` — no external dependencies.

    The posted payload has the shape::

        {
            "title": "...",
            "message": "...",
            "severity": "low|medium|high|critical",
            "metadata": { ... }
        }

    Args:
        url: The endpoint to POST to.
        headers: Additional HTTP headers to include (default: none).
        timeout: HTTP request timeout in seconds (default: 10).
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: int = _DEFAULT_HTTP_TIMEOUT,
    ) -> None:
        self.url = url
        self.extra_headers = headers or {}
        self.timeout = timeout

    def send(self, alert: Alert) -> bool:
        payload = json.dumps(
            {
                "title": alert.title,
                "message": alert.message,
                "severity": alert.severity.value,
                "metadata": alert.metadata,
            },
            default=str,
        ).encode("utf-8")

        all_headers = {"Content-Type": "application/json", **self.extra_headers}
        req = urllib.request.Request(
            url=self.url,
            data=payload,
            headers=all_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ssl.create_default_context(cafile=certifi.where())):
                pass
            return True
        except urllib.error.HTTPError as exc:
            logger.warning(
                "WebhookAlertChannel: HTTP %s %s", exc.code, exc.reason, exc_info=True
            )
            return False
        except Exception:
            logger.warning("WebhookAlertChannel: failed to send alert", exc_info=True)
            return False
