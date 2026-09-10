from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from dotenv import load_dotenv

ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]

ROOT = Path(__file__).resolve().parent.parent
for dotenv_file in (ROOT / ".env.local", ROOT / ".env", Path.cwd() / ".env.local"):
    load_dotenv(dotenv_file, override=False)


def _integer(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if parsed <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return parsed


def _reasoning_effort() -> ReasoningEffort:
    value = os.environ.get("AGENT_REASONING_EFFORT", "none").strip().lower()
    allowed = {"none", "low", "medium", "high", "xhigh", "max"}
    if value not in allowed:
        raise RuntimeError("AGENT_REASONING_EFFORT must be none, low, medium, high, xhigh, or max")
    return cast(ReasoningEffort, value)


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    openai_executor_api_key: str
    agents_api_url: str
    agent_model: str
    agent_reasoning_effort: ReasoningEffort
    codex_package: str
    sandbox_timeout_ms: int
    artifact_max_bytes: int
    sandbox_allowed_domains: tuple[str, ...]
    session_create_timeout_seconds: int = 20
    sandbox_startup_timeout_seconds: int = 90
    stream_execution_timeout_seconds: int = 150
    stream_no_progress_timeout_seconds: int = 45
    executor_healthcheck_interval_seconds: int = 5
    cleanup_timeout_seconds: int = 30


DEFAULT_SANDBOX_ALLOWED_DOMAINS = (
    "api.openai.com",
    "codex-cloud-environments.chatgpt.com",
    "registry.npmjs.org",
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "*.githubusercontent.com",
)


def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    values = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    if not values:
        raise RuntimeError(f"{name} must contain at least one domain")
    return values


def get_settings() -> Settings:
    application_key = os.environ.get("OPENAI_API_KEY", "").strip()
    executor_key = os.environ.get("OPENAI_EXECUTOR_API_KEY", "").strip()
    missing = [
        name
        for name, value in (
            ("OPENAI_API_KEY", application_key),
            ("OPENAI_EXECUTOR_API_KEY", executor_key),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Required settings are not configured: {', '.join(missing)}")
    if application_key == executor_key:
        raise RuntimeError("OPENAI_EXECUTOR_API_KEY must be separate from OPENAI_API_KEY")

    return Settings(
        openai_api_key=application_key,
        openai_executor_api_key=executor_key,
        # Accept the old preview endpoint while the public SDK appends /agents itself.
        agents_api_url=os.environ.get("AGENT_API_BASE_URL", "https://api.openai.com/v1")
        .rstrip("/")
        .removesuffix("/agents"),
        agent_model=os.environ.get("AGENT_MODEL", "gpt-5.6-sol"),
        agent_reasoning_effort=_reasoning_effort(),
        codex_package=os.environ.get("CODEX_PACKAGE", "@openai/codex@alpha"),
        sandbox_timeout_ms=_integer("SANDBOX_TIMEOUT_MS", 4 * 60 * 1000),
        artifact_max_bytes=_integer("ARTIFACT_MAX_BYTES", 200_000),
        sandbox_allowed_domains=_csv("SANDBOX_ALLOWED_DOMAINS", DEFAULT_SANDBOX_ALLOWED_DOMAINS),
        session_create_timeout_seconds=_integer("SESSION_CREATE_TIMEOUT_SECONDS", 20),
        sandbox_startup_timeout_seconds=_integer("SANDBOX_STARTUP_TIMEOUT_SECONDS", 90),
        stream_execution_timeout_seconds=_integer("STREAM_EXECUTION_TIMEOUT_SECONDS", 150),
        stream_no_progress_timeout_seconds=_integer("STREAM_NO_PROGRESS_TIMEOUT_SECONDS", 45),
        executor_healthcheck_interval_seconds=_integer("EXECUTOR_HEALTHCHECK_INTERVAL_SECONDS", 5),
        cleanup_timeout_seconds=_integer("CLEANUP_TIMEOUT_SECONDS", 30),
    )
