from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from anyio import create_task_group
from openai import AsyncOpenAI

import sandbox_agent.runner as runner
from sandbox_agent.config import Settings
from sandbox_agent.models import InspectionRequest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        openai_api_key="application-key",
        openai_executor_api_key="executor-key",
        agents_api_url="https://api.openai.com/v1",
        agent_model="gpt-5.6-sol",
        agent_reasoning_effort="none",
        codex_package="@openai/codex@alpha",
        sandbox_timeout_ms=60_000,
        artifact_max_bytes=10_000,
        sandbox_allowed_domains=("api.openai.com",),
        cleanup_timeout_seconds=1,
    )


def event(kind: str, **fields: Any) -> dict[str, Any]:
    return {
        "type": kind,
        "event_id": f"evt_{kind}_{len(fields)}",
        "session_id": "session_test",
        **fields,
    }


def turn(kind: str, subagent_id: str | None = None) -> dict[str, Any]:
    status = "queued" if kind == "created" else kind
    return event(
        f"agent.session.turn.{kind}",
        turn_id="turn_test",
        turn={
            "id": "turn_test",
            "agent_id": "agent_test",
            "session_id": "session_test",
            "created_at": 1,
            "object": "agent.session.turn",
            "status": status,
            "subagent_id": subagent_id,
            "error": {"code": "server_error", "message": "Turn failed"}
            if kind == "failed"
            else None,
        },
    )


def text_event(kind: str, value: str) -> dict[str, Any]:
    return event(
        f"agent.session.turn.output_text.{kind}",
        item_id="item_test",
        output_index=0,
        content_index=0,
        turn_id="turn_test",
        **{"delta" if kind == "delta" else "text": value},
    )


class EventBody(httpx2.AsyncByteStream):
    def __init__(self, harness: Harness):
        self.harness = harness

    async def __aiter__(self):
        # The SDK must open the stream before submitting input.
        await self.harness.submitted.wait()
        for item in self.harness.events:
            yield f"data: {json.dumps(item)}\n\n".encode()
        if self.harness.hang:
            await asyncio.Event().wait()

    async def aclose(self):
        self.harness.order.append("stream_closed")


class Harness:
    def __init__(self, monkeypatch, settings, events, *, hang=False):
        self.events = events
        self.hang = hang
        self.order: list[str] = []
        self.requests: list[tuple[str, str, Any]] = []
        self.submitted = asyncio.Event()
        self.files: dict[str, bytes] = {}
        self.cleanup_failures: set[str] = set()
        self.health_failure = False
        self.session = {
            "id": "session_test",
            "object": "agent.session",
            "created_at": 1,
            "last_active_at": 1,
            "metadata": {},
            "status": "idle",
            "required_actions": [],
            "vault_ids": [],
            "agent": {
                "id": "agent_test",
                "model": "gpt-5.6-sol",
                "multi_agent": {"enabled": False, "max_concurrent_subagents": 0},
                "reasoning": {"effort": "none"},
                "text": {"verbosity": "low"},
                "tools": [],
                "service_tier": "auto",
            },
            "environment": {
                "type": "self_hosted",
                "id": "environment_test",
                "remote_url": "https://api.openai.com/v1/agents/api?route=test",
                "capability_directories": [],
                "workspace_directory": "/vercel/sandbox",
            },
        }
        self.http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))
        self.client = AsyncOpenAI(
            api_key=settings.openai_api_key, http_client=self.http, max_retries=0
        )

        def make_client(**kwargs):
            assert kwargs["base_url"] == "https://api.openai.com/v1"
            return self.client

        async def start(**kwargs):
            self.order.append("sandbox_start")
            assert kwargs["environment_id"] == "environment_test"
            assert kwargs["remote_url"] == self.session["environment"]["remote_url"]
            assert kwargs["executor_api_key"] == "executor-key"
            return SimpleNamespace(sandbox=self, executor=object())

        async def health(_handle):
            if self.health_failure:
                raise RuntimeError("Executor exited")

        monkeypatch.setattr(runner, "AsyncOpenAI", make_client)
        monkeypatch.setattr(runner, "start_sandbox", start)
        monkeypatch.setattr(runner, "ensure_sandbox_running", health)

    @property
    def sandbox_id(self):
        return "sandbox_test"

    async def handle(self, request):
        assert request.headers["OpenAI-Beta"] == "agents=v1"
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.requests.append((request.method, path, body))
        if request.method == "POST" and path == "/v1/agents/sessions":
            self.order.append("session_create")
            assert body["environment"]["type"] == "self_hosted"
            assert body["agent"]["model"] == "gpt-5.6-sol"
            assert "agent_id" not in body
            return httpx2.Response(200, json=self.session)
        if path.endswith("/events"):
            if request.method == "GET":
                self.order.append("subscribe")
                return httpx2.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=EventBody(self)
                )
            kind = body["events"][0]["type"]
            if kind == "agent.session.input.cancel":
                self.order.append("cancel")
            else:
                self.order.append("input")
                assert "subscribe" in self.order
                assert kind == "agent.session.input.message"
                self.submitted.set()
            return httpx2.Response(200, json={})
        if request.method == "DELETE":
            self.order.append("session_delete")
            if "session" in self.cleanup_failures:
                return httpx2.Response(500, json={"error": {"message": "executor-key"}})
            return httpx2.Response(200, json={"id": "session_test", "deleted": True})
        assert request.method == "GET" and path == "/v1/agents/sessions/session_test"
        return httpx2.Response(200, json=self.session)

    async def write_files(self, files):
        self.order.append("artifacts")
        self.files.update({f["path"]: f["content"] for f in files})

    async def read_file(self, path):
        return self.files.get(path)

    async def stop(self, *, blocking):
        assert blocking
        self.order.append("sandbox_stop")
        if "sandbox" in self.cleanup_failures:
            raise RuntimeError("application-key executor-key")


REQUEST = InspectionRequest(
    repo_url="https://github.com/example/project",
    ref="main",
    question="What framework does this use?",
)


async def run(settings):
    frames = [frame async for frame in runner.stream_inspection(REQUEST, settings)]
    return [(frame.splitlines()[0][7:], json.loads(frame.splitlines()[1][6:])) for frame in frames]


@pytest.mark.anyio
async def test_beta_sdk_end_to_end_contract(monkeypatch, settings):
    events = [
        event("agent.session.idle", session={"status": "idle"}),
        turn("created"),
        text_event("delta", "Part"),
        text_event("done", "Full answer"),
        turn("completed"),
        event("agent.session.idle", session={"status": "idle"}, turn_id="turn_test"),
    ]
    harness = Harness(monkeypatch, settings, events)
    frames = await run(settings)
    result = next(data for kind, data in frames if kind == "result")
    assert result["ok"] is True
    assert result["answer"] == "Full answer"
    assert next(data for kind, data in frames if kind == "answer_delta")["delta"] == "Part"
    assert len(result["artifacts"]) == 3
    assert harness.order.index("subscribe") < harness.order.index("input")
    assert harness.order.index("artifacts") < harness.order.index("sandbox_stop")
    assert harness.order.index("session_delete") < harness.order.index("sandbox_stop")
    assert harness.http.is_closed
    assert "cancel" not in harness.order


@pytest.mark.anyio
async def test_text_done_without_deltas_is_preserved(monkeypatch, settings):
    harness = Harness(
        monkeypatch,
        settings,
        [
            turn("created"),
            text_event("done", "Answer"),
            turn("completed"),
            event("agent.session.idle", session={"status": "idle"}),
        ],
    )
    frames = await run(settings)
    assert next(data for kind, data in frames if kind == "result")["answer"] == "Answer"
    assert harness.http.is_closed


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        turn("failed"),
        turn("cancelled"),
        event(
            "agent.session.environment.failed",
            environment={
                "id": "environment_test",
                "type": "self_hosted",
                "status": "failed",
                "error": {
                    "type": "error",
                    "code": "connection_failed",
                    "message": "Connection failed",
                },
            },
        ),
        event("agent.session.failed", session={"error": "Session failed"}),
        event(
            "error",
            error={"code": "server_error", "message": "Stream failed", "type": "server_error"},
        ),
    ],
)
async def test_failures_are_reported_and_recorded(monkeypatch, settings, failure):
    harness = Harness(monkeypatch, settings, [turn("created"), failure])
    frames = await run(settings)
    assert next(data for kind, data in frames if kind == "result")["ok"] is False
    assert any(kind == "error" for kind, _ in frames)
    recorded = [
        json.loads(line)["event"]["type"]
        for line in harness.files["/tmp/sandbox-agent/events.jsonl"].decode().splitlines()
    ]
    # The SDK raises generic SSE errors before yielding them to the application.
    assert ("inspection.error" if failure["type"] == "error" else failure["type"]) in recorded
    assert "cancel" in harness.order
    assert harness.http.is_closed


@pytest.mark.anyio
@pytest.mark.parametrize(
    "events",
    [
        [],
        [event("agent.session.idle", session={"status": "idle"})],
        [turn("created"), turn("completed", "subagent_test")],
    ],
)
async def test_premature_stream_or_subagent_completion_is_not_success(
    monkeypatch, settings, events
):
    harness = Harness(monkeypatch, settings, events)
    frames = await run(settings)
    assert next(data for kind, data in frames if kind == "result")["ok"] is False
    assert "cancel" in harness.order
    assert harness.http.is_closed


@pytest.mark.anyio
@pytest.mark.parametrize("scope", ["asyncio", "anyio"])
async def test_disconnect_cancels_collects_and_cleans_up(monkeypatch, settings, scope):
    harness = Harness(monkeypatch, settings, [turn("created")], hang=True)
    if scope == "asyncio":
        task = asyncio.create_task(run(settings))
        await harness.submitted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        async with create_task_group() as tasks:
            tasks.start_soon(run, settings)
            await harness.submitted.wait()
            tasks.cancel_scope.cancel()
    assert harness.order.index("cancel") < harness.order.index("artifacts")
    assert harness.order.index("artifacts") < harness.order.index("sandbox_stop")
    assert harness.order.index("session_delete") < harness.order.index("sandbox_stop")
    assert "stream_closed" in harness.order
    assert harness.http.is_closed


@pytest.mark.anyio
@pytest.mark.parametrize(
    "limit", ["stream_execution_timeout_seconds", "stream_no_progress_timeout_seconds"]
)
async def test_timeout_cancels_and_cleans_up(monkeypatch, settings, limit):
    settings = replace(settings, **{limit: 0.03}, executor_healthcheck_interval_seconds=0.01)
    harness = Harness(monkeypatch, settings, [turn("created")], hang=True)
    frames = await run(settings)
    assert next(data for kind, data in frames if kind == "result")["ok"] is False
    assert "cancel" in harness.order
    assert harness.http.is_closed


@pytest.mark.anyio
async def test_executor_failure_cancels_and_cleans_up(monkeypatch, settings):
    harness = Harness(monkeypatch, settings, [turn("created")])
    harness.health_failure = True
    frames = await run(settings)
    assert any(kind == "error" and "Executor exited" in data["message"] for kind, data in frames)
    assert "cancel" in harness.order
    assert harness.http.is_closed


@pytest.mark.anyio
async def test_cleanup_attempts_are_independent_and_errors_redacted(monkeypatch, settings):
    harness = Harness(
        monkeypatch,
        settings,
        [
            turn("created"),
            turn("completed"),
            event("agent.session.idle", session={"status": "idle"}),
        ],
    )
    harness.cleanup_failures = {"sandbox", "session"}
    frames = await run(settings)
    result = next(data for kind, data in frames if kind == "result")
    assert result["ok"] is True
    assert len(result["warnings"]) == 2
    assert "application-key" not in json.dumps(frames)
    assert "executor-key" not in json.dumps(frames)
    assert "session_delete" in harness.order
    assert harness.http.is_closed


def test_activity_summarizes_beta_commands():
    assert runner._activity(
        {
            "type": "agent.session.turn.item.added",
            "item": {"type": "command_execution", "command": "rg --files"},
        }
    ) == {"type": "agent.session.turn.item.added", "message": "Running: rg --files"}


def test_error_redacts_both_openai_keys():
    safe = runner._safe_error(
        RuntimeError("application-key and executor-key"), "application-key", "executor-key"
    )
    assert safe.count("[REDACTED]") == 2


@pytest.mark.anyio
async def test_executor_health_is_checked_during_subscription(monkeypatch, settings):
    settings = replace(settings, executor_healthcheck_interval_seconds=0.01)
    harness = Harness(monkeypatch, settings, [])
    original = harness.handle

    async def handle(request):
        if request.method == "GET" and request.url.path.endswith("/events"):
            harness.health_failure = True
            await asyncio.Event().wait()
        return await original(request)

    harness.http._transport = httpx2.MockTransport(handle)
    frames = await run(settings)
    assert any(kind == "error" and "Executor exited" in data["message"] for kind, data in frames)
    assert "input" not in harness.order
    assert harness.http.is_closed


@pytest.mark.anyio
async def test_session_delete_retries_conflict_before_stopping_sandbox(monkeypatch, settings):
    harness = Harness(
        monkeypatch,
        settings,
        [
            turn("created"),
            turn("completed"),
            event("agent.session.idle", session={"status": "idle"}),
        ],
    )
    original = harness.handle
    conflicts = []

    async def handle(request):
        if request.method == "DELETE" and not conflicts:
            assert "sandbox_stop" not in harness.order
            conflicts.append(True)
            return httpx2.Response(409, json={"error": {"message": "Turn settling"}})
        return await original(request)

    harness.http._transport = httpx2.MockTransport(handle)
    frames = await run(settings)
    result = next(data for kind, data in frames if kind == "result")
    assert result["ok"] is True
    assert result["warnings"] == []
    assert conflicts == [True]
    assert harness.order.index("session_delete") < harness.order.index("sandbox_stop")
