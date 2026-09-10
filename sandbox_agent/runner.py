from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, aclosing, suppress
from datetime import UTC, datetime
from time import monotonic
from typing import Any, AsyncIterator, Coroutine
from uuid import uuid4

from anyio import CancelScope
from openai import AsyncOpenAI, ConflictError
from openai.types.beta import AgentSessionEvent

from sandbox_agent.artifacts import build_fallback_artifacts, collect_artifacts
from sandbox_agent.config import Settings
from sandbox_agent.models import InspectionRequest
from sandbox_agent.prompt import build_agent_instructions, build_inspection_prompt
from sandbox_agent.sse import encode_sse
from sandbox_agent.vercel_sandbox import SandboxHandle, ensure_sandbox_running, start_sandbox


async def stream_inspection(
    request: InspectionRequest,
    settings: Settings,
) -> AsyncIterator[str]:
    resources: dict[str, Any] = {
        "client": None,
        "session": None,
        "handle": None,
        "events": [],
        "text_parts": {},
        "cancel_pending": False,
        "artifacts_collected": False,
    }
    try:
        async with aclosing(_run_inspection(request, settings, resources)) as frames:
            async for frame in frames:
                yield frame
    finally:
        # Starlette cancels its response scope on disconnect. Finish resource work
        # inside a shield so that cancellation cannot skip the following awaits.
        with CancelScope(shield=True):
            with suppress(Exception):
                await _cancel_session(resources, settings.cleanup_timeout_seconds)
            if resources["handle"] is not None and not resources["artifacts_collected"]:
                with suppress(Exception):
                    async with asyncio.timeout(settings.cleanup_timeout_seconds):
                        await collect_artifacts(
                            sandbox=resources["handle"].sandbox,
                            request=request,
                            events=resources["events"],
                            streamed_answer="".join(resources["text_parts"].values()),
                            run_error="Inspection interrupted by the client.",
                            max_bytes=settings.artifact_max_bytes,
                        )
            await _cleanup(resources, settings.cleanup_timeout_seconds, settings)


async def _run_inspection(
    request: InspectionRequest,
    settings: Settings,
    resources: dict[str, Any],
) -> AsyncIterator[str]:
    run_id = f"run_{uuid4().hex[:12]}"
    started_at = monotonic()
    captured_events: list[dict[str, Any]] = resources["events"]
    text_parts: dict[tuple[str, int, int], str] = resources["text_parts"]
    client: AsyncOpenAI | None = None
    session: Any = None
    handle: SandboxHandle | None = None
    session_id: str | None = None
    sandbox_id: str | None = None
    final_status = "failed"
    turn_completed = False
    run_error: str | None = None
    prompt = build_inspection_prompt(
        repo_url=request.repo_url,
        ref=request.ref,
        question=request.question,
    )

    yield encode_sse(
        "status",
        {"run_id": run_id, "stage": "session", "message": "Creating agent session"},
    )

    try:
        client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.agents_api_url,
            max_retries=0,
            timeout=settings.session_create_timeout_seconds,
        )
        resources["client"] = client
        async with asyncio.timeout(settings.session_create_timeout_seconds):
            session = await client.beta.agents.sessions.create(
                agent={
                    "model": settings.agent_model,
                    "instructions": build_agent_instructions(),
                    "reasoning": {"effort": settings.agent_reasoning_effort},
                    "text": {"verbosity": "low"},
                },
                environment={
                    "type": "self_hosted",
                    "workspace_directory": "/vercel/sandbox",
                },
            )
        resources["session"] = session
        resources["cancel_pending"] = True
        session_id = session.id
        environment = session.environment
        if environment.type != "self_hosted":
            raise RuntimeError(f"Expected self-hosted environment, got {environment.type}")

        yield encode_sse(
            "status",
            {
                "run_id": run_id,
                "stage": "sandbox",
                "message": "Cloning the repository into Vercel Sandbox",
                "session_id": session_id,
            },
        )

        async with asyncio.timeout(settings.sandbox_startup_timeout_seconds):
            handle = await start_sandbox(
                executor_api_key=settings.openai_executor_api_key,
                environment_id=environment.id,
                remote_url=environment.remote_url,
                repo_url=request.repo_url,
                ref=request.ref,
                prompt=prompt,
                settings=settings,
            )
        resources["handle"] = handle
        sandbox_id = handle.sandbox.sandbox_id

        yield encode_sse(
            "status",
            {
                "run_id": run_id,
                "stage": "inspect",
                "message": "Codex is inspecting the repository",
                "sandbox_id": sandbox_id,
            },
        )

        async with aclosing(
            _stream_session_events(client, session_id, handle, prompt, settings)
        ) as events:
            async for event in events:
                payload = _event_payload(event)
                captured_events.append(
                    {"received_at": datetime.now(UTC).isoformat(), "event": payload}
                )
                _raise_for_failed_event(event)
                if event.type == "agent.session.turn.completed" and event.turn.subagent_id is None:
                    turn_completed = True
                    resources["cancel_pending"] = False

                activity = _activity(payload)
                if activity is not None:
                    yield encode_sse("activity", {"run_id": run_id, **activity})

                if event.type == "agent.session.turn.output_text.delta":
                    key = (event.item_id, event.output_index, event.content_index)
                    text_parts[key] = text_parts.get(key, "") + event.delta
                    yield encode_sse(
                        "answer_delta",
                        {"run_id": run_id, "delta": event.delta},
                    )
                elif event.type == "agent.session.turn.output_text.done":
                    key = (event.item_id, event.output_index, event.content_index)
                    text_parts[key] = event.text

        async with asyncio.timeout(settings.session_create_timeout_seconds):
            final_session = await client.beta.agents.sessions.retrieve(session_id)
        final_status = str(final_session.status)
        if not turn_completed:
            raise RuntimeError("Agent stream ended without a completed inspection turn")
    except asyncio.CancelledError:
        raise
    except Exception as error:
        run_error = _safe_error(
            error,
            settings.openai_api_key,
            settings.openai_executor_api_key,
        )
        captured_events.append(
            {
                "received_at": datetime.now(UTC).isoformat(),
                "event": {"type": "inspection.error", "message": run_error},
            }
        )
        with suppress(Exception):
            await _cancel_session(resources, settings.cleanup_timeout_seconds)
        yield encode_sse(
            "error",
            {"run_id": run_id, "stage": "failed", "message": run_error},
        )

    yield encode_sse(
        "status",
        {"run_id": run_id, "stage": "result", "message": "Preparing the answer"},
    )

    streamed_answer = "".join(text_parts.values())
    if handle is not None:
        try:
            async with asyncio.timeout(settings.cleanup_timeout_seconds):
                answer, evidence, artifacts = await collect_artifacts(
                    sandbox=handle.sandbox,
                    request=request,
                    events=captured_events,
                    streamed_answer=streamed_answer,
                    run_error=run_error,
                    max_bytes=settings.artifact_max_bytes,
                )
        except Exception as error:
            artifact_error = _safe_error(
                error,
                settings.openai_api_key,
                settings.openai_executor_api_key,
            )
            run_error = run_error or artifact_error
            answer, evidence, artifacts = build_fallback_artifacts(
                request=request,
                events=captured_events,
                streamed_answer=streamed_answer,
                run_error=run_error,
                max_bytes=settings.artifact_max_bytes,
            )
    else:
        answer, evidence, artifacts = build_fallback_artifacts(
            request=request,
            events=captured_events,
            streamed_answer=streamed_answer,
            run_error=run_error,
            max_bytes=settings.artifact_max_bytes,
        )

    resources["artifacts_collected"] = True
    with CancelScope(shield=True):
        cleanup_warnings = await _cleanup(resources, settings.cleanup_timeout_seconds, settings)
    if cleanup_warnings:
        cleanup_message = "; ".join(cleanup_warnings)
        yield encode_sse(
            "warning",
            {"run_id": run_id, "stage": "cleanup", "message": cleanup_message},
        )

    ok = run_error is None and turn_completed and final_status == "idle"
    yield encode_sse(
        "result",
        {
            "run_id": run_id,
            "ok": ok,
            "answer": answer,
            "evidence": evidence,
            "artifacts": [artifact.model_dump() for artifact in artifacts],
            "repo_url": request.repo_url,
            "ref": request.ref,
            "model": settings.agent_model,
            "session_id": session_id,
            "sandbox_id": sandbox_id,
            "duration_seconds": round(monotonic() - started_at, 1),
            "warnings": cleanup_warnings,
        },
    )
    yield encode_sse(
        "complete",
        {
            "run_id": run_id,
            "ok": ok,
            "stage": "complete" if ok else "failed",
            "message": (
                "Inspection complete with a cleanup warning"
                if ok and cleanup_warnings
                else "Inspection complete"
                if ok
                else run_error or "Inspection incomplete"
            ),
        },
    )


async def _stream_session_events(
    client: AsyncOpenAI,
    session_id: str,
    handle: SandboxHandle,
    prompt: str,
    settings: Settings,
) -> AsyncIterator[AgentSessionEvent]:
    try:
        async with (
            asyncio.timeout(settings.stream_execution_timeout_seconds),
            AsyncExitStack() as stack,
        ):
            events = await _with_executor_health(
                stack.enter_async_context(
                    client.beta.agents.sessions.stream(
                        session_id,
                        input=prompt,
                        timeout=settings.stream_no_progress_timeout_seconds,
                    )
                ),
                handle,
                settings,
            )
            while True:
                try:
                    event = await _with_executor_health(anext(events), handle, settings)
                except StopAsyncIteration:
                    break
                yield event
    except TimeoutError as error:
        raise RuntimeError(
            "Agent exceeded the "
            f"{settings.stream_execution_timeout_seconds}-second inspection limit"
        ) from error


async def _with_executor_health(
    operation: Coroutine[Any, Any, Any],
    handle: SandboxHandle,
    settings: Settings,
) -> Any:
    # Monitor the subscription handshake too: a rejected executor can otherwise
    # leave the SDK waiting for stream headers before input is ever submitted.
    task = asyncio.create_task(operation)
    started = monotonic()
    try:
        while True:
            done, _ = await asyncio.wait(
                {task},
                timeout=settings.executor_healthcheck_interval_seconds,
            )
            if done:
                value = task.result()
                await ensure_sandbox_running(handle)
                return value
            await ensure_sandbox_running(handle)
            if monotonic() - started >= settings.stream_no_progress_timeout_seconds:
                raise RuntimeError(
                    "Agent made no progress for "
                    f"{settings.stream_no_progress_timeout_seconds} seconds"
                )
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task


def _raise_for_failed_event(event: AgentSessionEvent) -> None:
    if event.type == "agent.session.environment.failed":
        raise RuntimeError(f"Agent environment failed: {event.environment.error}")
    if event.type == "agent.session.turn.failed":
        message = event.turn.error.message if event.turn.error else "unknown error"
        raise RuntimeError(f"Agent turn failed: {message}")
    if event.type == "agent.session.turn.cancelled":
        raise RuntimeError("Agent turn was cancelled")
    if event.type == "agent.session.failed":
        raise RuntimeError(f"Agent session failed: {event.session.error}")
    if event.type == "error":
        raise RuntimeError(f"Agent stream error: {event.error.message}")


def _event_payload(event: Any) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        payload = event.model_dump(mode="json", by_alias=True, exclude_none=True)
        if isinstance(payload, dict):
            return payload
    data = getattr(event, "data", None)
    if isinstance(data, dict):
        return data
    return {"type": getattr(event, "type", type(event).__name__), "value": str(event)}


def _activity(payload: dict[str, Any]) -> dict[str, str] | None:
    event_type = str(payload.get("type", ""))
    labels = {
        "agent.session.turn.created": "Agent turn queued",
        "agent.session.environment.connected": "Executor connected",
        "agent.session.turn.in_progress": "Repository inspection started",
        "agent.session.turn.completed": "Agent turn completed",
        "agent.session.idle": "Session is idle",
    }
    if event_type in labels:
        return {"type": event_type, "message": labels[event_type]}

    item = payload.get("item")
    if event_type == "agent.session.turn.item.added" and isinstance(item, dict):
        if item.get("type") == "command_execution":
            command = " ".join(str(item.get("command", "shell command")).split())
            return {"type": event_type, "message": f"Running: {command[:140]}"}
    if event_type == "agent.session.turn.item.done" and isinstance(item, dict):
        if item.get("type") == "command_execution":
            return {"type": event_type, "message": "Command finished"}
    return None


def _safe_error(error: Exception, *secrets: str) -> str:
    value = f"{type(error).__name__}: {error}"
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value[:4000]


async def _cancel_session(resources: dict[str, Any], timeout_seconds: int) -> None:
    if resources.get("cancel_pending") and resources.get("session") is not None:
        async with asyncio.timeout(timeout_seconds):
            await resources["client"].beta.agents.sessions.events.create(
                resources["session"].id,
                events=[{"type": "agent.session.input.cancel"}],
            )
        resources["cancel_pending"] = False


async def _cleanup(
    resources: dict[str, Any],
    timeout_seconds: int,
    settings: Settings,
) -> list[str]:
    session = resources.get("session")
    handle = resources.get("handle")
    client = resources.get("client")
    errors: list[str] = []

    if session is not None:
        try:
            async with asyncio.timeout(timeout_seconds):
                while True:
                    try:
                        await client.beta.agents.sessions.delete(session.id)
                        break
                    except ConflictError:
                        # Cancellation is asynchronous; allow the turn to settle.
                        await asyncio.sleep(0.25)
        except Exception as error:
            errors.append(
                "Session cleanup failed: "
                + _safe_error(
                    error,
                    settings.openai_api_key,
                    settings.openai_executor_api_key,
                )
            )
        finally:
            resources["session"] = None
    if handle is not None:
        try:
            async with asyncio.timeout(timeout_seconds):
                await handle.sandbox.stop(blocking=True)
        except Exception as error:
            errors.append(
                "Sandbox cleanup failed: "
                + _safe_error(
                    error,
                    settings.openai_api_key,
                    settings.openai_executor_api_key,
                )
            )
        finally:
            resources["handle"] = None
    if client is not None:
        try:
            async with asyncio.timeout(timeout_seconds):
                await client.close()
        except Exception as error:
            errors.append(
                "Client cleanup failed: "
                + _safe_error(
                    error,
                    settings.openai_api_key,
                    settings.openai_executor_api_key,
                )
            )
        finally:
            resources["client"] = None
    return errors
