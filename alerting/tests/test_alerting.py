"""Standalone unit tests for libs/alerting/alerting.py.

Run from the repo root with:
    PYTHONPATH=. pytest libs/alerting/tests/ -v

These tests have no dependency on question-service packages.
"""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from alerting.alerting import (
    AlertManager,
    AlertingConfig,
    ErrorCategory,
    ErrorSeverity,
    InventoryAlertManager,
    InventoryAlertResult,
    StratumAlert,
)


# ---------------------------------------------------------------------------
# ErrorCategory / ErrorSeverity
# ---------------------------------------------------------------------------


class TestEnums:
    def test_error_category_is_str(self):
        assert isinstance(ErrorCategory.BILLING_QUOTA, str)
        assert ErrorCategory.BILLING_QUOTA == "billing_quota"

    def test_error_severity_is_str(self):
        assert isinstance(ErrorSeverity.CRITICAL, str)
        assert ErrorSeverity.CRITICAL == "critical"

    def test_category_string_comparison(self):
        assert ErrorCategory.RATE_LIMIT == "rate_limit"
        assert "rate_limit" == ErrorCategory.RATE_LIMIT


# ---------------------------------------------------------------------------
# AlertManager — initialisation
# ---------------------------------------------------------------------------


class TestAlertManagerInit:
    def test_defaults(self):
        mgr = AlertManager()
        assert mgr.email_enabled is False
        assert mgr.alerts_sent == []

    def test_email_config(self):
        mgr = AlertManager(
            email_enabled=True,
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="user",
            smtp_password="secret",  # pragma: allowlist secret
            from_email="from@example.com",
            to_emails=["to@example.com"],
        )
        assert mgr.email_enabled is True
        assert mgr.smtp_host == "smtp.example.com"


# ---------------------------------------------------------------------------
# AlertManager — send_notification
# ---------------------------------------------------------------------------


class TestSendNotification:
    def test_noop_when_email_disabled_and_no_discord(self):
        """send_notification is a no-op when email is disabled and no Discord URL."""
        mgr = AlertManager()
        mgr.send_notification(
            title="Test",
            fields=[("Generated", 5), ("Inserted", 5)],
            severity="info",
        )

    def test_discord_noop_when_url_absent(self):
        """No Discord call when discord_webhook_url is not set."""
        mgr = AlertManager()
        mgr._send_discord_alert = MagicMock(side_effect=AssertionError("should not be called"))
        mgr.send_notification(title="Test", fields=[("Key", "Value")], severity="info")

    def test_discord_post_error_does_not_raise(self):
        """A Discord POST failure is swallowed — never propagates to caller."""
        mgr = AlertManager(discord_webhook_url="https://discord.example.com/webhook")
        mgr._send_discord_alert = MagicMock(side_effect=RuntimeError("network error"))
        mgr.send_notification(title="Test", fields=[("Key", "Value")], severity="info")

    def test_discord_called_with_green_for_info(self):
        """severity='info' maps to DISCORD_COLOR_SUCCESS (green)."""
        mgr = AlertManager(discord_webhook_url="https://discord.example.com/webhook")
        mgr._send_discord_alert = MagicMock(return_value=True)
        mgr.send_notification(title="All good", fields=[("Status", "ok")], severity="info")
        _, kwargs = mgr._send_discord_alert.call_args
        assert kwargs["color"] == AlertManager.DISCORD_COLOR_SUCCESS

    def test_discord_called_with_yellow_for_warning(self):
        """severity='warning' maps to DISCORD_COLOR_WARNING (yellow)."""
        mgr = AlertManager(discord_webhook_url="https://discord.example.com/webhook")
        mgr._send_discord_alert = MagicMock(return_value=True)
        mgr.send_notification(title="Heads up", fields=[], severity="warning")
        _, kwargs = mgr._send_discord_alert.call_args
        assert kwargs["color"] == AlertManager.DISCORD_COLOR_WARNING

    def test_discord_called_with_red_for_critical(self):
        """severity='critical' maps to DISCORD_COLOR_CRITICAL (red)."""
        mgr = AlertManager(discord_webhook_url="https://discord.example.com/webhook")
        mgr._send_discord_alert = MagicMock(return_value=True)
        mgr.send_notification(title="FIRE", fields=[], severity="critical")
        _, kwargs = mgr._send_discord_alert.call_args
        assert kwargs["color"] == AlertManager.DISCORD_COLOR_CRITICAL

    def test_discord_embed_fields_contain_labels(self):
        """Each (label, value) tuple becomes a Discord embed field."""
        mgr = AlertManager(discord_webhook_url="https://discord.example.com/webhook")
        mgr._send_discord_alert = MagicMock(return_value=True)
        mgr.send_notification(
            title="Run Complete",
            fields=[("Generated", 50), ("Inserted", 47), ("Duration", "45.2s")],
            severity="info",
        )
        _, kwargs = mgr._send_discord_alert.call_args
        field_names = [f["name"] for f in kwargs["fields"]]
        assert "Generated" in field_names
        assert "Inserted" in field_names
        assert "Duration" in field_names

    def test_metadata_appended_as_discord_field(self):
        """metadata dict is appended as a single 'Details' Discord embed field."""
        mgr = AlertManager(discord_webhook_url="https://discord.example.com/webhook")
        mgr._send_discord_alert = MagicMock(return_value=True)
        mgr.send_notification(
            title="Run Complete",
            fields=[("Generated", 10)],
            severity="info",
            metadata={"env": "production", "version": "1.2"},
        )
        _, kwargs = mgr._send_discord_alert.call_args
        field_names = [f["name"] for f in kwargs["fields"]]
        assert "Details" in field_names

    def test_smtp_error_caught_and_logged(self, caplog):
        """SMTP errors are caught and logged, not raised."""
        import smtplib
        import logging

        mgr = AlertManager(
            email_enabled=True,
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="user@example.com",
            smtp_password="test-password-not-real",  # pragma: allowlist secret
            from_email="alerts@example.com",
            to_emails=["recipient@example.com"],
        )
        with patch("smtplib.SMTP") as mock_smtp_class:
            mock_smtp_class.return_value.__enter__.return_value.send_message.side_effect = (
                smtplib.SMTPException("connection refused")
            )
            with caplog.at_level(logging.ERROR):
                mgr.send_notification(title="Test", fields=[("Key", "val")], severity="info")

        assert any("Failed to send notification email" in r.message for r in caplog.records)

    def test_email_sent_when_enabled(self):
        """Email is sent when email_enabled and SMTP is configured."""
        from unittest.mock import MagicMock

        mgr = AlertManager(
            email_enabled=True,
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="user@example.com",
            smtp_password="test-password-not-real",  # pragma: allowlist secret
            from_email="alerts@example.com",
            to_emails=["recipient@example.com"],
        )
        with patch("smtplib.SMTP") as mock_smtp_class:
            mock_server = MagicMock()
            mock_smtp_class.return_value.__enter__.return_value = mock_server
            mgr.send_notification(
                title="Run Complete",
                fields=[("Generated", 10), ("Inserted", 8)],
                severity="info",
            )
            mock_server.send_message.assert_called_once()

    def test_dict_value_formatted_as_string(self):
        """A dict value in fields is rendered as 'k: v' pairs."""
        mgr = AlertManager(discord_webhook_url="https://discord.example.com/webhook")
        mgr._send_discord_alert = MagicMock(return_value=True)
        mgr.send_notification(
            title="Run",
            fields=[("By Type", {"math": 10, "logic": 8})],
            severity="info",
        )
        _, kwargs = mgr._send_discord_alert.call_args
        by_type_field = next(f for f in kwargs["fields"] if f["name"] == "By Type")
        assert "math: 10" in by_type_field["value"] or "logic: 8" in by_type_field["value"]


# ---------------------------------------------------------------------------
# _log_inventory_check — JSON output
# ---------------------------------------------------------------------------


class TestLogInventoryCheck:
    def test_writes_valid_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            alert_file = os.path.join(tmpdir, "alerts.log")
            config = AlertingConfig(
                inventory_alert_file=alert_file,
                log_all_checks=True,
            )
            alert_manager = AlertManager()
            mgr = InventoryAlertManager(alert_manager=alert_manager, config=config)

            result = InventoryAlertResult()
            result.strata_checked = 10
            result.healthy_strata = 10
            result.warning_strata = []
            result.critical_strata = []

            mgr._log_inventory_check(result)

            with open(alert_file) as f:
                line = f.readline().strip()

            parsed = json.loads(line)  # must not raise
            assert parsed["type"] == "inventory_check"
            assert parsed["strata_checked"] == 10


# ---------------------------------------------------------------------------
# _create_html_alert — html.escape applied
# ---------------------------------------------------------------------------


class TestCreateHtmlAlert:
    def _make_error(self, message="<script>xss</script>", provider="<evil>"):
        err = MagicMock()
        err.message = message
        err.provider = provider
        err.category = ErrorCategory.BILLING_QUOTA
        err.severity = ErrorSeverity.CRITICAL
        err.original_error = "<img src=x onerror=alert(1)>"
        return err

    def test_html_escaped_in_output(self):
        mgr = AlertManager()
        err = self._make_error()
        html_body = mgr._create_html_alert(
            classified_error=err,
            alert_message="Test alert. Recommended Actions: check logs",
        )
        assert "<script>" not in html_body
        assert "&lt;script&gt;" in html_body
        assert "<evil>" not in html_body
        assert "&lt;evil&gt;" in html_body
        assert "&lt;img" in html_body
