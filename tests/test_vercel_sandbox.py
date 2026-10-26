from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from anyio import create_task_group
from vercel.sandbox import (
    Process,
    Sandbox,
    SandboxFilesystem,
    SandboxPathNotFoundError,
    SandboxRuntimeSession,
    SandboxStatus,
)

import sandbox_agent.vercel_sandbox as sandbox_module
from sandbox_agent.config import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def sdk(monkeypatch):
    # Autospec checks the installed SDK's real method signatures and attributes.
    box = create_autospec(Sandbox, instance=True)
    box.name = "inspection-test"
    session = create_autospec(SandboxRuntimeSession, instance=True)
    session.status = SandboxStatus.RUNNING
    session.cwd = "/vercel"
    session.fs = create_autospec(SandboxFilesystem, instance=True)
    executor = create_autospec(Process, instance=True)
    executor.returncode = None
    executor.communicate.return_value = ("", "")
    session.run_process.return_value = SimpleNamespace(returncode=0, stdout="installed", stderr="")
    session.create_process.return_value = executor

    async def get_session():
        return session

    box.session.side_effect = get_session

    async def create(**kwargs):
        return box

    create_mock = create_autospec(sandbox_module.create_sandbox, side_effect=create)
    monkeypatch.setattr(sandbox_module, "create_sandbox", create_mock)
    return SimpleNamespace(box=box, session=session, executor=executor, create=create_mock)


async def _start(repo_url="https://github.com/example/project"):
    return await sandbox_module.start_sandbox(
        executor_api_key="executor-key",
        environment_id="environment_test",
        remote_url="https://api.openai.com/v1/agents/api?route=test",
        repo_url=repo_url,
        ref="main",
        prompt="Inspect the project",
        settings=Settings(
            openai_api_key="application-key",
            openai_executor_api_key="executor-key",
            agents_api_url="https://api.openai.com/v1",
            agent_model="gpt-5.6-sol",
            agent_reasoning_effort="low",
            codex_package="@openai/codex@alpha",
            sandbox_timeout_ms=60_000,
            artifact_max_bytes=10_000,
            sandbox_allowed_domains=("api.openai.com", "github.com"),
        ),
    )


@pytest.mark.anyio
async def test_one_shot_sandbox_preserves_limits_and_restricted_executor_key(sdk):
    handle = await _start()
    kwargs = sdk.create.call_args.kwargs
    assert kwargs["persistent"] is False
    assert kwargs["image"] == "vercel/sandbox/node:24"
    assert kwargs["execution_time_limit"] == timedelta(seconds=60)
    assert kwargs["network_policy"].mode == "custom"
    assert dict(kwargs["network_policy"].allow) == {"api.openai.com": (), "github.com": ()}
    assert kwargs["source"].url == "https://github.com/example/project"
    assert kwargs["source"].revision == "main"
    assert kwargs["source"].depth == 1
    assert "env" not in kwargs
    sdk.session.run_process.assert_awaited_once_with(
        "npm", ["install", "-g", "@openai/codex@alpha"], sudo=True, capture_output=True
    )
    sdk.session.create_process.assert_awaited_once_with(
        "codex",
        [
            "exec-server",
            "--remote",
            "https://api.openai.com/v1/agents/api?route=test",
            "--environment-id",
            "environment_test",
        ],
        cwd="/vercel/sandbox",
        env={"CODEX_API_KEY": "executor-key"},
    )
    sdk.session.fs.write_bytes.assert_awaited_once_with(
        "/tmp/sandbox-agent/prompt.txt", b"Inspect the project"
    )
    assert handle.sandbox.sandbox_id == "inspection-test"
    sdk.box.destroy.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["project", "project.git", "sandbox"])
async def test_image_checkout_is_normalized_to_the_agents_api_workspace(sdk, name):
    await _start(repo_url=f"https://github.com/example/{name}")
    if name == "sandbox":
        sdk.session.fs.rename.assert_not_awaited()
    else:
        source, destination = sdk.session.fs.rename.call_args.args
        assert str(source) == "/vercel/project"
        assert destination == "/vercel/sandbox"


@pytest.mark.anyio
async def test_startup_cancellation_releases_allocated_sandbox(sdk):
    installing = asyncio.Event()

    async def install(*args, **kwargs):
        installing.set()
        await asyncio.Event().wait()

    sdk.session.run_process.side_effect = install
    async with create_task_group() as tasks:
        tasks.start_soon(_start)
        await installing.wait()
        tasks.cancel_scope.cancel()
    sdk.box.stop.assert_awaited_once()
    sdk.box.destroy.assert_awaited_once_with(delete_orphan_snapshots=True)


@pytest.mark.anyio
@pytest.mark.parametrize("failure_stage", ["session", "checkout", "install", "executor"])
async def test_startup_failure_still_destroys_when_stop_fails(sdk, failure_stage):
    target = {
        "session": sdk.box.session,
        "checkout": sdk.session.fs.rename,
        "install": sdk.session.run_process,
        "executor": sdk.session.create_process,
    }[failure_stage]
    target.side_effect = RuntimeError("startup failed")
    sdk.box.stop.side_effect = RuntimeError("stop failed")
    with pytest.raises(RuntimeError, match="startup failed"):
        await _start()
    sdk.box.destroy.assert_awaited_once_with(delete_orphan_snapshots=True)


@pytest.mark.anyio
async def test_failed_install_reports_stderr_and_cleans_up(sdk):
    sdk.session.run_process.return_value = SimpleNamespace(
        returncode=1, stdout="", stderr="npm installation error"
    )
    with pytest.raises(RuntimeError, match="Codex installation failed: npm installation error"):
        await _start()
    sdk.session.create_process.assert_not_awaited()
    sdk.box.destroy.assert_awaited_once_with(delete_orphan_snapshots=True)


@pytest.mark.anyio
async def test_cleanup_destroys_sandbox_even_when_exact_session_stop_fails(sdk):
    handle = await _start()
    sdk.session.stop.side_effect = RuntimeError("stop failed")
    with pytest.raises(RuntimeError, match="stop failed"):
        await handle.sandbox.stop(blocking=True)
    sdk.session.stop.assert_awaited_once()
    sdk.box.destroy.assert_awaited_once_with(delete_orphan_snapshots=True)


@pytest.mark.anyio
async def test_artifact_io_uses_original_session_and_handles_only_missing_paths(sdk):
    handle = await _start()
    sdk.session.fs.read_bytes.return_value = b"report"
    assert await handle.sandbox.read_file("/tmp/report") == b"report"
    missing = SandboxPathNotFoundError("/tmp/missing", operation="read", cwd=None, cause=None)
    sdk.session.fs.read_bytes.side_effect = missing
    assert await handle.sandbox.read_file("/tmp/missing") is None
    sdk.session.fs.read_bytes.side_effect = RuntimeError("connection failed")
    with pytest.raises(RuntimeError, match="connection failed"):
        await handle.sandbox.read_file("/tmp/report")
    await handle.sandbox.write_files([{"path": "/tmp/events", "content": b"event"}])
    sdk.session.fs.write_bytes.assert_awaited_with("/tmp/events", b"event")
    sdk.box.session.assert_called_once()


@pytest.mark.anyio
async def test_health_check_does_not_resume_stopped_session(sdk):
    handle = await _start()
    sdk.session.status = SandboxStatus.STOPPED
    with pytest.raises(RuntimeError, match="Vercel Sandbox is stopped"):
        await sandbox_module.ensure_sandbox_running(handle)
    sdk.session.refresh.assert_awaited_once()
    sdk.executor.refresh.assert_not_awaited()
    sdk.box.session.assert_called_once()


@pytest.mark.anyio
@pytest.mark.parametrize("returncode", [None, 0, 2])
async def test_health_check_detects_executor_exit_with_output(sdk, returncode):
    handle = await _start()
    sdk.executor.returncode = returncode
    sdk.executor.communicate.return_value = ("", "executor stopped")
    if returncode is None:
        await sandbox_module.ensure_sandbox_running(handle)
        sdk.executor.communicate.assert_not_awaited()
    else:
        with pytest.raises(RuntimeError, match=f"code {returncode}: executor stopped"):
            await sandbox_module.ensure_sandbox_running(handle)
    sdk.executor.refresh.assert_awaited_once()
