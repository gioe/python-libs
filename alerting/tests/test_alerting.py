"""Standalone unit tests for libs/alerting/alerting.py.

Run from the repo root with:
    PYTHONPATH=. pytest libs/alerting/tests/ -v

These tests have no dependency on question-service packages.
"""

import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from libs.alerting.alerting import (
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
# AlertManager — send_run_completion
# ---------------------------------------------------------------------------


class TestSendRunCompletion:
    def test_success_no_email(self):
        """send_run_completion succeeds without SMTP when email is disabled."""
        mgr = AlertManager()
        mgr.send_run_completion(
            exit_code=0,
            run_summary={
                "generated": 5,
                "inserted": 5,
                "errors": 0,
                "duration_seconds": 1.0,
                "details": {},
            },
        )

    def test_failure_no_email(self):
        mgr = AlertManager()
        mgr.send_run_completion(
            exit_code=1,
            run_summary={
                "generated": 0,
                "inserted": 0,
                "errors": 3,
                "duration_seconds": 0.5,
                "details": {},
            },
        )

    def test_none_run_summary(self):
        mgr = AlertManager()
        mgr.send_run_completion(exit_code=0, run_summary=None)


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
