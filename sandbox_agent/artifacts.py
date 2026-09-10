from __future__ import annotations

import json
from typing import Any

from sandbox_agent.models import Artifact, InspectionRequest
from sandbox_agent.prompt import RESULT_DIRECTORY

REQUIRED_ARTIFACTS = ("RESULT.md", "evidence.json", "events.jsonl")
MEDIA_TYPES = {
    "RESULT.md": "text/markdown",
    "evidence.json": "application/json",
    "events.jsonl": "application/x-ndjson",
}


async def collect_artifacts(
    *,
    sandbox: Any,
    request: InspectionRequest,
    events: list[dict[str, Any]],
    streamed_answer: str,
    run_error: str | None,
    max_bytes: int,
) -> tuple[str, dict[str, list[str]], list[Artifact]]:
    events_text = _events_jsonl(events)
    await sandbox.write_files(
        [
            {
                "path": f"{RESULT_DIRECTORY}/events.jsonl",
                "content": events_text.encode("utf-8"),
            }
        ]
    )

    result_raw = await sandbox.read_file(f"{RESULT_DIRECTORY}/RESULT.md")
    evidence_raw = await sandbox.read_file(f"{RESULT_DIRECTORY}/evidence.json")
    answer = _decode(result_raw).strip() or streamed_answer.strip()
    if not answer:
        answer = _fallback_answer(request, run_error)

    evidence = _parse_evidence(evidence_raw)
    values = {
        "RESULT.md": answer + "\n",
        "evidence.json": json.dumps(evidence, indent=2) + "\n",
        "events.jsonl": events_text,
    }
    return answer, evidence, _artifacts(values, max_bytes)


def build_fallback_artifacts(
    *,
    request: InspectionRequest,
    events: list[dict[str, Any]],
    streamed_answer: str,
    run_error: str | None,
    max_bytes: int,
) -> tuple[str, dict[str, list[str]], list[Artifact]]:
    answer = streamed_answer.strip() or _fallback_answer(request, run_error)
    evidence = {"files_read": [], "commands_run": [], "limitations": [run_error or "Run ended early."]}
    values = {
        "RESULT.md": answer + "\n",
        "evidence.json": json.dumps(evidence, indent=2) + "\n",
        "events.jsonl": _events_jsonl(events),
    }
    return answer, evidence, _artifacts(values, max_bytes)


def _parse_evidence(raw: bytes | None) -> dict[str, list[str]]:
    try:
        value = json.loads(_decode(raw))
        if not isinstance(value, dict):
            raise ValueError
        parsed: dict[str, list[str]] = {}
        for key in ("files_read", "commands_run", "limitations"):
            items = value.get(key, [])
            if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
                raise ValueError
            parsed[key] = items[:50]
        return parsed
    except (json.JSONDecodeError, ValueError, TypeError):
        return {
            "files_read": [],
            "commands_run": [],
            "limitations": ["The agent did not produce valid structured evidence."],
        }


def _artifacts(values: dict[str, str], max_bytes: int) -> list[Artifact]:
    artifacts: list[Artifact] = []
    for name in REQUIRED_ARTIFACTS:
        raw = values[name].encode("utf-8")
        truncated = len(raw) > max_bytes
        content = raw[:max_bytes].decode("utf-8", errors="replace")
        if truncated:
            content += "\n\n[Artifact truncated by the application.]\n"
        artifacts.append(
            Artifact(
                name=name,
                media_type=MEDIA_TYPES[name],
                content=content,
                truncated=truncated,
            )
        )
    return artifacts


def _decode(raw: bytes | None) -> str:
    return raw.decode("utf-8", errors="replace") if raw else ""


def _events_jsonl(events: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(event, sort_keys=True, default=str) + "\n" for event in events)


def _fallback_answer(request: InspectionRequest, run_error: str | None) -> str:
    detail = run_error or "The agent ended before producing an answer."
    return f"# Inspection incomplete\n\nQuestion: {request.question}\n\n{detail}"
