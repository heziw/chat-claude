"""retrieval-orchestrator (:8001) -- FastAPI app.

See /home/hezi/claude/chat/shared/CONTRACTS.md for the authoritative API
contract. This service implements:

- `POST /query`: 5-way query routing (app.retrieval.classify_query) over
  hybrid retrieval + streamed generation, returned as an SSE stream with
  `token`, `citation`, and `done` events -- the same event shapes as
  before, regardless of which route served the request:
    - "general": no files at all in the conversation -> plain
      conversational generation, no retrieval attempted, no citations.
    - "factual" (default once "general" is ruled out): hybrid
      (dense+BM25/RRF/rerank) retrieval (app.retrieval.retrieve) +
      app.generation.generate_stream.
    - "structured": exactly one ready xlsx/csv/json file in the
      conversation -> structured-data-agent's /structured-query.
    - "summarize": summarization-service's /summarize (+ poll).
    - "multi": comparison questions over >1 embedding-based (pdf/docx/
      image) file -> decompose into sub-questions, hybrid-retrieve each,
      merge+dedupe chunks, then generate as in the factual path.
- `GET /files/{file_id}/schema`: a thin proxy to structured-data-agent's
  `GET /schema/{file_id}`, returning a clear 502 (not a crash) if that
  service is unreachable.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import db, structured_client, summarization_client
from app.config import settings
from app.generation import generate_stream
from app.retrieval import (
    EMBEDDING_FILE_TYPES,
    STRUCTURED_FILE_TYPES,
    classify_query,
    decompose_query,
    judge_multi_coverage,
    retrieve,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="retrieval-orchestrator", version="0.1.0")


class HistoryMessage(BaseModel):
    role: str
    content: str


class QueryRequest(BaseModel):
    conversation_id: str
    question: str
    history: list[HistoryMessage] | None = None
    thinking_level: str = "standard"  # "fast"|"standard"|"deep" -- see CONTRACTS.md
    # Conversation-level custom instruction (see conversations.system_prompt in
    # MSSQL, gateway's settings panel) -- forwarded as a real system-role
    # message to whichever LLM call actually serves the question (every
    # route; see CONTRACTS.md "system_prompt").
    system_prompt: str | None = None
    # Base64 data URLs (e.g. "data:image/png;base64,...."), gateway-side
    # size/count-capped. Only ever used by the "general" route (see
    # _handle_general) -- genuine multimodal chat, not the OCR-based image
    # *ingestion* pipeline (ingestion-service/app/parsing.py:parse_image),
    # which is a completely separate, already-existing feature.
    images: list[str] | None = None


def _sse_event(event: str, data: str) -> str:
    """Format a single SSE event. `data` must already be a single-line
    string (JSON-encode first if it isn't plain text)."""
    # SSE data fields cannot contain raw newlines; escape them so a
    # multi-line chunk of text doesn't break the stream framing.
    safe_data = data.replace("\r\n", "\n").replace("\n", "\\n")
    return f"event: {event}\ndata: {safe_data}\n\n"


async def _stream_words(text: str) -> AsyncIterator[str]:
    """Stream `text` as SSE `token` events, split on whitespace -- used by
    the structured/summarize routes, which get a complete answer string
    back from a downstream service rather than an LLM token stream."""
    words = (text or "").split(" ")
    for i, word in enumerate(words):
        delta = word if i == 0 else f" {word}"
        yield _sse_event("token", delta)


def _build_prompt(question: str, chunks: list[dict[str, Any]], history: list[HistoryMessage] | None) -> str:
    """Render the question + retrieved chunks (with source_ref) into a
    single prompt string for the generation LLM."""
    context_block = "\n\n".join(
        f"[מקור: {chunk.get('source_ref') or 'לא ידוע'}]\n{chunk.get('text') or ''}" for chunk in chunks
    )
    history_block = ""
    if history:
        history_lines = "\n".join(f"{m.role}: {m.content}" for m in history)
        history_block = f"היסטוריית שיחה:\n{history_lines}\n\n"

    if context_block:
        return (
            f"{history_block}"
            f"ענה על השאלה הבאה בהתבסס על ההקשר שסופק. ציין את המקור כאשר רלוונטי.\n\n"
            f"הקשר:\n{context_block}\n\n"
            f"שאלה: {question}"
        )
    return f"{history_block}שאלה: {question}\n\n(לא נמצא הקשר רלוונטי בשיחה זו.)"


def _get_file_counts(conversation_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Returns (structured_ready_files, embedding_ready_files) for this
    conversation, newest-first. Never raises -- if MSSQL is unreachable
    this degrades to "no files known yet" (both empty), which routes to the
    factual path, i.e. the exact same behavior this service had before the
    routing feature existed.
    """
    try:
        files = db.list_files_for_conversation(conversation_id)
    except Exception as exc:
        logger.warning("Could not list files for conversation %s: %s", conversation_id, exc)
        return [], []

    structured = [
        f for f in files if f.get("file_type") in STRUCTURED_FILE_TYPES and f.get("index_status") == "ready"
    ]
    embedding = [
        f for f in files if f.get("file_type") in EMBEDDING_FILE_TYPES and f.get("index_status") == "ready"
    ]
    return structured, embedding


async def _stream_chunks_and_generate(
    question: str,
    chunks: list[dict[str, Any]],
    history: list[HistoryMessage] | None,
    thinking_level: str = "standard",
    system_prompt: str | None = None,
) -> AsyncIterator[str]:
    """Shared tail end of the factual and multi routes: render a prompt
    from `chunks`, stream generation tokens, then one `citation` event per
    chunk, then `done`. Exactly the event sequence the dense-only version
    of this service always produced.
    """
    prompt = _build_prompt(question, chunks, history)

    async for delta in generate_stream(
        prompt, context_chunks=chunks, thinking_level=thinking_level, system_prompt=system_prompt
    ):
        yield _sse_event("token", delta)

    for chunk in chunks:
        citation = {
            "source_ref": chunk.get("source_ref"),
            "snippet": (chunk.get("text") or "")[:300],
            "file_id": chunk.get("file_id"),
        }
        yield _sse_event("citation", json.dumps(citation, ensure_ascii=False))

    yield _sse_event("done", json.dumps({"chunks_used": len(chunks)}))


async def _handle_general(req: QueryRequest) -> AsyncIterator[str]:
    """No files at all in the conversation -- plain conversational route.
    No retrieval is attempted (there's nothing to retrieve against: no
    Milvus partition exists yet for a conversation with zero uploads), and
    the prompt deliberately omits any "no context was found" framing --
    that framing (used by the factual route when files exist but a
    specific question found no match) could otherwise bias the model
    toward refusing to just answer conversationally. `thinking_level` still
    flows through to `generate_stream` exactly as in every other route, so
    "deep" still asks the model to reason harder about its reply -- there's
    just no judge here, since plain conversation has no ground truth to
    verify an answer against the way a computed number, a summary's source
    chunks, or a multi-document comparison's file coverage do.
    """
    history_block = ""
    if req.history:
        history_lines = "\n".join(f"{m.role}: {m.content}" for m in req.history)
        history_block = f"היסטוריית שיחה:\n{history_lines}\n\n"
    prompt = f"{history_block}שאלה: {req.question}"

    async for delta in generate_stream(
        prompt,
        context_chunks=None,
        thinking_level=req.thinking_level,
        system_prompt=req.system_prompt,
        images=req.images,
    ):
        yield _sse_event("token", delta)

    yield _sse_event("done", json.dumps({"chunks_used": 0}))


async def _handle_factual(req: QueryRequest) -> AsyncIterator[str]:
    """Default route: hybrid retrieval (dense+BM25/RRF/rerank) + generation.
    This is the original (previously dense-only) path, now backed by
    app.retrieval.retrieve's hybrid implementation -- same inputs/outputs.
    Only reached once classify_query has ruled out "no files at all" (see
    its docstring) -- an empty `chunks` here means files exist but none
    matched, not that none were ever uploaded.
    """
    yield _sse_event("status", "מחפש ומנתח מידע רלוונטי...")
    chunks = retrieve(conversation_id=req.conversation_id, query=req.question, top_k=settings.TOP_K_RERANK)
    async for event in _stream_chunks_and_generate(
        req.question, chunks, req.history, req.thinking_level, system_prompt=req.system_prompt
    ):
        yield event


async def _handle_structured(req: QueryRequest, structured_file: dict[str, Any]) -> AsyncIterator[str]:
    """Route a question to structured-data-agent for the conversation's
    single ready xlsx/csv/json file, then stream its `answer` as tokens
    with a single synthetic citation (no chunk/source_ref -- the data isn't
    chunked, it's a NL->pandas query over the whole file).

    Emits one `reasoning` event (the generated pandas plan's steps, plus the
    judge's verdict when thinking_level=="deep") before streaming the
    answer -- see CONTRACTS.md's `reasoning` event shape.
    """
    file_id = str(structured_file["id"])
    yield _sse_event("status", "מתכנן ומריץ שאילתה על הנתונים...")
    try:
        result = structured_client.structured_query(
            file_id, req.question, req.thinking_level, system_prompt=req.system_prompt
        )
    except structured_client.StructuredQueryError as exc:
        logger.warning("structured route failed for file_id=%s: %s", file_id, exc)
        yield _sse_event("token", f"אירעה שגיאה בפנייה לשירות הנתונים המבניים: {exc}")
        yield _sse_event("done", json.dumps({"error": True}))
        return

    reasoning: dict[str, Any] = {"route": "structured", "steps": result.get("steps") or []}
    if result.get("judge") is not None:
        reasoning["judge"] = result["judge"]
    yield _sse_event("reasoning", json.dumps(reasoning, ensure_ascii=False))

    answer = result.get("answer") or ""
    async for event in _stream_words(answer):
        yield event

    citation = {"source_ref": "נתון מובנה", "snippet": answer[:300], "file_id": file_id}
    yield _sse_event("citation", json.dumps(citation, ensure_ascii=False))
    yield _sse_event("done", json.dumps({"chunks_used": 1}))


_GENERIC_SUMMARIZE_WORDS = {
    "סכם", "תמצת", "סיכום", "summary", "summarize", "summarise",
    "בבקשה", "please", "את", "של", "this", "the", "a", "an",
    "document", "מסמך", "זה", "קובץ", "file",
}


def _has_specific_focus(question: str) -> bool:
    """True if `question` names something beyond a bare "summarize this"
    request (see also summarization-service's own, independent
    `_focus_text` -- same idea, duplicated rather than shared because
    these are two separate services/deployables)."""
    words = re.findall(r"\w+", question.lower())
    return any(w not in _GENERIC_SUMMARIZE_WORDS and len(w) > 2 for w in words)


def _select_summarize_target(
    question: str, conversation_id: str, embedding_files: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Pick which embedding-indexed file a "summarize" question is about.

    With zero or one such file there's nothing to pick between. With more
    than one, blindly taking "the most recently uploaded" (the previous
    behavior) silently summarizes the wrong document whenever the question
    names a specific focus that actually belongs to an *earlier* upload
    (e.g. "summarize the hotel reimbursement rules" when the travel policy
    was uploaded before the expense policy) -- the judge added for
    thinking_level="deep" checks the summary's *content* against the focus,
    but can't fix having summarized the wrong file in the first place.
    Precedence:
      1. The question names exactly one file (by original filename) --
         unambiguous, wins outright.
      2. The question has some specific focus (not just "summarize this")
         -- probe with the same hybrid search the factual route uses and
         pick whichever file's chunks dominate the top results.
      3. No signal either way (a bare "summarize this") -- most recently
         uploaded, same default as before.
    """
    if len(embedding_files) <= 1:
        return embedding_files[0] if embedding_files else None

    q_lower = question.lower()
    name_matches = [
        f for f in embedding_files if f.get("original_name") and str(f["original_name"]).lower() in q_lower
    ]
    if len(name_matches) == 1:
        return name_matches[0]

    if _has_specific_focus(question):
        probe_chunks = retrieve(conversation_id=conversation_id, query=question, top_k=settings.TOP_K_DENSE)
        counts: dict[str, int] = {}
        for c in probe_chunks:
            fid = c.get("file_id")
            if fid is not None:
                counts[fid] = counts.get(fid, 0) + 1
        if counts:
            best_file_id = max(counts, key=lambda fid: counts[fid])
            match = next((f for f in embedding_files if str(f["id"]) == str(best_file_id)), None)
            if match is not None:
                return match

    return embedding_files[0]


async def _handle_summarize(
    req: QueryRequest, embedding_files: list[dict[str, Any]], structured_files: list[dict[str, Any]]
) -> AsyncIterator[str]:
    """Route a "summarize this" question to summarization-service, for
    whichever non-structured (pdf/docx/image) file the question is actually
    about (see _select_summarize_target) -- falling back to the most recent
    structured file if that's all there is. Polls until ready/failed/timeout,
    emitting a `status` event per poll iteration that's still in progress
    (summarizing a long document can take a while) and one `reasoning` event
    (which file was picked, how many chunks, the judge's verdict when
    thinking_level=="deep") before streaming the summary.
    """
    yield _sse_event("status", "בוחר קובץ מתאים לסיכום...")
    target = (
        _select_summarize_target(req.question, req.conversation_id, embedding_files)
        if embedding_files
        else (structured_files[0] if structured_files else None)
    )
    if target is None:
        yield _sse_event("token", "לא נמצאו קבצים בשיחה זו לסיכום.")
        yield _sse_event("done", json.dumps({"chunks_used": 0}))
        return

    file_id = str(target["id"])
    file_name = target.get("original_name") or file_id
    yield _sse_event("status", f'מסכם את "{file_name}"...')
    try:
        task_id = summarization_client.start_summarize(
            file_id, req.thinking_level, req.question, system_prompt=req.system_prompt
        )
        result: dict[str, Any] | None = None
        for poll_result in summarization_client.poll_summary_with_progress(task_id):
            result = poll_result
            if result.get("status") not in ("ready", "failed"):
                yield _sse_event("status", f'מסכם את "{file_name}"... (עדיין בעבודה)')
    except summarization_client.SummarizationError as exc:
        logger.warning("summarize route failed for file_id=%s: %s", file_id, exc)
        yield _sse_event("token", f"אירעה שגיאה בשירות הסיכום: {exc}")
        yield _sse_event("done", json.dumps({"error": True}))
        return

    status = (result or {}).get("status")
    if status != "ready":
        yield _sse_event("token", f"הסיכום לא הסתיים בזמן (סטטוס: {status}). נסה שוב בעוד מעט.")
        yield _sse_event("done", json.dumps({"error": True, "status": status}))
        return

    assert result is not None  # status == "ready" implies at least one poll iteration ran
    reasoning: dict[str, Any] = {"route": "summarize", "file": file_name, "chunk_count": result.get("chunk_count")}
    if result.get("judge") is not None:
        reasoning["judge"] = result["judge"]
    yield _sse_event("reasoning", json.dumps(reasoning, ensure_ascii=False))

    summary = result.get("summary") or ""
    async for event in _stream_words(summary):
        yield event

    citation = {"source_ref": "סיכום מסמך", "snippet": summary[:300], "file_id": file_id}
    yield _sse_event("citation", json.dumps(citation, ensure_ascii=False))
    yield _sse_event("done", json.dumps({"chunks_used": 1}))


async def _handle_multi(req: QueryRequest, embedding_files: list[dict[str, Any]]) -> AsyncIterator[str]:
    """Comparison route: decompose into 2-3 sub-questions, run hybrid
    retrieval on each independently, merge+dedupe by chunk_id, then
    generate over the unified context -- same tail as the factual route.

    When thinking_level=="deep": judge_multi_coverage checks whether the
    merged chunks actually represent every document being compared (the
    sub-questions are independent, so it's possible for two of them to both
    end up closest to the same document). On an incomplete verdict, one
    bounded correction: for each missing file_id, retrieve directly against
    that document (bypassing decomposition) and merge those chunks in too
    -- not an open loop, just one guaranteed-coverage pass. Emits one
    `reasoning` event (the sub-questions, plus the judge's verdict when
    applicable) before streaming the answer.
    """
    yield _sse_event("status", "מפרק את השאלה להשוואה...")
    sub_questions = decompose_query(req.question, req.thinking_level)

    seen_chunk_ids: set[str] = set()
    merged_chunks: list[dict[str, Any]] = []

    def _merge(chunks: list[dict[str, Any]]) -> None:
        for chunk in chunks:
            cid = chunk.get("chunk_id")
            if cid is not None and cid in seen_chunk_ids:
                continue
            if cid is not None:
                seen_chunk_ids.add(cid)
            merged_chunks.append(chunk)

    yield _sse_event("status", "מחפש מידע רלוונטי בכל המסמכים...")
    for sub_q in sub_questions:
        _merge(retrieve(conversation_id=req.conversation_id, query=sub_q, top_k=settings.TOP_K_RERANK))

    judge: dict[str, Any] | None = None
    if req.thinking_level == "deep" and embedding_files:
        judge = judge_multi_coverage(embedding_files, merged_chunks)
        if not judge.get("satisfactory", True):
            missing = judge.get("missing_file_ids") or []
            logger.info(
                "multi route judge unsatisfied for conversation=%s (%s); retrieving missing documents directly",
                req.conversation_id,
                judge.get("reason"),
            )
            yield _sse_event("status", "משלים מידע חסר ממסמכים נוספים...")
            for file_id in missing:
                _merge(
                    retrieve(
                        conversation_id=req.conversation_id,
                        query=req.question,
                        top_k=settings.TOP_K_RERANK,
                        file_id=str(file_id),
                    )
                )

    reasoning: dict[str, Any] = {"route": "multi", "sub_questions": sub_questions}
    if judge is not None:
        reasoning["judge"] = judge
    yield _sse_event("reasoning", json.dumps(reasoning, ensure_ascii=False))

    async for event in _stream_chunks_and_generate(
        req.question, merged_chunks, req.history, req.thinking_level, system_prompt=req.system_prompt
    ):
        yield event


async def _query_event_stream(req: QueryRequest) -> AsyncIterator[str]:
    try:
        structured_files, embedding_files = _get_file_counts(req.conversation_id)
        category = classify_query(
            req.question,
            structured_file_count=len(structured_files),
            embedding_file_count=len(embedding_files),
        )
        logger.info("conversation=%s classified as %r", req.conversation_id, category)

        if category == "general":
            handler = _handle_general(req)
        elif category == "structured" and structured_files:
            handler = _handle_structured(req, structured_files[0])
        elif category == "summarize":
            handler = _handle_summarize(req, embedding_files, structured_files)
        elif category == "multi":
            handler = _handle_multi(req, embedding_files)
        else:
            handler = _handle_factual(req)

        async for event in handler:
            yield event
    except Exception as exc:
        logger.exception("Error while handling /query for conversation %s", req.conversation_id)
        yield _sse_event("token", f"אירעה שגיאה בעיבוד השאלה: {exc}")
        yield _sse_event("done", json.dumps({"error": True}))


@app.post("/query")
async def query(req: QueryRequest) -> StreamingResponse:
    return StreamingResponse(_query_event_stream(req), media_type="text/event-stream")


@app.get("/files/{file_id}/schema")
async def get_file_schema(file_id: str) -> JSONResponse:
    url = f"{settings.STRUCTURED_AGENT_URL.rstrip('/')}/schema/{file_id}"
    try:
        async with httpx.AsyncClient(timeout=settings.HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(url)
        if resp.status_code >= 400:
            return JSONResponse(
                status_code=502,
                content={
                    "error": "structured-data-agent returned an error",
                    "upstream_status": resp.status_code,
                    "detail": resp.text,
                },
            )
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except httpx.RequestError as exc:
        logger.warning("structured-data-agent unreachable at %s: %s", url, exc)
        return JSONResponse(
            status_code=502,
            content={
                "error": "structured-data-agent is unreachable",
                "detail": str(exc),
                "upstream_url": url,
            },
        )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
