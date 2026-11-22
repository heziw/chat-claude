"""Client for summarization-service (:8003). See shared/CONTRACTS.md.

Used by the "summarize" query-routing path (app/main.py:_handle_summarize).
POST /summarize is async (202 + task_id); this module also does the polling
over GET /summarize/{task_id} so app/main.py's handler can stay a simple
"get the final summary text, or an error" caller.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class SummarizationError(Exception):
    """Raised when summarization-service is unreachable or returns an
    error status. Callers turn this into a friendly SSE error message
    rather than letting it crash the request."""


def start_summarize(file_id: str, thinking_level: str = "standard", question: str | None = None) -> str:
    """POST /summarize -> task_id. ``thinking_level`` is forwarded as-is --
    summarization-service maps it to a reasoning_effort hint on its own
    real-mode LLM calls (see CONTRACTS.md "thinking_level"). ``question`` is
    the user's actual chat message, passed through so a focused ask (e.g.
    "summarize the risk section") centers the summary instead of always
    producing a generic whole-document one."""
    url = f"{settings.SUMMARIZATION_SERVICE_URL.rstrip('/')}/summarize"
    try:
        with httpx.Client(timeout=settings.HTTP_TIMEOUT_SECONDS) as client:
            resp = client.post(
                url, json={"file_id": file_id, "thinking_level": thinking_level, "question": question}
            )
    except httpx.RequestError as exc:
        raise SummarizationError(f"summarization-service unreachable at {url}: {exc}") from exc

    if resp.status_code >= 400:
        raise SummarizationError(
            f"summarization-service returned {resp.status_code} for file_id={file_id}: {resp.text}"
        )
    return resp.json()["task_id"]


def poll_summary(task_id: str) -> dict[str, Any]:
    """Poll GET /summarize/{task_id} until status is ready/failed, or give
    up after settings.SUMMARIZE_POLL_TIMEOUT_SECONDS. On timeout, returns
    whatever the last poll said (typically still "processing") -- the
    caller treats a non-ready/failed status as "still working, try again
    later" rather than a hard error.
    """
    url = f"{settings.SUMMARIZATION_SERVICE_URL.rstrip('/')}/summarize/{task_id}"
    deadline = time.monotonic() + settings.SUMMARIZE_POLL_TIMEOUT_SECONDS
    last: dict[str, Any] = {"status": "pending"}
    try:
        with httpx.Client(timeout=settings.HTTP_TIMEOUT_SECONDS) as client:
            while True:
                resp = client.get(url)
                if resp.status_code >= 400:
                    raise SummarizationError(
                        f"summarization-service returned {resp.status_code} for task_id={task_id}: {resp.text}"
                    )
                last = resp.json()
                if last.get("status") in ("ready", "failed"):
                    return last
                if time.monotonic() >= deadline:
                    return last
                time.sleep(settings.SUMMARIZE_POLL_INTERVAL_SECONDS)
    except httpx.RequestError as exc:
        raise SummarizationError(f"summarization-service unreachable at {url}: {exc}") from exc
