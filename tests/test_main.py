from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

import sandbox_agent.main as main_module
from app import app
from sandbox_agent.config import Settings


def _settings() -> Settings:
    return Settings(
        openai_api_key="application-key",
        openai_executor_api_key="executor-key",
        agents_api_url="https://api.openai.com/v1",
        agent_model="gpt-5.6-sol",
        agent_reasoning_effort="low",
        codex_package="@openai/codex@alpha",
        sandbox_timeout_ms=60_000,
        artifact_max_bytes=10_000,
        sandbox_allowed_domains=("api.openai.com",),
    )


async def _stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
    yield 'event: complete\ndata: {"ok":true}\n\n'


def test_fastapi_serves_the_application_page() -> None:
    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert "Sandboxed Repo Agent" in response.text
    assert "Vercel Sandbox" in response.text


def test_inspection_endpoint_returns_event_stream(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "get_settings", _settings)
    monkeypatch.setattr(main_module, "stream_inspection", _stream)

    response = TestClient(app).post(
        "/api/inspect",
        json={
            "repo_url": "https://github.com/example/project",
            "ref": "main",
            "question": "How is this project structured?",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: complete" in response.text
