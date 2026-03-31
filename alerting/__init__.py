# Shared alerting library for AIQ services

from .alerting import (
    ResourceMonitor,
    ResourceStatus,
    ResourceMonitorResult,
    AlertManager,
    AlertingConfig,
    ErrorCategory,
    ErrorSeverity,
    AlertError,
    AlertableError,
)
from .channels import (
    AlertChannel,
    Alert,
    AlertSeverity,
    DiscordAlertChannel,
    WebhookAlertChannel,
)

__all__ = [
    "ResourceMonitor",
    "ResourceStatus",
    "ResourceMonitorResult",
    "AlertManager",
    "AlertingConfig",
    "ErrorCategory",
    "ErrorSeverity",
    "AlertError",
    "AlertableError",
    "AlertChannel",
    "Alert",
    "AlertSeverity",
    "DiscordAlertChannel",
    "WebhookAlertChannel",
]
