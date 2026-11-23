"""Client for structured-data-agent (:8002). See shared/CONTRACTS.md.

Used by the "structured" query-routing path (app/main.py:_handle_structured)
when a conversation has exactly one ready xlsx/csv/json file.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class StructuredQueryError(Exception):
    """Raised when structured-data-agent is unreachable or returns an
    error status. Callers turn this into a friendly SSE error message
    rather than letting it crash the request."""


def structured_query(
    file_id: str, question: str, thinking_level: str = "standard", system_prompt: str | None = None
) -> dict[str, Any]:
    """POST /structured-query -> {answer, code_executed, result_table?, steps, judge?}.

    ``thinking_level`` ("fast"|"standard"|"deep") is forwarded as-is --
    structured-data-agent is the service that actually interprets it (higher
    reasoning_effort on its LLM calls, plus a judge+replan pass when
    "deep"). See CONTRACTS.md "thinking_level".

    ``system_prompt`` -- the conversation's custom instruction (see
    CONTRACTS.md "system_prompt"), folded into the plan-generation prompt
    as a general instruction. Omitted from the payload entirely when unset
    rather than sent as null, keeping request logs clean.
    """
    url = f"{settings.STRUCTURED_AGENT_URL.rstrip('/')}/structured-query"
    payload: dict[str, Any] = {"file_id": file_id, "question": question, "thinking_level": thinking_level}
    if system_prompt:
        payload["system_prompt"] = system_prompt
    try:
        with httpx.Client(timeout=settings.HTTP_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json=payload)
    except httpx.RequestError as exc:
        raise StructuredQueryError(f"structured-data-agent unreachable at {url}: {exc}") from exc

    if resp.status_code >= 400:
        raise StructuredQueryError(
            f"structured-data-agent returned {resp.status_code} for file_id={file_id}: {resp.text}"
        )
    return resp.json()
