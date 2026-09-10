from __future__ import annotations

import json

from sandbox_agent.sse import encode_sse


def test_sse_frame_contains_named_json_event() -> None:
    frame = encode_sse("status", {"message": "Sandbox ready", "ok": True})

    lines = frame.strip().splitlines()
    assert lines[0] == "event: status"
    assert json.loads(lines[1].removeprefix("data: ")) == {
        "message": "Sandbox ready",
        "ok": True,
    }
