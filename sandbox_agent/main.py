from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from sandbox_agent.config import get_settings
from sandbox_agent.models import InspectionRequest
from sandbox_agent.runner import stream_inspection

app = FastAPI(
    title="Sandboxed Repo Agent",
    version="1.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
STATIC_DIRECTORY = Path(__file__).with_name("static")
app.mount("/assets", StaticFiles(directory=STATIC_DIRECTORY), name="assets")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIRECTORY / "index.html")


@app.get("/api")
async def api_root() -> dict[str, str]:
    return {"service": "sandboxed-repo-agent", "status": "ready"}


@app.get("/api/health")
async def health() -> dict[str, str | bool]:
    try:
        settings = get_settings()
    except RuntimeError:
        return {"status": "degraded", "required_configured": False}
    return {
        "status": "ready",
        "required_configured": True,
        "model": settings.agent_model,
        "reasoning_effort": settings.agent_reasoning_effort,
    }


@app.post("/api/inspect")
async def inspect_repository(payload: InspectionRequest) -> StreamingResponse:
    try:
        settings = get_settings()
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    return StreamingResponse(
        stream_inspection(payload, settings),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
