"""Tests for alerting/channels.py — AlertChannel, Alert, AlertSeverity,
DiscordAlertChannel, WebhookAlertChannel."""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from alerting.channels import (
    Alert,
    AlertChannel,
    AlertSeverity,
    DiscordAlertChannel,
    WebhookAlertChannel,
    _DISCORD_SEVERITY_COLORS,
)


# ---------------------------------------------------------------------------
# AlertSeverity
# ---------------------------------------------------------------------------


class TestAlertSeverity:
    def test_values_are_strings(self):
        for sev in AlertSeverity:
            assert isinstance(sev, str)

    def test_all_severities_present(self):
        assert {s.value for s in AlertSeverity} == {"low", "medium", "high", "critical"}


# ---------------------------------------------------------------------------
# Alert dataclass
# ---------------------------------------------------------------------------


class TestAlert:
    def test_defaults(self):
        a = Alert(title="T", message="M")
        assert a.severity == AlertSeverity.MEDIUM
        assert a.metadata == {}

    def test_custom_fields(self):
        a = Alert(
            title="Down",
            message="Service unreachable",
            severity=AlertSeverity.CRITICAL,
            metadata={"host": "prod-1"},
        )
        assert a.severity == AlertSeverity.CRITICAL
        assert a.metadata["host"] == "prod-1"

    def test_metadata_not_shared(self):
        a1 = Alert(title="A", message="A")
        a2 = Alert(title="B", message="B")
        a1.metadata["k"] = "v"
        assert "k" not in a2.metadata


# ---------------------------------------------------------------------------
# AlertChannel ABC
# ---------------------------------------------------------------------------


class TestAlertChannelABC:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            AlertChannel()  # type: ignore[abstract]

    def test_concrete_subclass_works(self):
        class Dummy(AlertChannel):
            def send(self, alert):
                return True

        d = Dummy()
        assert d.send(Alert(title="T", message="M")) is True


# ---------------------------------------------------------------------------
# Discord severity color map
# ---------------------------------------------------------------------------


class TestDiscordColorMap:
    def test_low_is_green(self):
        assert _DISCORD_SEVERITY_COLORS[AlertSeverity.LOW] == 0x00FF00

    def test_medium_is_yellow(self):
        assert _DISCORD_SEVERITY_COLORS[AlertSeverity.MEDIUM] == 0xFFFF00

    def test_high_is_orange(self):
        assert _DISCORD_SEVERITY_COLORS[AlertSeverity.HIGH] == 0xFF8800

    def test_critical_is_red(self):
        assert _DISCORD_SEVERITY_COLORS[AlertSeverity.CRITICAL] == 0xFF0000


# ---------------------------------------------------------------------------
# DiscordAlertChannel
# ---------------------------------------------------------------------------


def _make_mock_urlopen():
    """Return a context-manager mock suitable for patching urlopen."""
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cm)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


class TestDiscordAlertChannel:
    def test_send_returns_true_on_success(self):
        channel = DiscordAlertChannel(webhook_url="https://discord.example/webhook")
        with patch("alerting.channels.urllib.request.urlopen", return_value=_make_mock_urlopen()):
            result = channel.send(Alert(title="Hi", message="World"))
        assert result is True

    def test_send_returns_false_on_error(self):
        channel = DiscordAlertChannel(webhook_url="https://discord.example/webhook")
        with patch("alerting.channels.urllib.request.urlopen", side_effect=OSError("network")):
            result = channel.send(Alert(title="Hi", message="World"))
        assert result is False

    def test_payload_includes_embed_with_title_and_description(self):
        channel = DiscordAlertChannel(webhook_url="https://discord.example/webhook")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["body"] = json.loads(req.data.decode())
            return _make_mock_urlopen()

        with patch("alerting.channels.urllib.request.urlopen", side_effect=fake_urlopen):
            channel.send(Alert(title="Test title", message="Test message"))

        embed = captured["body"]["embeds"][0]
        assert embed["title"] == "Test title"
        assert embed["description"] == "Test message"

    def test_severity_color_mapping(self):
        channel = DiscordAlertChannel(webhook_url="https://discord.example/webhook")
        for severity in AlertSeverity:
            captured = {}

            def fake_urlopen(req, timeout, sev=severity):
                captured["color"] = json.loads(req.data.decode())["embeds"][0]["color"]
                return _make_mock_urlopen()

            with patch("alerting.channels.urllib.request.urlopen", side_effect=fake_urlopen):
                channel.send(Alert(title="T", message="M", severity=severity))

            assert captured["color"] == _DISCORD_SEVERITY_COLORS[severity], (
                f"Wrong color for severity {severity}"
            )

    def test_metadata_included_as_fields(self):
        channel = DiscordAlertChannel(webhook_url="https://discord.example/webhook")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["body"] = json.loads(req.data.decode())
            return _make_mock_urlopen()

        alert = Alert(title="T", message="M", metadata={"host": "prod-1", "code": "500"})
        with patch("alerting.channels.urllib.request.urlopen", side_effect=fake_urlopen):
            channel.send(alert)

        fields = {f["name"]: f["value"] for f in captured["body"]["embeds"][0]["fields"]}
        assert fields["host"] == "prod-1"
        assert fields["code"] == "500"

    def test_uses_post_method(self):
        channel = DiscordAlertChannel(webhook_url="https://discord.example/webhook")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["method"] = req.method
            return _make_mock_urlopen()

        with patch("alerting.channels.urllib.request.urlopen", side_effect=fake_urlopen):
            channel.send(Alert(title="T", message="M"))

        assert captured["method"] == "POST"

    def test_content_type_header(self):
        channel = DiscordAlertChannel(webhook_url="https://discord.example/webhook")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["ct"] = req.get_header("Content-type")
            return _make_mock_urlopen()

        with patch("alerting.channels.urllib.request.urlopen", side_effect=fake_urlopen):
            channel.send(Alert(title="T", message="M"))

        assert captured["ct"] == "application/json"

    def test_http_error_returns_false(self):
        channel = DiscordAlertChannel(webhook_url="https://discord.example/webhook")
        http_err = urllib.error.HTTPError(
            url="https://discord.example/webhook",
            code=400,
            msg="Bad Request",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )
        with patch("alerting.channels.urllib.request.urlopen", side_effect=http_err):
            result = channel.send(Alert(title="T", message="M"))
        assert result is False

    def test_custom_timeout_forwarded_to_urlopen(self):
        channel = DiscordAlertChannel(webhook_url="https://discord.example/webhook", timeout=3)
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["timeout"] = timeout
            return MagicMock().__enter__.return_value

        with patch("alerting.channels.urllib.request.urlopen", side_effect=fake_urlopen):
            channel.send(Alert(title="T", message="M"))

        assert captured.get("timeout") == 3


# ---------------------------------------------------------------------------
# WebhookAlertChannel
# ---------------------------------------------------------------------------


class TestWebhookAlertChannel:
    def test_send_returns_true_on_success(self):
        channel = WebhookAlertChannel(url="https://hooks.example/notify")
        with patch("alerting.channels.urllib.request.urlopen", return_value=_make_mock_urlopen()):
            result = channel.send(Alert(title="Hi", message="World"))
        assert result is True

    def test_send_returns_false_on_error(self):
        channel = WebhookAlertChannel(url="https://hooks.example/notify")
        with patch("alerting.channels.urllib.request.urlopen", side_effect=OSError("network")):
            result = channel.send(Alert(title="Hi", message="World"))
        assert result is False

    def test_payload_shape(self):
        channel = WebhookAlertChannel(url="https://hooks.example/notify")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["body"] = json.loads(req.data.decode())
            return _make_mock_urlopen()

        alert = Alert(
            title="Deploy failed",
            message="Exit 1",
            severity=AlertSeverity.HIGH,
            metadata={"env": "prod"},
        )
        with patch("alerting.channels.urllib.request.urlopen", side_effect=fake_urlopen):
            channel.send(alert)

        body = captured["body"]
        assert body["title"] == "Deploy failed"
        assert body["message"] == "Exit 1"
        assert body["severity"] == "high"
        assert body["metadata"] == {"env": "prod"}

    def test_extra_headers_are_sent(self):
        channel = WebhookAlertChannel(
            url="https://hooks.example/notify",
            headers={"X-Api-Key": "secret"},
        )
        captured = {}

        def fake_urlopen(req, timeout):
            captured["key"] = req.get_header("X-api-key")
            return _make_mock_urlopen()

        with patch("alerting.channels.urllib.request.urlopen", side_effect=fake_urlopen):
            channel.send(Alert(title="T", message="M"))

        assert captured["key"] == "secret"

    def test_uses_post_method(self):
        channel = WebhookAlertChannel(url="https://hooks.example/notify")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["method"] = req.method
            return _make_mock_urlopen()

        with patch("alerting.channels.urllib.request.urlopen", side_effect=fake_urlopen):
            channel.send(Alert(title="T", message="M"))

        assert captured["method"] == "POST"

    def test_http_error_returns_false(self):
        channel = WebhookAlertChannel(url="https://hooks.example/notify")
        http_err = urllib.error.HTTPError(
            url="https://hooks.example/notify",
            code=401,
            msg="Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )
        with patch("alerting.channels.urllib.request.urlopen", side_effect=http_err):
            result = channel.send(Alert(title="T", message="M"))
        assert result is False

    def test_custom_timeout_forwarded_to_urlopen(self):
        channel = WebhookAlertChannel(url="https://hooks.example/notify", timeout=7)
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["timeout"] = timeout
            return MagicMock().__enter__.return_value

        with patch("alerting.channels.urllib.request.urlopen", side_effect=fake_urlopen):
            channel.send(Alert(title="T", message="M"))

        assert captured.get("timeout") == 7

    def test_non_serializable_metadata_does_not_raise(self):
        from datetime import datetime

        channel = WebhookAlertChannel(url="https://hooks.example/notify")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return MagicMock().__enter__.return_value

        alert = Alert(title="T", message="M", metadata={"ts": datetime(2024, 1, 1)})
        with patch("alerting.channels.urllib.request.urlopen", side_effect=fake_urlopen):
            result = channel.send(alert)

        assert result is True
        assert "ts" in captured["body"]["metadata"]


# ---------------------------------------------------------------------------
# Import smoke test — verify importable from gioe_libs.alerting
# ---------------------------------------------------------------------------


def test_importable_from_gioe_libs_alerting():
    from gioe_libs.alerting import (  # noqa: F401
        Alert,
        AlertChannel,
        AlertSeverity,
        DiscordAlertChannel,
        WebhookAlertChannel,
    )
