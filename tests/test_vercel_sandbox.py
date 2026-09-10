from __future__ import annotations

from typing import Any

import pytest

import sandbox_agent.vercel_sandbox as sandbox_module
from sandbox_agent.config import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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
        sandbox_allowed_domains=("api.openai.com", "github.com"),
    )


@pytest.mark.anyio
async def test_executor_receives_only_the_restricted_key(monkeypatch) -> None:
    calls: list[tuple[str, Any]] = []
    create_kwargs: dict[str, Any] = {}

    class Command:
        exit_code = 0

        async def output(self, *, stream: str) -> str:
            assert stream == "both"
            return "installed"

    class Sandbox:
        async def run_command(self, *args: Any, **kwargs: Any) -> Command:
            calls.append(("run", (args, kwargs)))
            return Command()

        async def mk_dir(self, path: str) -> None:
            calls.append(("mkdir", path))

        async def write_files(self, files: list[dict[str, Any]]) -> None:
            calls.append(("write", files))

        async def run_command_detached(self, *args: Any, **kwargs: Any) -> object:
            calls.append(("detached", (args, kwargs)))
            return object()

        async def stop(self, *, blocking: bool) -> None:
            calls.append(("stop", blocking))

    sandbox = Sandbox()

    async def create(**kwargs: Any) -> Sandbox:
        create_kwargs.update(kwargs)
        return sandbox

    monkeypatch.setattr(sandbox_module.AsyncSandbox, "create", create)

    await sandbox_module.start_sandbox(
        executor_api_key="executor-key",
        environment_id="environment_test",
        remote_url="https://api.openai.com/v1/agents/api?route=test",
        repo_url="https://github.com/example/project",
        ref="main",
        prompt="Inspect the project",
        settings=_settings(),
    )

    _, ((command, arguments), detached_kwargs) = next(
        call for call in calls if call[0] == "detached"
    )
    assert command == "codex"
    assert arguments == [
        "exec-server",
        "--remote",
        "https://api.openai.com/v1/agents/api?route=test",
        "--environment-id",
        "environment_test",
    ]
    assert detached_kwargs["env"] == {"CODEX_API_KEY": "executor-key"}
    assert "env" not in create_kwargs
    assert create_kwargs["network_policy"].allow == ["api.openai.com", "github.com"]
    assert create_kwargs["source"].url == "https://github.com/example/project"


@pytest.mark.anyio
async def test_startup_cancellation_stops_allocated_sandbox(monkeypatch):
    import asyncio

    from anyio import create_task_group

    installing = asyncio.Event()
    stopped = []

    class Sandbox:
        async def run_command(self, *args, **kwargs):
            installing.set()
            await asyncio.Event().wait()

        async def stop(self, *, blocking):
            await asyncio.sleep(0)
            stopped.append(blocking)

    async def create(**kwargs):
        return Sandbox()

    monkeypatch.setattr(sandbox_module.AsyncSandbox, "create", create)

    async def start():
        await sandbox_module.start_sandbox(
            executor_api_key="executor-key",
            environment_id="environment_test",
            remote_url="https://api.openai.com/v1/agents/api",
            repo_url="https://github.com/example/project",
            ref="main",
            prompt="Inspect",
            settings=_settings(),
        )

    async with create_task_group() as tasks:
        tasks.start_soon(start)
        await installing.wait()
        tasks.cancel_scope.cancel()
    assert stopped == [True]
