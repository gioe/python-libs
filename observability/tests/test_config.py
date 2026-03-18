"""Tests for observability configuration."""

import logging
import os
from pathlib import Path
from unittest import mock

import pytest

from libs.observability.config import (
    ConfigurationError,
    ObservabilityConfig,
    OTELConfig,
    RoutingConfig,
    SentryConfig,
    _dict_to_config,
    _process_config_values,
    _safe_float,
    _safe_int,
    _substitute_env_vars,
    load_config,
    validate_sentry_dsn_format,
)


class TestEnvVarSubstitution:
    """Tests for environment variable substitution."""

    def test_simple_substitution(self) -> None:
        """Test simple ${VAR} substitution."""
        with mock.patch.dict(os.environ, {"TEST_VAR": "test_value"}):
            result = _substitute_env_vars("prefix-${TEST_VAR}-suffix")
            assert result == "prefix-test_value-suffix"

    def test_missing_var_returns_empty(self) -> None:
        """Test missing variable returns empty string."""
        with mock.patch.dict(os.environ, {}, clear=True):
            # Ensure the var is not set
            os.environ.pop("MISSING_VAR", None)
            result = _substitute_env_vars("${MISSING_VAR}")
            assert result == ""

    def test_default_value_when_missing(self) -> None:
        """Test ${VAR:default} returns default when var not set."""
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MISSING_VAR", None)
            result = _substitute_env_vars("${MISSING_VAR:default_value}")
            assert result == "default_value"

    def test_default_value_ignored_when_set(self) -> None:
        """Test ${VAR:default} uses env var when set."""
        with mock.patch.dict(os.environ, {"SET_VAR": "actual_value"}):
            result = _substitute_env_vars("${SET_VAR:default_value}")
            assert result == "actual_value"

    def test_multiple_substitutions(self) -> None:
        """Test multiple variables in one string."""
        with mock.patch.dict(os.environ, {"VAR1": "one", "VAR2": "two"}):
            result = _substitute_env_vars("${VAR1}-${VAR2}")
            assert result == "one-two"

    def test_no_substitution_needed(self) -> None:
        """Test string without env vars passes through unchanged."""
        result = _substitute_env_vars("plain string")
        assert result == "plain string"

    def test_empty_default_value(self) -> None:
        """Test ${VAR:} with empty default."""
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MISSING_VAR", None)
            result = _substitute_env_vars("${MISSING_VAR:}")
            assert result == ""


class TestLoadConfig:
    """Tests for configuration loading."""

    def test_default_config_values(self) -> None:
        """Test default configuration values are applied."""
        with mock.patch.dict(
            os.environ,
            {"SENTRY_DSN": "https://test@sentry.io/123"},
            clear=True,
        ):
            config = load_config()

            # Sentry defaults
            assert config.sentry.enabled is True
            assert config.sentry.traces_sample_rate == 0.1
            assert config.sentry.send_default_pii is False

            # OTEL defaults
            assert config.otel.enabled is True
            assert config.otel.metrics_enabled is True
            assert config.otel.traces_enabled is True
            assert config.otel.insecure is False

            # Routing defaults
            assert config.routing.errors == "sentry"
            assert config.routing.metrics == "otel"
            assert config.routing.traces == "otel"

    def test_service_name_override(self) -> None:
        """Test service_name parameter overrides config."""
        with mock.patch.dict(
            os.environ,
            {"SENTRY_DSN": "https://test@sentry.io/123"},
        ):
            config = load_config(service_name="my-custom-service")
            assert config.otel.service_name == "my-custom-service"

    def test_environment_override(self) -> None:
        """Test environment parameter overrides config."""
        with mock.patch.dict(
            os.environ,
            {"SENTRY_DSN": "https://test@sentry.io/123"},
        ):
            config = load_config(environment="production")
            assert config.sentry.environment == "production"

    def test_kwargs_override_sentry(self) -> None:
        """Test sentry_* kwargs override config."""
        config = load_config(sentry_enabled=False)
        assert config.sentry.enabled is False

    def test_kwargs_override_otel(self) -> None:
        """Test otel_* kwargs override config."""
        with mock.patch.dict(
            os.environ,
            {"SENTRY_DSN": "https://test@sentry.io/123"},
        ):
            config = load_config(otel_endpoint="http://localhost:4317")
            assert config.otel.endpoint == "http://localhost:4317"

    def test_kwargs_override_routing(self) -> None:
        """Test routing_* kwargs override config."""
        with mock.patch.dict(
            os.environ,
            {"SENTRY_DSN": "https://test@sentry.io/123"},
        ):
            config = load_config(routing_traces="both")
            assert config.routing.traces == "both"

    def test_env_var_in_config(self) -> None:
        """Test environment variables are substituted in config."""
        with mock.patch.dict(os.environ, {"SENTRY_DSN": "https://test@sentry.io/123"}):
            config = load_config()
            assert config.sentry.dsn == "https://test@sentry.io/123"


class TestDataclassDefaults:
    """Tests for configuration dataclass defaults."""

    def test_sentry_config_defaults(self) -> None:
        """Test SentryConfig default values."""
        config = SentryConfig()
        assert config.enabled is True
        assert config.dsn is None
        assert config.environment == "development"
        assert config.release is None
        assert config.traces_sample_rate == 0.1
        assert config.profiles_sample_rate == 0.0
        assert config.send_default_pii is False

    def test_otel_config_defaults(self) -> None:
        """Test OTELConfig default values."""
        config = OTELConfig()
        assert config.enabled is True
        assert config.service_name == "unknown-service"
        assert config.service_version is None
        assert config.endpoint is None
        assert config.exporter == "otlp"
        assert config.otlp_headers == ""
        assert config.metrics_enabled is True
        assert config.metrics_export_interval_millis == 60000
        assert config.traces_enabled is True
        assert config.traces_sample_rate == 1.0
        assert config.logs_enabled is False
        assert config.prometheus_enabled is True
        assert config.insecure is False

    def test_routing_config_defaults(self) -> None:
        """Test RoutingConfig default values."""
        config = RoutingConfig()
        assert config.errors == "sentry"
        assert config.metrics == "otel"
        assert config.traces == "otel"

    def test_observability_config_defaults(self) -> None:
        """Test ObservabilityConfig creates nested defaults."""
        config = ObservabilityConfig()
        assert isinstance(config.sentry, SentryConfig)
        assert isinstance(config.otel, OTELConfig)
        assert isinstance(config.routing, RoutingConfig)


class TestConfigValidation:
    """Tests for configuration validation."""

    def test_valid_config_passes_validation(self) -> None:
        """Test that a valid configuration passes validation."""
        config = ObservabilityConfig(
            sentry=SentryConfig(
                enabled=True,
                dsn="https://test@sentry.io/123",
                traces_sample_rate=0.1,
                profiles_sample_rate=0.0,
            ),
            otel=OTELConfig(enabled=True, endpoint="http://localhost:4317"),
            routing=RoutingConfig(errors="sentry", metrics="otel", traces="otel"),
        )
        # Should not raise
        config.validate()

    def test_missing_sentry_dsn_when_enabled_raises_error(self) -> None:
        """Test that missing Sentry DSN when enabled raises ConfigurationError."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn=None),
        )
        with pytest.raises(ConfigurationError) as exc_info:
            config.validate()

        error_message = str(exc_info.value)
        assert "Sentry DSN is required when sentry.enabled=True" in error_message
        assert "SENTRY_DSN" in error_message

    def test_empty_sentry_dsn_when_enabled_raises_error(self) -> None:
        """Test that empty string Sentry DSN when enabled raises ConfigurationError."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn=""),
        )
        with pytest.raises(ConfigurationError) as exc_info:
            config.validate()

        assert "Sentry DSN is required" in str(exc_info.value)

    def test_missing_sentry_dsn_when_disabled_passes(self) -> None:
        """Test that missing Sentry DSN is OK when Sentry is disabled."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=False, dsn=None),
        )
        # Should not raise
        config.validate()

    def test_invalid_traces_sample_rate_too_high_raises_error(self) -> None:
        """Test that traces_sample_rate > 1.0 raises ConfigurationError."""
        config = ObservabilityConfig(
            sentry=SentryConfig(
                enabled=True,
                dsn="https://test@sentry.io/123",
                traces_sample_rate=1.5,
            ),
        )
        with pytest.raises(ConfigurationError) as exc_info:
            config.validate()

        error_message = str(exc_info.value)
        assert "Invalid sentry.traces_sample_rate: 1.5" in error_message
        assert "must be between 0.0 and 1.0" in error_message

    def test_invalid_traces_sample_rate_negative_raises_error(self) -> None:
        """Test that traces_sample_rate < 0.0 raises ConfigurationError."""
        config = ObservabilityConfig(
            sentry=SentryConfig(
                enabled=True,
                dsn="https://test@sentry.io/123",
                traces_sample_rate=-0.1,
            ),
        )
        with pytest.raises(ConfigurationError) as exc_info:
            config.validate()

        error_message = str(exc_info.value)
        assert "Invalid sentry.traces_sample_rate: -0.1" in error_message

    def test_invalid_profiles_sample_rate_too_high_raises_error(self) -> None:
        """Test that profiles_sample_rate > 1.0 raises ConfigurationError."""
        config = ObservabilityConfig(
            sentry=SentryConfig(
                enabled=True,
                dsn="https://test@sentry.io/123",
                profiles_sample_rate=2.0,
            ),
        )
        with pytest.raises(ConfigurationError) as exc_info:
            config.validate()

        error_message = str(exc_info.value)
        assert "Invalid sentry.profiles_sample_rate: 2.0" in error_message
        assert "must be between 0.0 and 1.0" in error_message

    def test_invalid_profiles_sample_rate_negative_raises_error(self) -> None:
        """Test that profiles_sample_rate < 0.0 raises ConfigurationError."""
        config = ObservabilityConfig(
            sentry=SentryConfig(
                enabled=True,
                dsn="https://test@sentry.io/123",
                profiles_sample_rate=-0.5,
            ),
        )
        with pytest.raises(ConfigurationError) as exc_info:
            config.validate()

        error_message = str(exc_info.value)
        assert "Invalid sentry.profiles_sample_rate: -0.5" in error_message

    def test_valid_sample_rates_at_boundaries(self) -> None:
        """Test that sample rates at 0.0 and 1.0 are valid."""
        config = ObservabilityConfig(
            sentry=SentryConfig(
                enabled=True,
                dsn="https://test@sentry.io/123",
                traces_sample_rate=0.0,
                profiles_sample_rate=1.0,
            ),
        )
        # Should not raise
        config.validate()

    def test_invalid_routing_errors_value_raises_error(self) -> None:
        """Test that invalid routing.errors value raises ConfigurationError."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
            routing=RoutingConfig(errors="invalid"),  # type: ignore
        )
        with pytest.raises(ConfigurationError) as exc_info:
            config.validate()

        error_message = str(exc_info.value)
        assert "Invalid routing.errors: 'invalid'" in error_message
        assert "both, otel, sentry" in error_message

    def test_invalid_routing_metrics_value_raises_error(self) -> None:
        """Test that invalid routing.metrics value raises ConfigurationError."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
            routing=RoutingConfig(metrics="datadog"),  # type: ignore
        )
        with pytest.raises(ConfigurationError) as exc_info:
            config.validate()

        error_message = str(exc_info.value)
        assert "Invalid routing.metrics: 'datadog'" in error_message

    def test_invalid_routing_traces_value_raises_error(self) -> None:
        """Test that invalid routing.traces value raises ConfigurationError."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
            routing=RoutingConfig(traces="zipkin"),  # type: ignore
        )
        with pytest.raises(ConfigurationError) as exc_info:
            config.validate()

        error_message = str(exc_info.value)
        assert "Invalid routing.traces: 'zipkin'" in error_message

    def test_all_valid_routing_values_pass(self) -> None:
        """Test that all valid routing values pass validation."""
        for routing_value in ["sentry", "otel", "both"]:
            config = ObservabilityConfig(
                sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
                routing=RoutingConfig(
                    errors=routing_value,  # type: ignore
                    metrics=routing_value,  # type: ignore
                    traces=routing_value,  # type: ignore
                ),
            )
            # Should not raise
            config.validate()

    def test_multiple_errors_aggregated_into_one_exception(self) -> None:
        """Test that multiple validation errors are aggregated into a single exception."""
        config = ObservabilityConfig(
            sentry=SentryConfig(
                enabled=True,
                dsn=None,  # Error 1: Missing DSN
                traces_sample_rate=1.5,  # Error 2: Invalid sample rate
                profiles_sample_rate=-0.1,  # Error 3: Invalid sample rate
            ),
            routing=RoutingConfig(
                errors="invalid",  # Error 4: Invalid routing value
                metrics="datadog",  # Error 5: Invalid routing value
            ),
        )
        with pytest.raises(ConfigurationError) as exc_info:
            config.validate()

        error_message = str(exc_info.value)
        # Check that all errors are present
        assert "Sentry DSN is required" in error_message
        assert "Invalid sentry.traces_sample_rate: 1.5" in error_message
        assert "Invalid sentry.profiles_sample_rate: -0.1" in error_message
        assert "Invalid routing.errors: 'invalid'" in error_message
        assert "Invalid routing.metrics: 'datadog'" in error_message
        # Check that errors are listed with bullets
        assert "Configuration validation failed:" in error_message
        assert error_message.count("  - ") == 5

    def test_missing_otel_endpoint_when_otel_routing_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that missing OTEL endpoint when routing to OTEL logs a warning."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
            otel=OTELConfig(enabled=True, endpoint=None, exporter="otlp"),
            routing=RoutingConfig(errors="otel"),
        )

        with caplog.at_level(logging.WARNING):
            config.validate()

        # Should log warning but not raise
        assert len(caplog.records) == 1
        assert "OTEL endpoint is not configured" in caplog.text
        assert "OTLP exporter" in caplog.text
        assert "OTEL_ENDPOINT" in caplog.text

    def test_missing_otel_endpoint_when_routing_both_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that missing OTEL endpoint when routing to 'both' logs a warning."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
            otel=OTELConfig(enabled=True, endpoint=None),
            routing=RoutingConfig(traces="both"),
        )

        with caplog.at_level(logging.WARNING):
            config.validate()

        assert "OTEL endpoint is not configured" in caplog.text

    def test_missing_otel_endpoint_when_only_sentry_routing_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that missing OTEL endpoint doesn't warn when routing only to Sentry."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
            otel=OTELConfig(enabled=True, endpoint=None),
            routing=RoutingConfig(errors="sentry", metrics="sentry", traces="sentry"),
        )

        with caplog.at_level(logging.WARNING):
            config.validate()

        assert len(caplog.records) == 0

    def test_missing_otel_endpoint_when_otel_disabled_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that missing OTEL endpoint doesn't warn when OTEL is disabled."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
            otel=OTELConfig(enabled=False, endpoint=None),
            routing=RoutingConfig(errors="otel"),
        )

        with caplog.at_level(logging.WARNING):
            config.validate()

        assert len(caplog.records) == 0

    def test_invalid_otel_traces_sample_rate_raises_error(self) -> None:
        """Test that OTEL traces_sample_rate > 1.0 raises ConfigurationError."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
            otel=OTELConfig(enabled=True, traces_sample_rate=1.5),
        )
        with pytest.raises(ConfigurationError) as exc_info:
            config.validate()

        error_message = str(exc_info.value)
        assert "Invalid otel.traces_sample_rate: 1.5" in error_message
        assert "must be between 0.0 and 1.0" in error_message

    def test_invalid_otel_traces_sample_rate_negative_raises_error(self) -> None:
        """Test that OTEL traces_sample_rate < 0.0 raises ConfigurationError."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
            otel=OTELConfig(enabled=True, traces_sample_rate=-0.1),
        )
        with pytest.raises(ConfigurationError) as exc_info:
            config.validate()

        assert "Invalid otel.traces_sample_rate: -0.1" in str(exc_info.value)

    def test_invalid_otel_exporter_raises_error(self) -> None:
        """Test that invalid OTEL exporter value raises ConfigurationError."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
            otel=OTELConfig(enabled=True, exporter="jaeger"),  # type: ignore
        )
        with pytest.raises(ConfigurationError) as exc_info:
            config.validate()

        error_message = str(exc_info.value)
        assert "Invalid otel.exporter: 'jaeger'" in error_message
        assert "console, none, otlp" in error_message

    def test_invalid_otel_metrics_export_interval_raises_error(self) -> None:
        """Test that negative metrics_export_interval_millis raises ConfigurationError."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
            otel=OTELConfig(enabled=True, metrics_export_interval_millis=-1000),
        )
        with pytest.raises(ConfigurationError) as exc_info:
            config.validate()

        error_message = str(exc_info.value)
        assert "Invalid otel.metrics_export_interval_millis: -1000" in error_message
        assert "must be positive" in error_message

    def test_zero_metrics_export_interval_raises_error(self) -> None:
        """Test that zero metrics_export_interval_millis raises ConfigurationError."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
            otel=OTELConfig(enabled=True, metrics_export_interval_millis=0),
        )
        with pytest.raises(ConfigurationError) as exc_info:
            config.validate()

        assert "must be positive" in str(exc_info.value)

    def test_missing_otel_endpoint_with_console_exporter_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that missing endpoint doesn't warn when using console exporter."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
            otel=OTELConfig(enabled=True, endpoint=None, exporter="console"),
            routing=RoutingConfig(errors="otel"),
        )

        with caplog.at_level(logging.WARNING):
            config.validate()

        # No warning because console exporter doesn't need endpoint
        assert len(caplog.records) == 0

    def test_all_valid_otel_exporter_values_pass(self) -> None:
        """Test that all valid OTEL exporter values pass validation."""
        for exporter in ["console", "otlp", "none"]:
            config = ObservabilityConfig(
                sentry=SentryConfig(enabled=True, dsn="https://test@sentry.io/123"),
                otel=OTELConfig(enabled=True, exporter=exporter),  # type: ignore
            )
            # Should not raise
            config.validate()

    def test_load_config_calls_validate(self) -> None:
        """Test that load_config() calls validate() on the returned config."""
        with mock.patch.dict(os.environ, {"SENTRY_DSN": ""}):
            # This should raise because Sentry is enabled by default with empty DSN
            with pytest.raises(ConfigurationError) as exc_info:
                load_config()

            assert "Sentry DSN is required" in str(exc_info.value)

    def test_load_config_with_valid_env_vars_passes_validation(self) -> None:
        """Test that load_config() with valid env vars passes validation."""
        with mock.patch.dict(
            os.environ,
            {"SENTRY_DSN": "https://test@sentry.io/123"},
        ):
            config = load_config()
            # Should not raise and should have the DSN set
            assert config.sentry.dsn == "https://test@sentry.io/123"

    def test_load_config_with_sentry_disabled_passes_validation(self) -> None:
        """Test that load_config() with Sentry disabled doesn't require DSN."""
        with mock.patch.dict(os.environ, {}, clear=True):
            config = load_config(sentry_enabled=False)
            # Should not raise even without DSN
            assert config.sentry.enabled is False

    def test_load_config_with_invalid_override_raises_error(self) -> None:
        """Test that load_config() with invalid override raises ConfigurationError."""
        with mock.patch.dict(
            os.environ,
            {"SENTRY_DSN": "https://test@sentry.io/123"},
        ):
            with pytest.raises(ConfigurationError) as exc_info:
                load_config(sentry_traces_sample_rate=2.0)

            assert "Invalid sentry.traces_sample_rate: 2.0" in str(exc_info.value)


class TestProcessConfigValues:
    """Tests for _process_config_values helper."""

    def test_list_with_env_var_strings(self) -> None:
        """Test list values with environment variable substitution."""
        with mock.patch.dict(os.environ, {"LIST_VAR": "substituted"}):
            config = {"items": ["${LIST_VAR}", "plain"]}
            result = _process_config_values(config)
            assert result["items"] == ["substituted", "plain"]

    def test_list_with_non_string_items(self) -> None:
        """Test list values with non-string items pass through unchanged."""
        config = {"numbers": [1, 2, 3], "mixed": ["string", 42, True]}
        result = _process_config_values(config)
        assert result["numbers"] == [1, 2, 3]
        assert result["mixed"] == ["string", 42, True]


class TestYAMLLoading:
    """Tests for YAML file loading."""

    def test_load_config_with_invalid_yaml_syntax(self, tmp_path: Path) -> None:
        """Test that invalid YAML syntax raises yaml.YAMLError."""
        import yaml

        invalid_yaml = tmp_path / "invalid.yaml"
        invalid_yaml.write_text("{ invalid yaml syntax: [")

        with pytest.raises(yaml.YAMLError):
            load_config(config_path=str(invalid_yaml))

    def test_load_yaml_import_error(self) -> None:
        """Test ImportError raised when PyYAML is not installed."""
        import builtins
        import sys

        import libs.observability.config as config_module

        # Save original import and remove yaml from sys.modules cache
        original_import = builtins.__import__
        yaml_module = sys.modules.pop("yaml", None)

        def mock_import(name: str, *args, **kwargs):
            if name == "yaml":
                raise ImportError("No module named 'yaml'")
            return original_import(name, *args, **kwargs)

        try:
            with mock.patch.object(builtins, "__import__", side_effect=mock_import):
                with pytest.raises(ImportError) as exc_info:
                    config_module._load_yaml(Path("/nonexistent/path.yaml"))

                assert "PyYAML is required" in str(exc_info.value)
        finally:
            # Restore yaml module to sys.modules cache
            if yaml_module is not None:
                sys.modules["yaml"] = yaml_module


class TestConfigFileMerging:
    """Tests for configuration file merging."""

    def test_load_config_with_custom_yaml_file(self, tmp_path: Path) -> None:
        """Test loading config from a custom YAML file merges with defaults."""
        custom_config = tmp_path / "custom.yaml"
        custom_config.write_text(
            """
sentry:
  traces_sample_rate: 0.5
  environment: staging
otel:
  service_name: custom-service
"""
        )

        with mock.patch.dict(
            os.environ,
            {"SENTRY_DSN": "https://test@sentry.io/123"},
        ):
            config = load_config(config_path=str(custom_config))

            # Custom values should override defaults
            assert config.sentry.traces_sample_rate == 0.5
            assert config.sentry.environment == "staging"
            assert config.otel.service_name == "custom-service"

            # Default values should be preserved
            assert config.sentry.enabled is True
            assert config.otel.metrics_enabled is True

    def test_load_config_with_nonexistent_file(self) -> None:
        """Test loading config with nonexistent file gracefully falls back to defaults."""
        with mock.patch.dict(
            os.environ,
            {"SENTRY_DSN": "https://test@sentry.io/123"},
        ):
            # Should not raise an error when file doesn't exist
            config = load_config(config_path="/nonexistent/path/config.yaml")

            # Should still work with default config
            assert config.sentry.enabled is True
            assert config.otel.enabled is True

    def test_load_config_merges_nested_dict_values(self, tmp_path: Path) -> None:
        """Test that nested dict values are properly merged (not replaced)."""
        custom_config = tmp_path / "custom.yaml"
        custom_config.write_text(
            """
sentry:
  traces_sample_rate: 0.8
routing:
  traces: both
"""
        )

        with mock.patch.dict(
            os.environ,
            {"SENTRY_DSN": "https://test@sentry.io/123"},
        ):
            config = load_config(config_path=str(custom_config))

            # Custom values
            assert config.sentry.traces_sample_rate == 0.8
            assert config.routing.traces == "both"

            # Default routing values should be preserved (from default.yaml merge)
            assert config.routing.errors == "sentry"
            assert config.routing.metrics == "otel"

    def test_load_config_custom_file_overwrites_top_level(self, tmp_path: Path) -> None:
        """Test that non-dict top-level values are overwritten, not merged."""
        custom_config = tmp_path / "custom.yaml"
        custom_config.write_text(
            """
custom_key: custom_value
"""
        )

        with mock.patch.dict(
            os.environ,
            {"SENTRY_DSN": "https://test@sentry.io/123"},
        ):
            config = load_config(config_path=str(custom_config))

            # Config should still be valid
            assert config.sentry.enabled is True


class TestSafeConversionHelpers:
    """Tests for safe type conversion helper functions."""

    def test_safe_float_with_valid_float(self) -> None:
        """Test _safe_float with valid float value."""
        result = _safe_float(0.5, 0.0, "test_field")
        assert result == 0.5

    def test_safe_float_with_valid_int(self) -> None:
        """Test _safe_float with valid integer value."""
        result = _safe_float(1, 0.0, "test_field")
        assert result == 1.0

    def test_safe_float_with_valid_string(self) -> None:
        """Test _safe_float with valid string value."""
        result = _safe_float("0.75", 0.0, "test_field")
        assert result == 0.75

    def test_safe_float_with_none_returns_default(self) -> None:
        """Test _safe_float with None returns default."""
        result = _safe_float(None, 0.5, "test_field")
        assert result == 0.5

    def test_safe_float_with_invalid_string_raises_error(self) -> None:
        """Test _safe_float with invalid string raises ConfigurationError."""
        with pytest.raises(ConfigurationError) as exc_info:
            _safe_float("not-a-number", 0.0, "sentry.traces_sample_rate")

        error_message = str(exc_info.value)
        assert "sentry.traces_sample_rate" in error_message
        assert "'not-a-number'" in error_message
        assert "cannot be converted to float" in error_message

    def test_safe_int_with_valid_int(self) -> None:
        """Test _safe_int with valid integer value."""
        result = _safe_int(60000, 0, "test_field")
        assert result == 60000

    def test_safe_int_with_valid_float(self) -> None:
        """Test _safe_int with valid float value (truncates)."""
        result = _safe_int(30000.9, 0, "test_field")
        assert result == 30000

    def test_safe_int_with_valid_string(self) -> None:
        """Test _safe_int with valid string value."""
        result = _safe_int("45000", 0, "test_field")
        assert result == 45000

    def test_safe_int_with_none_returns_default(self) -> None:
        """Test _safe_int with None returns default."""
        result = _safe_int(None, 60000, "test_field")
        assert result == 60000

    def test_safe_int_with_invalid_string_raises_error(self) -> None:
        """Test _safe_int with invalid string raises ConfigurationError."""
        with pytest.raises(ConfigurationError) as exc_info:
            _safe_int("not-an-integer", 0, "otel.metrics_export_interval_millis")

        error_message = str(exc_info.value)
        assert "otel.metrics_export_interval_millis" in error_message
        assert "'not-an-integer'" in error_message
        assert "cannot be converted to integer" in error_message

    def test_dict_to_config_with_invalid_sample_rate(self) -> None:
        """Test _dict_to_config with invalid sample rate raises helpful error."""
        data = {
            "sentry": {"traces_sample_rate": "invalid"},
        }
        with pytest.raises(ConfigurationError) as exc_info:
            _dict_to_config(data)

        error_message = str(exc_info.value)
        assert "sentry.traces_sample_rate" in error_message
        assert "cannot be converted to float" in error_message

    def test_dict_to_config_with_invalid_export_interval(self) -> None:
        """Test _dict_to_config with invalid export interval raises helpful error."""
        data = {
            "otel": {"metrics_export_interval_millis": "not-a-number"},
        }
        with pytest.raises(ConfigurationError) as exc_info:
            _dict_to_config(data)

        error_message = str(exc_info.value)
        assert "otel.metrics_export_interval_millis" in error_message
        assert "cannot be converted to integer" in error_message


class TestSentryDSNValidation:
    """Tests for Sentry DSN format validation."""

    def test_valid_dsn_basic(self) -> None:
        """Test valid DSN with basic format."""
        errors = validate_sentry_dsn_format("https://abc123@sentry.io/456")
        assert errors == []

    def test_valid_dsn_with_secret_key(self) -> None:
        """Test valid DSN with optional secret key (deprecated but valid)."""
        errors = validate_sentry_dsn_format("https://public:secret@sentry.io/789")
        assert errors == []

    def test_valid_dsn_with_subdomain(self) -> None:
        """Test valid DSN with organization subdomain."""
        errors = validate_sentry_dsn_format("https://abc123@o123.ingest.sentry.io/456789")
        assert errors == []

    def test_valid_dsn_with_custom_host(self) -> None:
        """Test valid DSN with custom self-hosted Sentry."""
        errors = validate_sentry_dsn_format("https://key@sentry.mycompany.com/1")
        assert errors == []

    def test_valid_dsn_with_port(self) -> None:
        """Test valid DSN with custom port."""
        errors = validate_sentry_dsn_format("https://key@sentry.local:9000/123")
        assert errors == []

    def test_valid_dsn_http_protocol(self) -> None:
        """Test valid DSN with http protocol (for local development)."""
        errors = validate_sentry_dsn_format("http://key@localhost/1")
        assert errors == []

    def test_invalid_dsn_wrong_protocol(self) -> None:
        """Test DSN with invalid protocol."""
        errors = validate_sentry_dsn_format("ftp://key@sentry.io/123")
        assert len(errors) == 1
        assert "Invalid DSN protocol: 'ftp'" in errors[0]
        assert "Must be 'http' or 'https'" in errors[0]

    def test_invalid_dsn_missing_public_key(self) -> None:
        """Test DSN without public key."""
        errors = validate_sentry_dsn_format("https://sentry.io/123")
        assert len(errors) == 1
        assert "missing public key" in errors[0]

    def test_invalid_dsn_missing_host(self) -> None:
        """Test DSN without host."""
        # This is a malformed URL that won't have a hostname
        errors = validate_sentry_dsn_format("https://key@/123")
        assert len(errors) == 1
        assert "missing host" in errors[0]

    def test_invalid_dsn_missing_project_id(self) -> None:
        """Test DSN without project ID."""
        errors = validate_sentry_dsn_format("https://key@sentry.io/")
        assert len(errors) == 1
        assert "missing project ID" in errors[0]

    def test_invalid_dsn_non_numeric_project_id(self) -> None:
        """Test DSN with non-numeric project ID."""
        errors = validate_sentry_dsn_format("https://key@sentry.io/my-project")
        assert len(errors) == 1
        assert "project ID must be numeric" in errors[0]
        assert "'my-project'" in errors[0]

    def test_invalid_dsn_multiple_errors(self) -> None:
        """Test DSN with multiple validation errors."""
        errors = validate_sentry_dsn_format("ftp://sentry.io/abc")
        assert len(errors) >= 2
        # Should have protocol error and missing public key error

    def test_invalid_dsn_completely_malformed(self) -> None:
        """Test completely malformed DSN."""
        errors = validate_sentry_dsn_format("not-a-url-at-all")
        # Should have errors for missing components
        assert len(errors) >= 1

    def test_invalid_dsn_empty_string(self) -> None:
        """Test empty DSN string."""
        errors = validate_sentry_dsn_format("")
        # Should have errors for missing protocol and components
        assert len(errors) >= 1

    def test_config_validation_catches_malformed_dsn(self) -> None:
        """Test that config validation catches malformed DSN."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://sentry.io/123"),
        )
        with pytest.raises(ConfigurationError) as exc_info:
            config.validate()

        error_message = str(exc_info.value)
        assert "missing public key" in error_message

    def test_config_validation_catches_non_numeric_project_id(self) -> None:
        """Test that config validation catches non-numeric project ID."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://key@sentry.io/my-project"),
        )
        with pytest.raises(ConfigurationError) as exc_info:
            config.validate()

        error_message = str(exc_info.value)
        assert "project ID must be numeric" in error_message

    def test_config_validation_accepts_valid_dsn(self) -> None:
        """Test that config validation accepts valid DSN."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=True, dsn="https://abc123@o456.ingest.sentry.io/789"),
        )
        # Should not raise
        config.validate()

    def test_config_validation_skips_dsn_format_check_when_disabled(self) -> None:
        """Test that DSN format is not checked when Sentry is disabled."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=False, dsn="invalid-dsn"),
        )
        # Should not raise even with invalid DSN because Sentry is disabled
        config.validate()

    def test_config_validation_skips_dsn_format_check_when_none(self) -> None:
        """Test that DSN format is not checked when DSN is None."""
        config = ObservabilityConfig(
            sentry=SentryConfig(enabled=False, dsn=None),
        )
        # Should not raise
        config.validate()

    def test_dsn_validation_with_path_segments(self) -> None:
        """Test DSN validation with extra path segments (not typical but test parsing)."""
        # Standard DSN only has project ID in path
        errors = validate_sentry_dsn_format("https://key@sentry.io/extra/path/123")
        # The path is 'extra/path/123' which is not a valid numeric project ID
        assert len(errors) == 1
        assert "project ID must be numeric" in errors[0]

    def test_dsn_validation_with_trailing_whitespace(self) -> None:
        """Test DSN with trailing whitespace fails validation.

        Trailing whitespace in DSN is a common config typo. urlparse treats trailing
        whitespace as part of the URL path, causing project ID validation to fail.
        """
        errors = validate_sentry_dsn_format("https://key@sentry.io/123 ")
        assert len(errors) == 1
        assert "project ID must be numeric" in errors[0]

    def test_dsn_validation_with_leading_whitespace_passes(self) -> None:
        """Test DSN with leading whitespace passes validation.

        Note: urlparse() strips leading whitespace, so leading spaces don't cause
        validation failures. This test documents the current behavior.
        """
        errors = validate_sentry_dsn_format(" https://key@sentry.io/123")
        # urlparse strips leading whitespace, so this parses correctly
        assert errors == []
