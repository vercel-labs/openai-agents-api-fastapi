from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from anyio import CancelScope
from vercel.sandbox import (
    GitSource,
    NetworkPolicy,
    Process,
    Sandbox,
    SandboxPathNotFoundError,
    SandboxRuntimeSession,
    SandboxStatus,
    create_sandbox,
)

from sandbox_agent.config import Settings
from sandbox_agent.prompt import RESULT_DIRECTORY, WORKSPACE


@dataclass
class InspectionSandbox:
    box: Sandbox
    session: SandboxRuntimeSession

    @property
    def sandbox_id(self) -> str:
        return self.box.name

    async def write_files(self, files: list[dict[str, Any]]) -> None:
        for file in files:
            await self.session.fs.write_bytes(file["path"], file["content"])

    async def read_file(self, path: str) -> bytes | None:
        try:
            return await self.session.fs.read_bytes(path)
        except SandboxPathNotFoundError:
            return None

    async def stop(self, *, blocking: bool) -> None:
        # Both SDK calls wait for completion. Always destroy the one-shot resource,
        # even when stopping its runtime session fails.
        try:
            await self.session.stop()
        finally:
            await self.box.destroy(delete_orphan_snapshots=True)


@dataclass
class SandboxHandle:
    sandbox: InspectionSandbox
    executor: Process


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
    box = await create_sandbox(
        source=GitSource(url=repo_url, revision=ref, depth=1),
        image="vercel/sandbox/node:24",
        persistent=False,
        execution_time_limit=timedelta(milliseconds=settings.sandbox_timeout_ms),
        network_policy=NetworkPolicy.custom(
            allow={domain: () for domain in settings.sandbox_allowed_domains}
        ),
    )

    try:
        # Bind all operations to this exact session: a stopped inspection must
        # never resume automatically during health checks or artifact collection.
        session = await box.session()
        sandbox = InspectionSandbox(box=box, session=session)
        # Managed images clone GitSource into a repository-named directory under
        # the image's working directory. Keep the Agents API workspace stable.
        repository_name = PurePosixPath(urlsplit(repo_url).path).name.removesuffix(".git")
        checkout = PurePosixPath(session.cwd) / repository_name
        if checkout != PurePosixPath(WORKSPACE):
            await session.fs.rename(checkout, WORKSPACE)
        install = await session.run_process(
            "npm",
            ["install", "-g", settings.codex_package],
            sudo=True,
            capture_output=True,
        )
        install_output = (install.stdout or "") + (install.stderr or "")
        if install.returncode != 0:
            raise RuntimeError(f"Codex installation failed: {install_output[-3000:]}")

        await session.fs.mkdir(RESULT_DIRECTORY)
        await sandbox.write_files(
            [
                {
                    "path": f"{RESULT_DIRECTORY}/prompt.txt",
                    "content": prompt.encode("utf-8"),
                }
            ]
        )
        executor = await session.create_process(
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
                try:
                    await box.stop()
                finally:
                    await box.destroy(delete_orphan_snapshots=True)
        raise


async def ensure_sandbox_running(handle: SandboxHandle) -> None:
    session = handle.sandbox.session
    await session.refresh()
    if session.status != SandboxStatus.RUNNING:
        raise RuntimeError(f"Vercel Sandbox is {session.status.value}")

    await handle.executor.refresh()
    exit_code = handle.executor.returncode
    if exit_code is not None:
        stdout, stderr = await handle.executor.communicate()
        output = (stdout or "") + (stderr or "")
        detail = output.strip()[-3000:] or "no executor output"
        raise RuntimeError(f"Codex executor exited with code {exit_code}: {detail}")
