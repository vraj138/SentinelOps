"""Tests for sentinelops.config."""

import pytest

from sentinelops.config import Settings

ENV_VARS = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "PROMETHEUS_URL", "DATABASE_URL"]


def test_settings_load_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.anthropic_api_key is None
    assert settings.openai_api_key is None
    assert settings.prometheus_url == "http://localhost:9090"
    assert settings.database_url is None


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")

    settings = Settings(_env_file=None)

    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-test"
    assert settings.prometheus_url == "http://prometheus:9090"
