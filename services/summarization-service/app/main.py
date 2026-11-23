"""summarization-service FastAPI app.

Endpoints (see shared/CONTRACTS.md):
    POST /summarize            -> {task_id}            (202)
    GET  /summarize/{task_id}  -> {status, summary?, judge?}

`question` (optional) is the user's actual chat message that routed here --
when it names a specific focus rather than a bare "summarize this", the map
stage centers on it. `thinking_level=="deep"` additionally runs a judge+
one-shot-replan pass (see app/summarizer.py and app/tasks.py); its verdict
is surfaced as `judge` for transparency/debugging, same as
structured-data-agent's `judge` field -- existing consumers (retrieval-
orchestrator) only read `status`/`summary`, so this is purely additive.
"""
from __future__ import annotations

import logging

from celery.result import AsyncResult
from fastapi import FastAPI
from pydantic import BaseModel

from app.tasks import celery_app, summarize_file_task

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="summarization-service", version="0.1.0")

# Celery task states -> the status vocabulary this API exposes
# (pending | processing | ready | failed), per CONTRACTS.md.
_STATE_TO_STATUS = {
    "PENDING": "pending",
    "RECEIVED": "pending",
    "STARTED": "processing",
    "RETRY": "processing",
    "SUCCESS": "ready",
    "FAILURE": "failed",
}


class SummarizeRequest(BaseModel):
    file_id: str
    thinking_level: str = "standard"  # "fast"|"standard"|"deep" -- see CONTRACTS.md
    question: str | None = None  # the user's actual chat message, for focus -- see module docstring
    # Conversation-level custom instruction, forwarded from
    # retrieval-orchestrator (see CONTRACTS.md "system_prompt") -- folded
    # into the map stage's instruction alongside the question's focus.
    system_prompt: str | None = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/summarize", status_code=202)
def summarize(req: SummarizeRequest):
    """Kick off the async hierarchical map-reduce Celery task and return
    immediately with {task_id}. Validation of file_id (exists? indexed?
    has chunks?) happens inside the task itself; failures surface via
    GET /summarize/{task_id} with status="failed".
    """
    task = summarize_file_task.delay(
        file_id=req.file_id,
        thinking_level=req.thinking_level,
        question=req.question,
        system_prompt=req.system_prompt,
    )
    return {"task_id": task.id}


@app.get("/summarize/{task_id}")
def summarize_status(task_id: str):
    result = AsyncResult(task_id, app=celery_app)
    status = _STATE_TO_STATUS.get(result.state, "processing")

    response: dict = {"status": status}

    if status == "ready":
        payload = result.result or {}
        response["summary"] = payload.get("summary")
        if "judge" in payload:
            response["judge"] = payload["judge"]
    elif status == "failed":
        response["summary"] = None
        response["error"] = str(result.result) if result.result is not None else "unknown error"

    return response
