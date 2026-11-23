"""structured-data-agent (:8002) -- FastAPI app.

See /home/hezi/claude/chat/shared/CONTRACTS.md for the authoritative API
contract. This service implements:

- `GET /schema/{file_id}`: loads the parquet side-store DataFrame(s) for a
  file (one or more sheets) and returns their columns/dtypes, row counts,
  and a 5-row sample.
- `POST /structured-query`: NL -> a short pandas *plan* (1-4 steps,
  app.llm.generate_plan), executed step-by-step in one persistent sandbox
  namespace (app.sandbox.SandboxSession) so later steps can use what
  earlier steps computed (e.g. a derived ratio column, then "pick the max
  row"). Each step gets one retry if it raises -- the failing step alone is
  regenerated with its error fed back (app.llm.regenerate_step), not the
  whole plan -- before giving up with a 422. A single aggregation question
  is just the common case of a 1-step plan; there's no separate code path
  for "simple" questions to keep in sync with this one.

  `thinking_level` (`"fast"|"standard"|"deep"`, default `"standard"`, see
  CONTRACTS.md) is forwarded to every real-mode LLM call as a
  `reasoning_effort` hint. `"deep"` additionally runs `app.llm.judge_result`
  on the finished answer -- a *semantic* check an exception can never catch
  (e.g. a zero-denominator ratio producing a silent NaN/Inf): if the judge
  is unsatisfied, exactly one full replan is attempted (the judge's reason
  fed back into `generate_plan`'s `extra_context`), and the original result
  is kept if that replan doesn't produce a better one. Bounded to one
  judge call + one replan -- not an open-ended loop.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd
from fastapi import HTTPException
from fastapi_offline import FastAPIOffline
from pydantic import BaseModel

from app import db
from app.config import settings
from app.data_store import (
    StructuredDataNotFoundError,
    describe_schema_text,
    load_dataframes,
    schema_for_dataframes,
)
from app.llm import generate_plan, judge_result, regenerate_step
from app.sandbox import SandboxSession, SandboxTimeoutError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPIOffline(title="structured-data-agent", version="0.1.0")


class StructuredQueryRequest(BaseModel):
    file_id: str
    question: str
    thinking_level: str = "standard"
    # Conversation-level custom instruction, forwarded from
    # retrieval-orchestrator (see CONTRACTS.md "system_prompt") -- folded
    # into the plan-generation prompt as a general instruction alongside
    # the schema/question.
    system_prompt: str | None = None


def _load_dfs_for_file(file_id: str) -> dict[str, pd.DataFrame]:
    fm = db.get_file_metadata(file_id)
    if fm is None:
        raise HTTPException(status_code=404, detail=f"file_id {file_id} not found in file_metadata")
    try:
        return load_dataframes(
            file_id=file_id,
            structured_store_ref=fm.get("structured_store_ref"),
            conversation_id=fm["conversation_id"],
        )
    except StructuredDataNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _json_safe(value: Any) -> Any:
    """Recursively convert pandas/numpy objects into plain JSON-serializable
    Python types (DataFrame/Series -> list/dict, numpy scalars -> python
    scalars, numpy arrays -> lists)."""
    if isinstance(value, pd.DataFrame):
        return _json_safe(value.to_dict(orient="records"))
    if isinstance(value, pd.Series):
        return _json_safe(value.to_dict())
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return _json_safe(value.tolist())
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _serialize_result(result: Any) -> tuple[Any | None, Any]:
    """Return (result_table, display_value). result_table is populated only
    when `result` is a DataFrame/Series (i.e. genuinely tabular); otherwise
    it's None and display_value holds the JSON-safe scalar/structure."""
    is_table = isinstance(result, (pd.DataFrame, pd.Series))
    safe = _json_safe(result)
    return (safe, safe) if is_table else (None, safe)


def _build_answer_text(question: str, result_table: Any, display_value: Any) -> str:
    if result_table is not None:
        n = len(result_table) if hasattr(result_table, "__len__") else "?"
        return f'התוצאה לשאלה "{question}" היא טבלה עם {n} רשומות (ראו result_table).'
    return f'התוצאה לשאלה "{question}" היא: {display_value}'


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/schema/{file_id}")
def get_schema(file_id: str) -> dict:
    dfs = _load_dfs_for_file(file_id)
    return {"file_id": file_id, **schema_for_dataframes(dfs)}


def _run_step_or_retry(
    session: SandboxSession,
    step: dict[str, str],
    step_no: int,
    total_steps: int,
    question: str,
    schema_description: str,
    prior_steps: list[dict[str, str]],
    file_id: str,
    thinking_level: str,
) -> dict[str, str]:
    """Run one plan step in ``session``; on failure, regenerate just that
    step (error fed back) and run the correction once. Returns the step
    that actually succeeded (original or corrected), for the transparency
    record in the response. Raises HTTPException(422) if both attempts
    fail or time out.
    """
    try:
        session.run_step(step["code"], timeout_seconds=settings.SANDBOX_TIMEOUT_SECONDS)
        return step
    except SandboxTimeoutError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"שלב {step_no}/{total_steps} ('{step['description']}') לא הסתיים בזמן: {exc}",
        ) from exc
    except Exception as first_exc:  # noqa: BLE001 -- any exec() failure triggers the retry path
        logger.warning(
            "Step %d/%d failed for file_id=%s question=%r: %s",
            step_no,
            total_steps,
            file_id,
            question,
            first_exc,
        )
        retry_step = regenerate_step(
            question, schema_description, prior_steps, step, str(first_exc), thinking_level
        )
        try:
            session.run_step(retry_step["code"], timeout_seconds=settings.SANDBOX_TIMEOUT_SECONDS)
            return retry_step
        except SandboxTimeoutError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"שלב {step_no}/{total_steps} (לאחר תיקון) לא הסתיים בזמן: {exc}",
            ) from exc
        except Exception as second_exc:  # noqa: BLE001
            logger.warning(
                "Step %d/%d also failed after retry for file_id=%s question=%r: %s",
                step_no,
                total_steps,
                file_id,
                question,
                second_exc,
            )
            raise HTTPException(
                status_code=422,
                detail=(
                    f"שלב {step_no}/{total_steps} ('{step['description']}') נכשל גם לאחר ניסיון תיקון. "
                    f"שגיאה ראשונה: {first_exc}. שגיאה בניסיון השני: {second_exc}."
                ),
            ) from second_exc


def _execute_plan(
    plan: list[dict[str, str]],
    dfs: dict[str, pd.DataFrame],
    question: str,
    schema_description: str,
    file_id: str,
    thinking_level: str,
) -> tuple[SandboxSession, list[dict[str, str]]]:
    """Run every step of ``plan`` (with per-step retry) against a fresh
    ``SandboxSession``, then handle the "no step ever set `result`" edge
    case the same way as a failed last step. Shared by the initial attempt
    and the judge-triggered replan below. Raises HTTPException(422) if any
    step (or the no-`result` fixup) fails twice.
    """
    session = SandboxSession(dfs)
    executed_steps: list[dict[str, str]] = []

    for i, step in enumerate(plan, start=1):
        executed = _run_step_or_retry(
            session, step, i, len(plan), question, schema_description, executed_steps, file_id, thinking_level
        )
        executed_steps.append(executed)

    if session.result is None:
        # Every step ran without raising, but none of them set `result` --
        # almost certainly the last step's doing. Treat it the same as a
        # failure of that step (one retry, then give up) rather than
        # returning a silently empty answer.
        last_step = executed_steps[-1]
        fixed = regenerate_step(
            question,
            schema_description,
            executed_steps[:-1],
            last_step,
            "התוכנית הסתיימה בלי להגדיר את המשתנה result",
            thinking_level,
        )
        try:
            session.run_step(fixed["code"], timeout_seconds=settings.SANDBOX_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=422,
                detail=f"התוכנית הסתיימה בלי להגדיר 'result', וגם תיקון השלב האחרון נכשל: {exc}",
            ) from exc
        executed_steps[-1] = fixed

    return session, executed_steps


@app.post("/structured-query")
def structured_query(req: StructuredQueryRequest) -> dict:
    dfs = _load_dfs_for_file(req.file_id)
    schema_description = describe_schema_text(dfs)

    plan = generate_plan(req.question, schema_description, req.thinking_level, system_prompt=req.system_prompt)
    session, executed_steps = _execute_plan(
        plan, dfs, req.question, schema_description, req.file_id, req.thinking_level
    )
    result_table, display_value = _serialize_result(session.result)

    judge: dict[str, object] | None = None
    if req.thinking_level == "deep":
        judge = judge_result(req.question, schema_description, executed_steps, display_value, req.thinking_level)
        if not judge.get("satisfactory", True):
            logger.info(
                "Judge flagged result as unsatisfactory for file_id=%s question=%r: %s",
                req.file_id,
                req.question,
                judge.get("reason"),
            )
            replan = generate_plan(
                req.question,
                schema_description,
                req.thinking_level,
                extra_context=str(judge.get("reason") or ""),
                system_prompt=req.system_prompt,
            )
            try:
                retry_session, retry_steps = _execute_plan(
                    replan, dfs, req.question, schema_description, req.file_id, req.thinking_level
                )
            except HTTPException:
                # The bounded replan attempt itself failed twice on some
                # step -- keep the original (already-computed) result
                # rather than losing a working answer to a failed
                # self-correction attempt. One judge call, one replan
                # attempt, no further looping either way.
                logger.warning(
                    "Judge-triggered replan failed for file_id=%s question=%r; keeping original result",
                    req.file_id,
                    req.question,
                )
                judge["corrected"] = False
            else:
                if retry_session.result is not None:
                    session, executed_steps = retry_session, retry_steps
                    result_table, display_value = _serialize_result(session.result)
                    judge["corrected"] = True
                else:
                    judge["corrected"] = False
        else:
            judge["corrected"] = None

    answer = _build_answer_text(req.question, result_table, display_value)

    response: dict[str, object] = {
        "answer": answer,
        "code_executed": "\n\n".join(f"# {s['description']}\n{s['code']}" for s in executed_steps),
        "result_table": result_table,
        "steps": executed_steps,
    }
    if judge is not None:
        response["judge"] = judge
    return response
