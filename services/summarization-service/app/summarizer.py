"""Hierarchical map-reduce summarization.

summarize_text() follows the same MOCK_LLM mock/real contract described in
shared/CONTRACTS.md: when MOCK_LLM=true it returns a deterministic template
sentence built from the start of the input text (good for exercising the
whole pipeline end-to-end without a GPU); when MOCK_LLM=false it calls the
OpenAI-compatible VLLM_ROUTER_URL /chat/completions endpoint (Llama-3.1-8B
per the project plan, which also serves as the map-summarization model).

hierarchical_summarize() drives the overall flow:
    Map:    group raw chunk texts into batches (bounded by both chunk count
            and total characters) and summarize each batch independently.
            If the caller passed a `question` with a specific focus (not
            just a bare "summarize this"), the map instruction asks for a
            summary centered on that focus instead of a generic one.
    Reduce: if more than one map-summary came out, recursively merge them
            in groups until a single final summary remains, capped at
            REDUCE_MAX_DEPTH levels so a pathological input (e.g. the mock
            summarizer not shrinking text enough) can't loop forever.

judge_summary() + the `extra_context` param on hierarchical_summarize() are
the "deep" thinking_level judge+replan pair (see CONTRACTS.md
"thinking_level"): app/tasks.py calls judge_summary() once after a summary
is produced, and if it's flagged unsatisfactory, calls
hierarchical_summarize() again with `extra_context` set to the judge's
reason -- one judge call, at most one full re-run, never an open loop.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_INSTRUCTION = "סכם בקצרה"
_MAP_INSTRUCTION = "סכם בקצרה את הקטעים הבאים מתוך מסמך ארוך"
_REDUCE_INSTRUCTION = "מזג את הסיכומים הבאים לסיכום ביניים אחד קצר"
_FINAL_REDUCE_INSTRUCTION = "מזג את כל הסיכומים הבאים לסיכום סופי אחד קצר וממצה"

# Generic "please summarize" phrasing that carries no specific focus of its
# own -- if the user's question is just one of these (or empty), there's no
# "focus" to thread into the map instruction or to check coverage of later.
_GENERIC_SUMMARY_PHRASES = {
    "סכם", "סכם בבקשה", "סכם את המסמך", "סכם את זה", "תמצת", "תמצת בבקשה",
    "summarize", "summarize this", "summarize the document", "please summarize",
}

# thinking_level ("fast"|"standard"|"deep", see CONTRACTS.md) -> the
# reasoning_effort value sent on every map/reduce/judge call. Documented
# assumption about the field name the model behind VLLM_ROUTER_URL expects
# (mirrors OpenAI's o-series convention).
_REASONING_EFFORT = {"fast": "low", "standard": "medium", "deep": "high"}


def _focus_text(question: str | None) -> str | None:
    """Return `question` if it carries a specific focus beyond a bare
    "summarize this" request, else None. Used both to decide whether to
    thread a focus into the map instruction and, in judge_summary(), to
    decide whether there's anything concrete to check coverage of."""
    if not question:
        return None
    q = question.strip()
    if not q or q.lower() in _GENERIC_SUMMARY_PHRASES:
        return None
    return q


def _map_instruction(
    question: str | None, extra_context: str | None, system_prompt: str | None = None
) -> str:
    focus = _focus_text(question)
    instruction = _MAP_INSTRUCTION
    if focus:
        instruction += f", תוך התמקדות במענה על: {focus}"
    if system_prompt:
        instruction += f". הנחיה כללית נוספת מהמשתמש: {system_prompt}"
    if extra_context:
        instruction += f". הערה מניסיון קודם: {extra_context}"
    return instruction


def summarize_text(text: str, instruction: str = DEFAULT_INSTRUCTION, thinking_level: str = "standard") -> str:
    """Summarize one blob of text with a single LLM call (mock or real)."""
    if settings.MOCK_LLM:
        return _mock_summarize(text, instruction)
    return _real_summarize(text, instruction, thinking_level)


def _mock_summarize(text: str, instruction: str) -> str:
    """Deterministic template summary that folds in the instruction and the
    start of the text, so mock runs are reproducible and visibly shrink the
    input (important so the reduce recursion actually converges).
    """
    normalized = " ".join(text.split())
    snippet = normalized[:160]
    word_count = len(normalized.split())
    return f'[סיכום מדומה | {instruction}] פותח ב-"{snippet}" (סוכמו כ-{word_count} מילים).'


def _real_summarize(text: str, instruction: str, thinking_level: str = "standard") -> str:
    """Call the OpenAI-compatible VLLM_ROUTER_URL /chat/completions endpoint."""
    url = f"{settings.VLLM_ROUTER_URL.rstrip('/')}/chat/completions"
    payload = {
        "model": settings.VLLM_ROUTER_MODEL,
        "messages": [
            {"role": "system", "content": instruction},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
        "reasoning_effort": _REASONING_EFFORT.get(thinking_level, "medium"),
    }
    with httpx.Client(timeout=settings.HTTP_TIMEOUT_SECONDS) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"]


def _group_texts(texts: list[str], max_count: int, max_chars: int) -> list[list[str]]:
    """Bucket texts into groups, each holding at most `max_count` items and
    at most `max_chars` combined characters (whichever limit is hit first
    starts a new group). Always makes forward progress: a single item that
    alone exceeds max_chars still gets its own group rather than being
    dropped or looped on.
    """
    groups: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    for t in texts:
        if current and (len(current) >= max_count or current_chars + len(t) > max_chars):
            groups.append(current)
            current = []
            current_chars = 0
        current.append(t)
        current_chars += len(t)
    if current:
        groups.append(current)
    return groups


def _map_summarize(
    chunk_texts: list[str],
    thinking_level: str,
    question: str | None,
    extra_context: str | None,
    system_prompt: str | None = None,
) -> list[str]:
    groups = _group_texts(chunk_texts, settings.MAP_GROUP_SIZE, settings.MAP_GROUP_MAX_CHARS)
    logger.info("Map stage: %d chunk(s) grouped into %d batch(es)", len(chunk_texts), len(groups))
    instruction = _map_instruction(question, extra_context, system_prompt)
    summaries = []
    for i, group in enumerate(groups):
        joined = "\n\n".join(group)
        summary = summarize_text(joined, instruction=instruction, thinking_level=thinking_level)
        logger.info("Map batch %d/%d: %d chunk(s) -> %d-char summary", i + 1, len(groups), len(group), len(summary))
        summaries.append(summary)
    return summaries


def _reduce_summaries(summaries: list[str], depth: int, thinking_level: str) -> str:
    if not summaries:
        return ""
    if len(summaries) == 1:
        return summaries[0]

    if depth >= settings.REDUCE_MAX_DEPTH:
        logger.warning("Reduce stage hit max depth (%d); force-merging %d summaries in one call",
                        settings.REDUCE_MAX_DEPTH, len(summaries))
        combined = "\n\n".join(summaries)
        return summarize_text(combined, instruction=_FINAL_REDUCE_INSTRUCTION, thinking_level=thinking_level)

    groups = _group_texts(summaries, settings.REDUCE_GROUP_SIZE, settings.REDUCE_GROUP_MAX_CHARS)
    if len(groups) >= len(summaries):
        # Grouping made no progress (shouldn't normally happen with sane
        # REDUCE_GROUP_SIZE/MAX_CHARS) -- force a single merge so we always
        # terminate instead of recursing with an unchanged list.
        logger.warning("Reduce grouping made no progress (%d groups for %d summaries); force-merging",
                        len(groups), len(summaries))
        combined = "\n\n".join(summaries)
        return summarize_text(combined, instruction=_FINAL_REDUCE_INSTRUCTION, thinking_level=thinking_level)

    logger.info("Reduce stage depth=%d: %d summaries grouped into %d batch(es)", depth, len(summaries), len(groups))
    next_level = [
        summarize_text("\n\n".join(group), instruction=_REDUCE_INSTRUCTION, thinking_level=thinking_level)
        for group in groups
    ]
    return _reduce_summaries(next_level, depth + 1, thinking_level)


def hierarchical_summarize(
    chunks: list[dict],
    thinking_level: str = "standard",
    question: str | None = None,
    extra_context: str | None = None,
    system_prompt: str | None = None,
) -> str:
    """Full map-reduce pipeline over an ordered list of chunk dicts (each
    with at least a "text" key). Returns the single final summary string.

    ``question`` is the user's actual chat message that routed to the
    summarize path (e.g. "summarize the risk section" vs. a bare "summarize
    this") -- when it carries a specific focus (see `_focus_text`), the map
    stage is instructed to center on it instead of producing a generic
    summary. ``extra_context`` is only ever set by app/tasks.py's judge+
    replan re-run (the judge's stated reason for being unsatisfied with the
    first attempt); it's appended to the map instruction too. ``system_prompt``
    is the conversation's custom instruction (see CONTRACTS.md
    "system_prompt"), also folded into the map instruction.
    """
    chunk_texts = [c["text"] for c in chunks if c.get("text")]
    if not chunk_texts:
        return ""

    map_summaries = _map_summarize(chunk_texts, thinking_level, question, extra_context, system_prompt)
    final_summary = _reduce_summaries(map_summaries, depth=1, thinking_level=thinking_level)
    return final_summary


# ---------------------------------------------------------------------------
# Judge (thinking_level == "deep" only)
# ---------------------------------------------------------------------------
#
# Unlike structured-data-agent's judge (which reacts to a result that ran
# without raising but looks wrong, e.g. a silent NaN/Inf), there's no
# "exception" equivalent here -- summarize_text() always returns *some*
# string. What can still go wrong: the map-reduce pipeline produces a
# degenerate (empty/near-empty) summary (e.g. a file with zero usable
# chunks), or -- when the question asked for a specific focus -- the final
# summary drops that focus entirely (the reduce stage's instruction is
# generic, so a focus a map-stage summary mentioned could get merged away).
# app/tasks.py calls this once when thinking_level=="deep", and on an
# unsatisfactory verdict re-runs hierarchical_summarize() exactly once with
# `extra_context` set to the reason -- never an open loop.


def judge_summary(
    question: str | None, chunk_texts: list[str], summary: str, thinking_level: str = "deep"
) -> dict[str, object]:
    if settings.MOCK_LLM:
        return _mock_judge(question, summary)
    verdict = _real_judge(question, chunk_texts, summary, thinking_level)
    return verdict if verdict is not None else {"satisfactory": True, "reason": "judge call failed; assuming ok"}


def _mock_judge(question: str | None, summary: str) -> dict[str, object]:
    """Deterministic checks, no LLM call:

    1. Degenerate output (empty/near-empty summary) -- always checkable,
       and the one failure mode genuinely reachable in mock mode (e.g. a
       file whose chunks all came back empty-text from OCR).
    2. Focus coverage, when the question specified one -- checkable in
       principle, but note the mock summarizer (`_mock_summarize`) embeds
       its *instruction* string verbatim into its output, so once a focus
       is threaded into the map instruction, a mock-mode summary will
       trivially contain it -- this check is meant for real mode, where an
       LLM can genuinely drop a focus the instruction asked for; it's kept
       here (rather than skipped in mock mode) because it's still a
       correct, inexpensive sanity check, not a disguised no-op.
    """
    normalized = (summary or "").strip()
    if len(normalized) < 10:
        return {"satisfactory": False, "reason": "הסיכום ריק או קצר מדי להיות שימושי"}

    focus = _focus_text(question)
    if focus:
        focus_words = [w for w in focus.split() if len(w) > 2]
        if focus_words and not any(w.lower() in normalized.lower() for w in focus_words):
            return {
                "satisfactory": False,
                "reason": f"הסיכום לא מתייחס באופן נראה-לעין למוקד שהתבקש: '{focus}'",
            }

    return {"satisfactory": True, "reason": "הסיכום סביר ביחס לשאלה"}


def _build_judge_prompt(question: str | None, chunk_texts: list[str], summary: str) -> str:
    source_sample = "\n\n".join(chunk_texts)[:4000]
    focus = _focus_text(question)
    focus_line = f"\nהמשתמש ביקש להתמקד ב: {focus}" if focus else ""
    return (
        "קיבלת קטעי מקור מתוך מסמך, וסיכום שנוצר עבורם. שפוט אם הסיכום סביר "
        "וממצה ביחס למקור (ולמוקד שהתבקש, אם יש כזה) -- לא בהכרח מושלם, רק "
        "שלא חסר ממנו משהו מהותי ושלא הוא ריק/חסר תוכן.\n"
        f"{focus_line}\n\n"
        f"קטעי מקור (עד 4000 תווים):\n{source_sample}\n\n"
        f"סיכום שנוצר:\n{summary}\n\n"
        'החזר JSON בלבד: {"satisfactory": true/false, "reason": "..."}'
    )


def _real_judge(
    question: str | None, chunk_texts: list[str], summary: str, thinking_level: str
) -> dict[str, object] | None:
    import json

    url = f"{settings.VLLM_ROUTER_URL.rstrip('/')}/chat/completions"
    payload = {
        "model": settings.VLLM_ROUTER_MODEL,
        "messages": [{"role": "user", "content": _build_judge_prompt(question, chunk_texts, summary)}],
        "reasoning_effort": _REASONING_EFFORT.get(thinking_level, "high"),
    }
    try:
        with httpx.Client(timeout=settings.HTTP_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if isinstance(parsed.get("satisfactory"), bool):
            return {"satisfactory": parsed["satisfactory"], "reason": str(parsed.get("reason", ""))}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Router LLM judge call failed (%s); treating summary as satisfactory", exc)
    return None
