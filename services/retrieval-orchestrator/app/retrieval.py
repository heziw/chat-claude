"""Retrieval + query-routing layer for retrieval-orchestrator.

Two public entry points app/main.py builds on:

- ``classify_query(question, structured_file_count, embedding_file_count)``:
  query routing. Returns one of ``"factual"`` / ``"structured"`` /
  ``"summarize"`` / ``"multi"``. See the function docstring for the exact
  precedence rules.
- ``retrieve(conversation_id, query, top_k)``: hybrid retrieval. Runs dense
  (Milvus HNSW) + BM25 (client-side ``rank_bm25``) search, fuses the two
  candidate lists with Reciprocal Rank Fusion (RRF), then reranks the fused
  set. Returns the final top ``top_k`` chunks.

Also ``decompose_query(question)`` (multi-document query decomposition, used
by the "multi" route) and ``embed_query(text)`` (mock/real embedding, same
contract as before).

**Why BM25 is client-side, not Milvus-native**: Milvus 2.4.9 (the version
pinned in docker-compose.yml) has no native full-text/BM25 search -- that
was only added in Milvus 2.5. Adding a SPARSE_FLOAT_VECTOR field to the
`doc_chunks` collection now would require a schema migration coordinated
with ingestion-service (which owns the write path) and isn't worth it at
this project's scale: each conversation's Milvus partition holds at most a
few thousand chunks, so pulling the whole partition with `Collection.query()`
and scoring it with `rank_bm25.BM25Okapi` in-process is cheap and simple.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# pymilvus talks to a live Milvus server. We import it eagerly (it's a plain
# pip dependency and importing it does not open any connection), but we never
# call connections.connect()/search() at import time -- only lazily, inside
# try/except, when a request actually needs Milvus. This way `import app.main`
# always succeeds even if Milvus itself is unreachable.
try:
    from pymilvus import Collection, connections, utility
except Exception as exc:  # pragma: no cover - only hit if pymilvus itself is missing
    logger.warning("pymilvus import failed (%s); Milvus search will be unavailable", exc)
    Collection = None  # type: ignore[assignment]
    connections = None  # type: ignore[assignment]
    utility = None  # type: ignore[assignment]

_MILVUS_ALIAS = "default"
_milvus_connected = False

# file_metadata.file_type values that go through the embedding pipeline
# (Milvus) vs. the side-store (parquet) pipeline -- see CONTRACTS.md. Note
# ingestion-service stamps file_type as the *raw lowercased file extension*
# (app/main.py:_file_type_from_name), not a normalized "image" -- hence the
# explicit image extensions here rather than a single "image" value.
EMBEDDING_FILE_TYPES = {"pdf", "docx", "image", "png", "jpg", "jpeg", "gif", "bmp", "tiff"}
STRUCTURED_FILE_TYPES = {"xlsx", "csv", "json"}

_VALID_CATEGORIES = {"factual", "structured", "summarize", "multi"}

# thinking_level ("fast"|"standard"|"deep", see CONTRACTS.md) -> the
# reasoning_effort value sent to the router model for the one real-mode
# call in this module that's worth varying it for (decompose_query).
# classify_query's router call deliberately does NOT use this -- routing is
# a cheap, mechanical categorization, not a task that benefits from deeper
# reasoning.
_REASONING_EFFORT = {"fast": "low", "standard": "medium", "deep": "high"}

# --- Query-routing keyword heuristics (mock mode, and the real-mode fallback
# when the router LLM call fails/doesn't parse) ---
_SUMMARIZE_KEYWORDS = ["סכם", "תמצת", "סיכום", "summary", "summarize"]
_STRUCTURED_KEYWORDS = [
    "סכום", "כמה", "ממוצע", "מקסימום", "מינימום", "סך",
    "sum", "average", "avg", "count", "max", "min", "total",
]
_MULTI_KEYWORDS = ["השווה", "השוואה", "לעומת", "בין", "compare", "comparison", "difference", "versus"]


# ---------------------------------------------------------------------------
# Query routing
# ---------------------------------------------------------------------------


def classify_query(question: str, structured_file_count: int = 0, embedding_file_count: int = 0) -> str:
    """Classify `question` into one of 5 routes.

    Precedence (most to least specific signal):
    0. "general" -- hard override: the conversation has **no files at all**
       (neither structured nor embedding-indexed). None of the other 4
       routes make sense with nothing to retrieve/summarize/compute on, so
       this short-circuits before even attempting LLM/heuristic
       classification (cheap, decisive, and saves a pointless router call
       in real mode) -- see app/main.py:_handle_general.
    1. "summarize" -- summary keywords present ("סכם"/"תמצת"/"summary"/...).
    2. "multi" -- comparison keywords present ("השווה"/"compare"/"בין...ל"/...)
       AND there's more than one embedding-based (pdf/docx/image) file to
       compare across.
    3. "structured" -- there's exactly one ready xlsx/csv/json file in the
       conversation. Per the project plan this conversation has, by
       construction, only one thing a structured question could be about,
       so this fires even without explicit aggregation keywords ("a
       reasonable default") -- the keyword list is used in mock mode to
       pick a better sub-score when deciding between routes via the LLM,
       but isn't a hard requirement here.
    4. "factual" -- default (the original, already-working path). Only
       reachable once rule 0 has ruled out "no files at all", so an empty
       retrieval result here means "files exist but none matched/indexed
       yet", not "no files were ever uploaded" (see generation.py's
       `context_chunks is None` vs `[]` distinction).

    In real mode (MOCK_LLM=false) the router LLM is consulted first (for
    rules 1-4 only); its answer only overrides the heuristic above if it
    parses cleanly to one of the 4 known categories, otherwise falls back
    to the heuristic.
    """
    if structured_file_count == 0 and embedding_file_count == 0:
        return "general"

    intent = None
    if not settings.MOCK_LLM:
        intent = _llm_classify(question)

    if intent not in _VALID_CATEGORIES:
        intent = _heuristic_classify(question, structured_file_count, embedding_file_count)

    # Hard constraints: don't route to a path that has no data to serve it,
    # regardless of what the LLM/heuristic guessed.
    if intent == "structured" and structured_file_count != 1:
        intent = _heuristic_classify(
            question, structured_file_count=0, embedding_file_count=embedding_file_count
        )
    if intent == "multi" and embedding_file_count <= 1:
        intent = "factual"

    return intent


def _heuristic_classify(question: str, structured_file_count: int, embedding_file_count: int) -> str:
    q = question.lower()

    if any(kw in q for kw in _SUMMARIZE_KEYWORDS):
        return "summarize"

    if embedding_file_count > 1 and any(kw in q for kw in _MULTI_KEYWORDS):
        return "multi"

    if structured_file_count == 1:
        return "structured"

    return "factual"


def _llm_classify(question: str) -> str | None:
    """Ask VLLM_ROUTER_URL to classify `question`. Returns None (triggering
    the heuristic fallback in classify_query) on any failure -- unreachable
    service, non-2xx, or a response that doesn't parse to a known category.
    """
    url = f"{settings.VLLM_ROUTER_URL.rstrip('/')}/chat/completions"
    prompt = (
        "סווג את השאלה הבאה לאחת מהקטגוריות בלבד: factual, structured, summarize, multi.\n"
        "- structured: שאלה על נתונים טבלאיים (סכומים, ממוצעים, מספרים).\n"
        "- summarize: בקשה לסיכום/תמצות מסמך.\n"
        "- multi: שאלת השוואה בין כמה מסמכים.\n"
        "- factual: כל שאלה אחרת.\n"
        'החזר JSON בלבד בפורמט: {"category": "<one of the above>"}\n\n'
        f"שאלה: {question}"
    )
    payload = {
        "model": settings.VLLM_ROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        with httpx.Client(timeout=settings.HTTP_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        category = parsed.get("category")
        if category in _VALID_CATEGORIES:
            return category
        logger.warning("Router LLM returned unknown category %r; falling back to heuristic", category)
    except Exception as exc:
        logger.warning("Router LLM classification failed (%s); falling back to heuristic", exc)
    return None


# --- Multi-document query decomposition (the "multi" route) ---

_MULTI_SPLIT_RE = re.compile(
    r"\s+(?:לעומת|בהשוואה\s+ל|compared\s+to|versus|vs\.?|and)\s+", re.IGNORECASE
)
_BETWEEN_RE = re.compile(r"בין\s+(.+?)\s+(?:לבין|ל)\s+(.+)")


def decompose_query(question: str, thinking_level: str = "standard") -> list[str]:
    """Split a comparison question into 2-3 sub-questions, one per
    document/aspect being compared, so each can be run through hybrid
    retrieval independently (app/main.py:_handle_multi merges+dedupes the
    resulting chunks before generation).
    """
    if settings.MOCK_LLM:
        return _heuristic_decompose(question)
    decomposed = _llm_decompose(question, thinking_level)
    if decomposed:
        return decomposed
    return _heuristic_decompose(question)


def _heuristic_decompose(question: str) -> list[str]:
    parts = [p.strip() for p in _MULTI_SPLIT_RE.split(question) if p.strip()]
    if len(parts) >= 2:
        return parts[:3]

    m = _BETWEEN_RE.search(question)
    if m:
        return [m.group(1).strip(), m.group(2).strip()]

    # No clear split found -- fall back to using the original question as
    # both sub-questions (per spec: still runs hybrid retrieval, just
    # without any real decomposition benefit).
    return [question, question]


def _llm_decompose(question: str, thinking_level: str = "standard") -> list[str] | None:
    url = f"{settings.VLLM_ROUTER_URL.rstrip('/')}/chat/completions"
    prompt = (
        "פרק את שאלת ההשוואה הבאה ל-2 או 3 תתי-שאלות נפרדות, אחת לכל מסמך/היבט "
        'שיש להשוות. החזר JSON בלבד: {"sub_questions": ["...", "..."]}\n\n'
        f"שאלה: {question}"
    )
    payload = {
        "model": settings.VLLM_ROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "reasoning_effort": _REASONING_EFFORT.get(thinking_level, "medium"),
    }
    try:
        with httpx.Client(timeout=settings.HTTP_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        sub_questions = parsed.get("sub_questions")
        if isinstance(sub_questions, list) and 1 < len(sub_questions) <= 3:
            cleaned = [str(s).strip() for s in sub_questions if str(s).strip()]
            if len(cleaned) >= 2:
                return cleaned
    except Exception as exc:
        logger.warning("Router LLM decomposition failed (%s); falling back to heuristic", exc)
    return None


# ---------------------------------------------------------------------------
# Embedding (unchanged mock/real contract)
# ---------------------------------------------------------------------------


def _partition_name(conversation_id: str) -> str:
    """Milvus partition name = conv_<conversation_id without dashes> (CONTRACTS.md)."""
    return f"conv_{conversation_id.replace('-', '')}"


def embed_query(text: str) -> list[float]:
    """Embed a query string into a dense vector of dim settings.EMBED_DIM.

    Mock/real contract matches the rest of the system (see module docstring
    and shared/CONTRACTS.md "Mock mode" section).
    """
    if settings.MOCK_LLM:
        return _mock_embed(text)
    return _real_embed(text)


def _mock_embed(text: str) -> list[float]:
    """Deterministic pseudo-random embedding derived from a hash of `text`.

    Not semantically meaningful -- only used so mock-mode runs are
    reproducible and exercise the full pipeline shape (correct vector dim,
    stable results across repeated calls with the same text).
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    # Expand the 32-byte digest into EMBED_DIM floats in [-1, 1] by re-hashing
    # with a counter, avoiding any extra numeric dependency.
    vector: list[float] = []
    counter = 0
    while len(vector) < settings.EMBED_DIM:
        block = hashlib.sha256(digest + counter.to_bytes(4, "big")).digest()
        for i in range(0, len(block), 2):
            if len(vector) >= settings.EMBED_DIM:
                break
            chunk = block[i : i + 2]
            val = int.from_bytes(chunk, "big")
            vector.append((val / 65535.0) * 2.0 - 1.0)
        counter += 1
    return vector[: settings.EMBED_DIM]


def _real_embed(text: str) -> list[float]:
    """Call the OpenAI-compatible VLLM_EMBED_URL /embeddings endpoint."""
    url = f"{settings.VLLM_EMBED_URL.rstrip('/')}/embeddings"
    payload = {"model": settings.VLLM_EMBED_MODEL, "input": text}
    with httpx.Client(timeout=settings.HTTP_TIMEOUT_SECONDS) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    return data["data"][0]["embedding"]


# ---------------------------------------------------------------------------
# Milvus access (dense search + fetch-all-for-BM25)
# ---------------------------------------------------------------------------


def _ensure_milvus_connected() -> None:
    global _milvus_connected
    if _milvus_connected:
        return
    if connections is None:
        raise RuntimeError("pymilvus is not available")
    connections.connect(alias=_MILVUS_ALIAS, host=settings.MILVUS_HOST, port=str(settings.MILVUS_PORT))
    _milvus_connected = True


def _get_loaded_partition_collection(conversation_id: str) -> tuple[Any, str] | None:
    """Shared setup for both _search_dense and _fetch_all_chunks_in_partition:
    connect, verify the collection+partition exist, and load the collection.
    Returns (collection, partition_name), or None if there's legitimately no
    data yet (collection/partition missing) -- never raises for that case.
    """
    partition = _partition_name(conversation_id)
    _ensure_milvus_connected()

    if not utility.has_collection(settings.MILVUS_COLLECTION, using=_MILVUS_ALIAS):
        logger.info("Milvus collection %s does not exist yet", settings.MILVUS_COLLECTION)
        return None

    collection = Collection(settings.MILVUS_COLLECTION, using=_MILVUS_ALIAS)

    if not collection.has_partition(partition):
        logger.info("Partition %s does not exist yet for conversation %s", partition, conversation_id)
        return None

    collection.load()
    return collection, partition


def _search_dense(
    conversation_id: str, query_vector: list[float], top_k: int, file_id_filter: str | None = None
) -> list[dict[str, Any]]:
    """Dense-only vector search against the `doc_chunks` Milvus collection,
    scoped to this conversation's partition (and, if given, to a single
    `file_id` -- used by the "multi" route's judge-triggered replan to pull
    guaranteed content from a specific document rather than hoping
    decompose_query's sub-questions happened to cover it).

    Returns an empty list (never raises) when Milvus is unreachable, the
    collection doesn't exist yet, or the conversation's partition doesn't
    exist yet (e.g. no files uploaded to this conversation, or files are
    still indexing) -- all of those are legitimate "no context yet" states
    that the caller turns into a friendly explanatory answer rather than an
    error.
    """
    try:
        setup = _get_loaded_partition_collection(conversation_id)
        if setup is None:
            return []
        collection, partition = setup

        search_params = {"metric_type": "COSINE", "params": {"ef": 64}}
        results = collection.search(
            data=[query_vector],
            anns_field="dense_vector",
            param=search_params,
            limit=top_k,
            partition_names=[partition],
            expr=f'file_id == "{file_id_filter}"' if file_id_filter else None,
            output_fields=["chunk_id", "file_id", "file_type", "source_ref", "text"],
        )

        hits = []
        for hit in results[0]:
            entity = hit.entity
            hits.append(
                {
                    "chunk_id": entity.get("chunk_id"),
                    "file_id": entity.get("file_id"),
                    "file_type": entity.get("file_type"),
                    "source_ref": entity.get("source_ref"),
                    "text": entity.get("text"),
                    "score": float(hit.distance) if hit.distance is not None else None,
                }
            )
        return hits
    except Exception as exc:
        logger.warning("Dense Milvus search failed for conversation %s: %s", conversation_id, exc)
        return []


def _fetch_all_chunks_in_partition(
    conversation_id: str, file_id_filter: str | None = None
) -> list[dict[str, Any]]:
    """Fetch every chunk in this conversation's partition (no vector, just a
    scalar filter) so BM25 can be scored client-side over the whole corpus
    (optionally scoped to one `file_id`, same rationale as _search_dense's
    `file_id_filter`). Fine at this project's scale (hundreds-to-thousands
    of chunks per conversation partition, not millions). Returns [] (never
    raises) on any Milvus issue, same "no context yet" convention as
    _search_dense.
    """
    try:
        setup = _get_loaded_partition_collection(conversation_id)
        if setup is None:
            return []
        collection, partition = setup

        expr = 'chunk_id != ""'
        if file_id_filter:
            expr += f' and file_id == "{file_id_filter}"'
        rows = collection.query(
            expr=expr,
            output_fields=["chunk_id", "file_id", "file_type", "source_ref", "text"],
            partition_names=[partition],
            limit=16384,
        )
        return [
            {
                "chunk_id": r.get("chunk_id"),
                "file_id": r.get("file_id"),
                "file_type": r.get("file_type"),
                "source_ref": r.get("source_ref"),
                "text": r.get("text"),
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("Fetching all chunks for BM25 failed for conversation %s: %s", conversation_id, exc)
        return []


# ---------------------------------------------------------------------------
# BM25 (client-side, rank_bm25)
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    """Simple whitespace/punctuation tokenizer: lowercase (no-op for
    Hebrew, matters for English/mixed text) + split on \\w+ runs. Not
    stemming/normalizing -- good enough for client-side BM25 over a
    per-conversation corpus of at most a few thousand chunks."""
    return re.findall(r"\w+", (text or "").lower())


def _search_bm25(
    conversation_id: str, query: str, top_k: int, file_id_filter: str | None = None
) -> list[dict[str, Any]]:
    """BM25 search over every chunk in the conversation's partition
    (optionally scoped to one `file_id`, see _fetch_all_chunks_in_partition).
    Returns [] (never raises) if there are no chunks, or if `rank_bm25`
    itself can't be imported (defensive -- it's a required dependency per
    requirements.txt, but retrieve() should still degrade to dense-only
    rather than 500 if it's somehow missing).
    """
    all_chunks = _fetch_all_chunks_in_partition(conversation_id, file_id_filter)
    if not all_chunks:
        return []

    try:
        from rank_bm25 import BM25Okapi
    except Exception as exc:
        logger.warning("rank_bm25 not available (%s); skipping BM25 stage", exc)
        return []

    tokenized_corpus = [_tokenize(c.get("text") or "") for c in all_chunks]
    bm25 = BM25Okapi(tokenized_corpus)
    scores = bm25.get_scores(_tokenize(query))

    ranked_idx = sorted(range(len(all_chunks)), key=lambda i: scores[i], reverse=True)[:top_k]
    results = []
    for i in ranked_idx:
        chunk = dict(all_chunks[i])
        chunk["score"] = float(scores[i])
        results.append(chunk)
    return results


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


def _rrf_fuse(
    dense: list[dict[str, Any]], bm25: list[dict[str, Any]], top_n: int, k: int
) -> list[dict[str, Any]]:
    """Standard RRF: score(doc) = sum over lists containing doc of
    1/(k + rank), rank 1-based. Fuses by chunk_id, keeping whichever
    entity dict (text/source_ref/etc.) was seen first."""
    rrf_scores: dict[str, float] = {}
    entities: dict[str, dict[str, Any]] = {}

    for source in (dense, bm25):
        for rank, chunk in enumerate(source, start=1):
            cid = chunk.get("chunk_id")
            if cid is None:
                continue
            rrf_scores[cid] = rrf_scores.get(cid, 0.0) + 1.0 / (k + rank)
            entities.setdefault(cid, chunk)

    ranked_ids = sorted(rrf_scores.keys(), key=lambda cid: rrf_scores[cid], reverse=True)[:top_n]
    fused = []
    for cid in ranked_ids:
        entity = dict(entities[cid])
        entity["score"] = rrf_scores[cid]
        fused.append(entity)
    return fused


# ---------------------------------------------------------------------------
# Reranker
# ---------------------------------------------------------------------------


def _rerank(query: str, candidates: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    if not candidates:
        return []

    if settings.MOCK_LLM:
        scores = _mock_rerank_scores(query, candidates)
    else:
        scores = _real_rerank_scores(query, candidates)

    ranked = sorted(zip(candidates, scores), key=lambda pair: pair[1], reverse=True)
    return [chunk for chunk, _ in ranked[:top_k]]


def _mock_rerank_scores(query: str, candidates: list[dict[str, Any]]) -> list[float]:
    """Deterministic mock reranker: score = size of the word-overlap set
    between query tokens and chunk-text tokens. Simple, but exercises the
    same "rerank reorders the RRF list" code path as a real cross-encoder."""
    q_tokens = set(_tokenize(query))
    return [float(len(q_tokens & set(_tokenize(c.get("text") or "")))) for c in candidates]


def _real_rerank_scores(query: str, candidates: list[dict[str, Any]]) -> list[float]:
    """Call VLLM_RERANK_URL. Endpoint shape assumption (documented here since
    CONTRACTS.md doesn't pin it down beyond "OpenAI-compatible-ish /v1/..."):
    vLLM served with `--task score`/a cross-encoder reranker model exposes a
    Cohere/Jina-style rerank endpoint, so we POST
    `{VLLM_RERANK_URL}/rerank` with `{"model", "query", "documents"}` and
    expect back `{"results": [{"index": i, "relevance_score": s}, ...]}`.
    On any failure, falls back to preserving the incoming (RRF) order rather
    than failing the whole request.
    """
    url = f"{settings.VLLM_RERANK_URL.rstrip('/')}/rerank"
    documents = [c.get("text") or "" for c in candidates]
    payload = {"model": settings.VLLM_RERANK_MODEL, "query": query, "documents": documents}
    try:
        with httpx.Client(timeout=settings.HTTP_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        scores = [0.0] * len(candidates)
        for item in data.get("results", []):
            idx = item.get("index")
            if idx is not None and 0 <= idx < len(scores):
                scores[idx] = float(item.get("relevance_score", 0.0))
        return scores
    except Exception as exc:
        logger.warning("Real reranker call failed (%s); keeping RRF order instead", exc)
        return [float(len(candidates) - i) for i in range(len(candidates))]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def retrieve(
    conversation_id: str, query: str, top_k: int | None = None, file_id: str | None = None
) -> list[dict[str, Any]]:
    """Retrieve context chunks for `query` within `conversation_id`'s
    partition: dense search + BM25 search -> RRF fusion -> rerank -> top_k.

    `file_id`, when given, scopes both searches to that single document --
    used by the "multi" route's judge-triggered replan (see
    judge_multi_coverage) to force-pull content from one specific document
    rather than relying on decompose_query's sub-questions to have covered
    it.

    Returns [] (never raises) if there's no data yet for this conversation
    (same convention the dense-only version had) -- callers render that as
    a friendly "no context found" answer, not an error.
    """
    final_top_k = top_k or settings.TOP_K_RERANK

    query_vector = embed_query(query)
    dense_hits = _search_dense(conversation_id, query_vector, settings.TOP_K_DENSE, file_id_filter=file_id)
    bm25_hits = _search_bm25(conversation_id, query, settings.TOP_K_DENSE, file_id_filter=file_id)

    if not dense_hits and not bm25_hits:
        return []

    fused = _rrf_fuse(dense_hits, bm25_hits, top_n=settings.RRF_TOP_N, k=settings.RRF_K)
    return _rerank(query, fused, top_k=final_top_k)


# ---------------------------------------------------------------------------
# Judge (thinking_level == "deep" only, "multi" route)
# ---------------------------------------------------------------------------
#
# decompose_query's sub-questions are independent (fan-out, not a dependent
# chain), so the failure mode worth judging here is different from
# structured's "code ran but the number looks wrong": it's "the decomposition
# or retrieval happened to pull chunks from only one of the documents being
# compared" (e.g. two sub-questions that both end up closest, in embedding
# space, to the same document). This is a purely mechanical check -- no LLM
# call needed even in real mode, unlike structured/summarize's judges --
# because "which file_ids are actually represented in the merged chunk set"
# is just a set comparison, not a semantic question.


def judge_multi_coverage(
    embedding_files: list[dict[str, Any]], merged_chunks: list[dict[str, Any]]
) -> dict[str, object]:
    all_ids = {str(f["id"]) for f in embedding_files}
    covered_ids = {str(c["file_id"]) for c in merged_chunks if c.get("file_id") is not None}
    missing = all_ids - covered_ids

    # decompose_query caps out at 3 sub-questions (see its docstring), so
    # full coverage is a realistic bar only up to that many documents; past
    # that, just require more than one document actually represented.
    if len(all_ids) <= 3:
        satisfactory = not missing
    else:
        satisfactory = len(covered_ids) >= 2
        missing = set() if satisfactory else missing

    reason = (
        "ההשוואה מכסה את כל המסמכים הרלוונטיים"
        if satisfactory
        else f"לא נשלף תוכן עבור {len(missing)} מתוך {len(all_ids)} מסמכים שאמורים היו להיכלל בהשוואה"
    )
    return {"satisfactory": satisfactory, "reason": reason, "missing_file_ids": sorted(missing)}
