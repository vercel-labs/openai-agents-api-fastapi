from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from anyio import CancelScope
from vercel.sandbox import AsyncSandbox, GitSource, NetworkPolicyCustom, SandboxStatus

from sandbox_agent.config import Settings
from sandbox_agent.prompt import RESULT_DIRECTORY, WORKSPACE


@dataclass
class SandboxHandle:
    sandbox: Any
    executor: Any


async def start_sandbox(
    *,
    executor_api_key: str,
    environment_id: str,
    remote_url: str,
    repo_url: str,
    ref: str,
    prompt: str,
    settings: Settings,
) -> SandboxHandle:
    sandbox = await AsyncSandbox.create(
        source=GitSource(url=repo_url, revision=ref, depth=1),
        runtime="node24",
        timeout=settings.sandbox_timeout_ms,
        network_policy=NetworkPolicyCustom(allow=list(settings.sandbox_allowed_domains)),
    )

    try:
        install = await sandbox.run_command(
            "npm",
            ["install", "-g", settings.codex_package],
            sudo=True,
        )
        install_output = await install.output(stream="both")
        if install.exit_code != 0:
            raise RuntimeError(f"Codex installation failed: {install_output[-3000:]}")

        await sandbox.mk_dir(RESULT_DIRECTORY)
        await sandbox.write_files(
            [
                {
                    "path": f"{RESULT_DIRECTORY}/prompt.txt",
                    "content": prompt.encode("utf-8"),
                }
            ]
        )
        executor = await sandbox.run_command_detached(
            "codex",
            [
                "exec-server",
                "--remote",
                remote_url,
                "--environment-id",
                environment_id,
            ],
            cwd=WORKSPACE,
            env={"CODEX_API_KEY": executor_api_key},
        )
        return SandboxHandle(sandbox=sandbox, executor=executor)
    except BaseException:
        # Startup timeouts and caller cancellation must also release the sandbox.
        with CancelScope(shield=True), suppress(Exception):
            async with asyncio.timeout(settings.cleanup_timeout_seconds):
                await sandbox.stop(blocking=True)
        raise


async def ensure_sandbox_running(handle: SandboxHandle) -> None:
    await handle.sandbox.refresh()
    if handle.sandbox.status != SandboxStatus.RUNNING:
        raise RuntimeError(f"Vercel Sandbox is {handle.sandbox.status.value}")

    command = await handle.sandbox.get_command(handle.executor.cmd_id)
    exit_code = getattr(command.cmd, "exit_code", None)
    if exit_code is not None:
        output = await command.output(stream="both")
        detail = output.strip()[-3000:] or "no executor output"
        raise RuntimeError(f"Codex executor exited with code {exit_code}: {detail}")
