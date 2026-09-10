from __future__ import annotations

import pytest

from sandbox_agent.config import get_settings


def test_settings_require_both_openai_keys(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_EXECUTOR_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY, OPENAI_EXECUTOR_API_KEY"):
        get_settings()


def test_settings_use_short_no_reasoning_defaults(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "application-key")
    monkeypatch.setenv("OPENAI_EXECUTOR_API_KEY", "executor-key")
    monkeypatch.delenv("AGENT_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("STREAM_EXECUTION_TIMEOUT_SECONDS", raising=False)

    settings = get_settings()

    assert settings.agent_reasoning_effort == "none"
    assert settings.stream_execution_timeout_seconds == 150
    assert settings.sandbox_timeout_ms == 240_000


def test_settings_reject_reusing_application_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "shared-key")
    monkeypatch.setenv("OPENAI_EXECUTOR_API_KEY", "shared-key")

    with pytest.raises(RuntimeError, match="must be separate"):
        get_settings()


def test_settings_validate_reasoning_effort(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "application-key")
    monkeypatch.setenv("OPENAI_EXECUTOR_API_KEY", "executor-key")
    monkeypatch.setenv("AGENT_REASONING_EFFORT", "maximum-ish")

    with pytest.raises(RuntimeError, match="AGENT_REASONING_EFFORT"):
        get_settings()


@pytest.mark.parametrize(
    "configured,expected",
    [
        (None, "https://api.openai.com/v1"),
        ("https://api.openai.com/v1/agents/", "https://api.openai.com/v1"),
        ("https://api.openai.com/v1", "https://api.openai.com/v1"),
        ("https://proxy.example/v1/agents", "https://proxy.example/v1"),
    ],
)
def test_public_sdk_base_url_accepts_legacy_overrides(monkeypatch, configured, expected):
    monkeypatch.setenv("OPENAI_API_KEY", "application-key")
    monkeypatch.setenv("OPENAI_EXECUTOR_API_KEY", "executor-key")
    if configured is None:
        monkeypatch.delenv("AGENT_API_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("AGENT_API_BASE_URL", configured)
    assert get_settings().agents_api_url == expected
