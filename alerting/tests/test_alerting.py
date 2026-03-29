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
    ResourceMonitor,
    ResourceMonitorResult,
    ResourceStatus,
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
        assert mgr.alerts_sent == []


# ---------------------------------------------------------------------------
# AlertManager — send_notification
# ---------------------------------------------------------------------------


class TestSendNotification:
    def test_noop_when_no_discord(self):
        """send_notification is a no-op when no Discord URL is configured."""
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
# _log_resource_check — JSON output
# ---------------------------------------------------------------------------


class TestLogResourceCheck:
    def test_writes_valid_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            alert_file = os.path.join(tmpdir, "alerts.log")
            config = AlertingConfig(
                resource_alert_file=alert_file,
                log_all_checks=True,
            )
            alert_manager = AlertManager()
            resources = [ResourceStatus(name="queue-A", count=100)]
            mgr = ResourceMonitor(
                check_fn=lambda: resources,
                alert_manager=alert_manager,
                config=config,
            )

            result = ResourceMonitorResult()
            result.resources_checked = 10
            result.healthy_resources = 10
            result.warning_resources = []
            result.critical_resources = []

            mgr._log_resource_check(result)

            with open(alert_file) as f:
                line = f.readline().strip()

            parsed = json.loads(line)  # must not raise
            assert parsed["type"] == "resource_check"
            assert parsed["resources_checked"] == 10


# ---------------------------------------------------------------------------
# service_name — interpolation in alert templates
# ---------------------------------------------------------------------------


class TestServiceName:
    def test_alert_manager_default_service_name(self):
        mgr = AlertManager()
        assert mgr.service_name == "Alerting Service"

    def test_alert_manager_custom_service_name(self):
        mgr = AlertManager(service_name="My App")
        assert mgr.service_name == "My App"

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

    def test_resource_monitor_syncs_service_name_to_alert_manager(self):
        am = AlertManager()  # default service_name = "Alerting Service"
        config = AlertingConfig(service_name="Synced Service")
        ResourceMonitor(check_fn=lambda: [], alert_manager=am, config=config)
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
# send_alert — Discord routing by severity
# ---------------------------------------------------------------------------


class TestSendAlertDiscordRouting:
    def test_critical_severity_triggers_discord_alert(self):
        """send_alert calls _send_critical_discord_alert when severity=CRITICAL."""
        mgr = AlertManager(discord_webhook_url="https://discord.example.com/webhook")
        mgr._send_critical_discord_alert = MagicMock(return_value=True)
        err = AlertError(
            category=ErrorCategory.SERVER_ERROR,
            severity=ErrorSeverity.CRITICAL,
            provider="openai",
            original_error="500",
            message="Internal server error",
        )
        mgr.send_alert(err)
        mgr._send_critical_discord_alert.assert_called_once()

    def test_non_critical_severity_does_not_trigger_discord_alert(self):
        """send_alert does NOT call _send_critical_discord_alert for non-CRITICAL severity."""
        mgr = AlertManager(discord_webhook_url="https://discord.example.com/webhook")
        mgr._send_critical_discord_alert = MagicMock(return_value=True)
        err = AlertError(
            category=ErrorCategory.NETWORK_ERROR,
            severity=ErrorSeverity.HIGH,
            provider="openai",
            original_error="timeout",
            message="Connection timed out",
        )
        mgr.send_alert(err)
        mgr._send_critical_discord_alert.assert_not_called()


# ---------------------------------------------------------------------------
# _build_resource_context — no duplicate Recommended Actions section
# ---------------------------------------------------------------------------


class TestResourceContext:
    def test_no_duplicate_recommended_actions_section(self):
        """Recommended Actions must appear exactly once in the full alert message."""
        import tempfile, os
        config = AlertingConfig(
            service_name="TestSvc",
            include_recommendations=True,
            resource_alert_file=os.path.join(tempfile.mkdtemp(), "alerts.log"),
        )
        am = AlertManager()
        monitor = ResourceMonitor(check_fn=lambda: [], alert_manager=am, config=config)

        resources = [ResourceStatus(name="queue-math-easy", count=2)]
        threshold = config.critical_min
        alert_error = monitor._build_resource_error(
            resources, ErrorSeverity.CRITICAL, threshold
        )
        context = monitor._build_resource_context(resources, threshold)
        msg = am._build_alert_message(alert_error, context)
        assert msg.count("Recommended Actions:") == 1

    def test_check_and_alert_calls_check_fn(self):
        """check_and_alert invokes check_fn and classifies resources correctly."""
        config = AlertingConfig(critical_min=5, warning_min=20, healthy_min=50)
        am = AlertManager()

        resources = [
            ResourceStatus(name="queue-A", count=2),   # critical
            ResourceStatus(name="queue-B", count=15),  # warning
            ResourceStatus(name="queue-C", count=60),  # healthy
        ]
        monitor = ResourceMonitor(
            check_fn=lambda: resources, alert_manager=am, config=config
        )
        # Patch send_alert to avoid real IO
        am.send_alert = MagicMock(return_value=True)

        result = monitor.check_and_alert()

        assert result.resources_checked == 3
        assert len(result.critical_resources) == 1
        assert result.critical_resources[0].name == "queue-A"
        assert len(result.warning_resources) == 1
        assert result.warning_resources[0].name == "queue-B"
        assert result.healthy_resources == 1

    def test_cooldown_suppresses_repeat_alerts(self):
        """A resource in cooldown does not trigger a second alert."""
        config = AlertingConfig(
            critical_min=5,
            warning_min=20,
            healthy_min=50,
            per_resource_cooldown_minutes=60,
        )
        am = AlertManager()
        am.send_alert = MagicMock(return_value=True)

        resources = [ResourceStatus(name="queue-A", count=2)]
        monitor = ResourceMonitor(
            check_fn=lambda: resources, alert_manager=am, config=config
        )

        monitor.check_and_alert()  # first check — alert sent
        result = monitor.check_and_alert()  # second check — in cooldown

        assert am.send_alert.call_count == 1
        assert result.alerts_suppressed >= 1

    def test_check_fn_exception_returns_empty_result(self):
        """If check_fn raises, check_and_alert returns an empty result without propagating."""
        am = AlertManager()
        am.send_alert = MagicMock(return_value=True)

        def raising_check_fn():
            raise RuntimeError("db timeout")

        monitor = ResourceMonitor(
            check_fn=raising_check_fn, alert_manager=am, config=AlertingConfig()
        )
        result = monitor.check_and_alert()

        assert result.resources_checked == 0
        assert result.alerts_sent == 0
        am.send_alert.assert_not_called()
