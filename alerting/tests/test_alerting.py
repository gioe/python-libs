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
    AlertError,
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
        assert isinstance(ErrorCategory.AUTHENTICATION, str)
        assert ErrorCategory.AUTHENTICATION == "authentication"

    def test_error_severity_is_str(self):
        assert isinstance(ErrorSeverity.CRITICAL, str)
        assert ErrorSeverity.CRITICAL == "critical"

    def test_category_string_comparison(self):
        assert ErrorCategory.RESOURCE_LOW == "resource_low"
        assert "resource_low" == ErrorCategory.RESOURCE_LOW


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
        err.category = ErrorCategory.AUTHENTICATION
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


# ---------------------------------------------------------------------------
# service_name — interpolation in email templates
# ---------------------------------------------------------------------------


class TestServiceName:
    def test_alert_manager_default_service_name(self):
        mgr = AlertManager()
        assert mgr.service_name == "Alerting Service"

    def test_alert_manager_custom_service_name(self):
        mgr = AlertManager(service_name="My App")
        assert mgr.service_name == "My App"

    def test_email_subject_uses_service_name(self):
        mgr = AlertManager(service_name="My App")
        err = MagicMock()
        err.severity = ErrorSeverity.CRITICAL
        err.category = ErrorCategory.SERVER_ERROR
        err.provider = "openai"
        subject = mgr._get_email_subject(err)
        assert "My App" in subject
        assert "IQ Tracker" not in subject
        assert "AIQ" not in subject

    def test_html_footer_uses_service_name(self):
        mgr = AlertManager(service_name="Acme Alerts")
        err = MagicMock()
        err.message = "test"
        err.provider = "openai"
        err.category = ErrorCategory.SERVER_ERROR
        err.severity = ErrorSeverity.HIGH
        err.original_error = "err"
        html_body = mgr._create_html_alert(err, "Test. Recommended Actions: check logs")
        assert "Acme Alerts" in html_body
        assert "IQ Tracker" not in html_body

    def test_alerting_config_default_service_name(self):
        config = AlertingConfig()
        assert config.service_name == "Alerting Service"
        assert "Alerting Service" in config.subject_prefix_warning
        assert "Alerting Service" in config.subject_prefix_critical

    def test_alerting_config_custom_service_name_sets_subject_prefixes(self):
        config = AlertingConfig(service_name="MyService")
        assert "MyService" in config.subject_prefix_warning
        assert "MyService" in config.subject_prefix_critical

    def test_alerting_config_explicit_prefix_not_overridden(self):
        config = AlertingConfig(
            service_name="MyService",
            subject_prefix_warning="[CUSTOM] Warning",
        )
        assert config.subject_prefix_warning == "[CUSTOM] Warning"

    def test_alerting_config_from_yaml_loads_service_name(self):
        import tempfile, yaml, os
        data = {"service_name": "YAML Service"}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump(data, f)
            tmp_path = f.name
        try:
            config = AlertingConfig.from_yaml(tmp_path)
            assert config.service_name == "YAML Service"
            assert "YAML Service" in config.subject_prefix_warning
        finally:
            os.unlink(tmp_path)

    def test_inventory_alert_manager_syncs_service_name_to_alert_manager(self):
        am = AlertManager()  # default service_name = "Alerting Service"
        config = AlertingConfig(service_name="Synced Service")
        InventoryAlertManager(alert_manager=am, config=config)
        assert am.service_name == "Synced Service"


# ---------------------------------------------------------------------------
# recommended_actions — rendering and serialization
# ---------------------------------------------------------------------------


class TestRecommendedActions:
    def _make_alert_error(self, recommended_actions=None):
        kwargs = dict(
            category=ErrorCategory.AUTHENTICATION,
            severity=ErrorSeverity.HIGH,
            provider="openai",
            original_error="Unauthorized",
            message="API key invalid",
        )
        if recommended_actions is not None:
            kwargs["recommended_actions"] = recommended_actions
        return AlertError(**kwargs)

    def test_recommended_actions_default_is_empty_list(self):
        err = self._make_alert_error()
        assert err.recommended_actions == []

    def test_recommended_actions_rendered_in_alert_message(self):
        err = self._make_alert_error(recommended_actions=["Do thing A", "Do thing B"])
        mgr = AlertManager()
        msg = mgr._build_alert_message(err)
        assert "Do thing A" in msg
        assert "Do thing B" in msg

    def test_empty_recommended_actions_falls_back_to_category_defaults(self):
        err = self._make_alert_error(recommended_actions=[])
        mgr = AlertManager()
        msg = mgr._build_alert_message(err)
        # Category default for AUTHENTICATION references "API key"
        assert "API key" in msg

    def test_recommended_actions_included_in_to_dict_when_present(self):
        err = self._make_alert_error(recommended_actions=["Step 1", "Step 2"])
        d = err.to_dict()
        assert "recommended_actions" in d
        assert d["recommended_actions"] == ["Step 1", "Step 2"]

    def test_recommended_actions_omitted_from_to_dict_when_empty(self):
        err = self._make_alert_error(recommended_actions=[])
        d = err.to_dict()
        assert "recommended_actions" not in d


# ---------------------------------------------------------------------------
# _build_inventory_context — no duplicate Recommended Actions section
# ---------------------------------------------------------------------------


class TestInventoryContext:
    def test_no_duplicate_recommended_actions_section(self):
        """Recommended Actions must appear exactly once in the full alert message."""
        import tempfile, os
        config = AlertingConfig(
            service_name="TestSvc",
            include_recommendations=True,
            inventory_alert_file=os.path.join(tempfile.mkdtemp(), "alerts.log"),
        )
        am = AlertManager()
        inv_mgr = InventoryAlertManager(alert_manager=am, config=config)

        strata = [
            StratumAlert(
                question_type="math",
                difficulty="easy",
                current_count=2,
                threshold=5,
                severity=ErrorSeverity.CRITICAL,
            )
        ]
        alert_error = inv_mgr._build_inventory_error(strata, ErrorSeverity.CRITICAL)
        context = inv_mgr._build_inventory_context(strata)
        msg = am._build_alert_message(alert_error, context)
        assert msg.count("Recommended Actions:") == 1
