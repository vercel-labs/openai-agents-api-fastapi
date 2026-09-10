from __future__ import annotations

import json
from typing import Any

import pytest

from sandbox_agent.artifacts import collect_artifacts
from sandbox_agent.models import InspectionRequest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_collects_answer_evidence_and_event_trace() -> None:
    writes: list[list[dict[str, Any]]] = []

    class Sandbox:
        async def write_files(self, files: list[dict[str, Any]]) -> None:
            writes.append(files)

        async def read_file(self, path: str) -> bytes | None:
            if path.endswith("RESULT.md"):
                return b"# Answer\n\nThis repository uses FastAPI.\n"
            if path.endswith("evidence.json"):
                return json.dumps(
                    {
                        "files_read": ["requirements.txt"],
                        "commands_run": ["sed -n 1,80p requirements.txt"],
                        "limitations": [],
                    }
                ).encode()
            return None

    request = InspectionRequest(
        repo_url="https://github.com/example/project",
        question="Which framework is used?",
    )
    answer, evidence, artifacts = await collect_artifacts(
        sandbox=Sandbox(),
        request=request,
        events=[{"event": {"type": "session.idle"}}],
        streamed_answer="",
        run_error=None,
        max_bytes=20_000,
    )

    assert "FastAPI" in answer
    assert evidence["files_read"] == ["requirements.txt"]
    assert [artifact.name for artifact in artifacts] == [
        "RESULT.md",
        "evidence.json",
        "events.jsonl",
    ]
    assert writes[0][0]["path"].endswith("events.jsonl")


@pytest.mark.anyio
async def test_invalid_evidence_is_replaced() -> None:
    class Sandbox:
        async def write_files(self, files: list[dict[str, Any]]) -> None:
            del files

        async def read_file(self, path: str) -> bytes | None:
            if path.endswith("RESULT.md"):
                return b"A useful answer"
            return b"not json"

    request = InspectionRequest(
        repo_url="https://github.com/example/project",
        question="Which framework is used?",
    )
    _, evidence, _ = await collect_artifacts(
        sandbox=Sandbox(),
        request=request,
        events=[],
        streamed_answer="",
        run_error=None,
        max_bytes=20_000,
    )

    assert evidence["files_read"] == []
    assert "valid structured evidence" in evidence["limitations"][0]
