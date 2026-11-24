# retrieval-orchestrator

FastAPI service that answers `POST /query`: routes the question, retrieves
context, and streams back an answer over SSE. This is the service that
implements the "advanced RAG" architecture (hybrid search, reranking, 5-way
query routing) rather than naive single-shot vector search. See
`/shared/CONTRACTS.md` for the full system contract. Listens on `:8001`.

## 5-way query routing (`app/retrieval.py: classify_query`)

Every question is classified into one of five routes before any retrieval
happens, in this precedence order:

0. **general** — hard override, checked *before* any LLM/heuristic
   classification runs: the conversation has no files at all (neither
   structured nor embedding-indexed). None of the other four routes make
   sense with nothing to retrieve/summarize/compute on, so this
   short-circuits immediately (also saves a pointless router-LLM call in
   real mode). Plain conversational generation, no retrieval, no citations
   — see `_handle_general` below for why this needed its own route rather
   than just falling through to "factual" with an empty context.
1. **summarize** — summary keywords present (`סכם`/`תמצת`/`summary`/...).
2. **multi** — comparison keywords present (`השווה`/`compare`/`בין...ל`/...)
   **and** more than one embedding-indexed (pdf/docx/image) file exists in
   the conversation.
3. **structured** — exactly one `ready` xlsx/csv/json file exists in the
   conversation (unconditional once that's true — by construction there's
   nothing else a structured question could be about).
4. **factual** — the default once "general" is ruled out. An empty
   retrieval result here means files exist but none matched/indexed yet,
   not that none were ever uploaded.

In real mode (`MOCK_LLM=false`) a router-LLM call (`VLLM_ROUTER_URL`) picks
the category first, among rules 1-4 only; its answer only overrides the
heuristic above if it parses cleanly to one of those four valid categories,
otherwise the heuristic decides. Each route has its own SSE handler in
`app/main.py` (`_handle_general` / `_handle_factual` / `_handle_structured`
/ `_handle_summarize` / `_handle_multi`), but all five speak the same
`token`/`citation`/`done` wire format — "general" just never emits a
`citation` event (there's no document to cite). structured/summarize/multi
additionally emit `status` (plain-text progress labels, e.g. "מתכנן ומריץ
שאילתה על הנתונים...") and one `reasoning` event each (the pandas plan's
steps / which file got summarized / the sub-questions a comparison was split
into, plus a judge verdict when `thinking_level="deep"` ran one) — ephemeral,
not persisted in MSSQL, purely for the client's live-progress UI. See
CONTRACTS.md's `status`/`reasoning` event docs for the full payload shapes.

## `system_prompt` and `images` (not part of routing itself)

- **`system_prompt`** — the conversation's custom instruction (set via the
  client's settings panel, stored in `conversations.system_prompt`, fetched
  by the gateway and forwarded on every `/query` call). Threaded into
  *every* route that actually generates text: a real system-role message
  for general/factual/multi (`generation.py:generate_stream`), folded as an
  instruction line into the plan-generation prompt for structured
  (`structured-data-agent` has no system-role concept), and into the map
  instruction for summarize. `classify_query` itself never looks at it —
  routing is driven by `question` only.
- **`images`** — base64 data URLs attached to a chat message, only ever used
  by the **general** route (`_handle_general`). When present, real mode
  routes the call to `VLLM_VISION_URL`/`VLLM_VISION_MODEL` instead of
  `VLLM_GENERATION_URL`, as an OpenAI-style multimodal `user` message — a
  separate deployment since there's no guarantee the main generation model
  is itself multimodal. Unrelated to ingestion-service's OCR/embedded-image-
  captioning pipeline (different services, different purpose: this is
  synchronous single-message vision chat, not document indexing).

### Why "general" is its own route, not just "factual" with no context

The factual route's prompt, when retrieval finds nothing, explicitly says
*"no relevant context was found in this conversation"* — correct framing
when files exist but a specific question didn't match anything, but if
reused for a conversation with **zero files**, that phrasing can bias even
a capable model toward refusing to just chat ("I have no context..."
instead of answering a plain "how are you?"). `_handle_general` builds a
plain `history + question` prompt with none of that framing, and skips
retrieval entirely (there's no Milvus partition to query against before any
file has ever been uploaded). `thinking_level` still flows through to
`generate_stream` exactly as everywhere else — "deep" still asks the model
to reason harder — but there's no judge on this route: plain conversation
has no ground truth (a computed number, source chunks, file coverage) to
verify an answer against.

## Hybrid search + rerank (the "factual" route, `app/retrieval.py: retrieve()`)

`Milvus HNSW dense search` + `BM25 (client-side, rank_bm25)` → **Reciprocal
Rank Fusion** → **rerank** → top-k context for generation.

- **Why BM25 is client-side, not Milvus-native**: Milvus 2.4.9 (the version
  pinned in `docker-compose.yml`) has no native full-text/BM25 search — that
  only shipped in 2.5. Since the search scope is one conversation's
  partition (hundreds to low-thousands of chunks, not a global index),
  fetching the whole partition and scoring it with `BM25Okapi` in-process is
  cheap and avoids a Milvus version dependency / schema migration.
- **RRF**: standard `score(doc) = Σ 1/(k + rank)` over the dense and BM25
  rank lists (`RRF_K=60`, top `RRF_TOP_N=10` candidates survive the fusion).
- **Rerank**: mock mode scores by word-overlap between the question and each
  candidate (deterministic, exercises the same "rerank reorders the list"
  code path as a real cross-encoder); real mode POSTs
  `{VLLM_RERANK_URL}/rerank` with `{model, query, documents}` — **this
  endpoint shape is an assumption** (Cohere/Jina-style), documented in code,
  since vLLM's own `--task score` HTTP contract wasn't pinned down further
  than "exposes *a* scoring endpoint" at the time this was written; verify
  against your actual vLLM version before flipping `MOCK_LLM=false`. On any
  failure it falls back to the RRF order rather than failing the request.
  Final `TOP_K_RERANK=5` chunks go into the generation prompt.

## The other three routes

- **structured** (`app/structured_client.py`) — calls
  `structured-data-agent POST /structured-query` with the conversation's
  single structured file_id, streams back its `answer` word-by-word, cites
  `source_ref="נתון מובנה"`.
- **summarize** (`app/summarization_client.py`) — calls
  `summarization-service POST /summarize` for whichever embedding-indexed
  file the question is actually about (`_select_summarize_target`, below),
  then polls `GET /summarize/{task_id}` (`SUMMARIZE_POLL_INTERVAL_SECONDS` /
  `_TIMEOUT_SECONDS`) until `ready`/`failed`/timeout, streams the summary.

**Which file does "summarize this" target, with more than one file in the
conversation?** (`_select_summarize_target` in `app/main.py`) Precedence:
1. The question names exactly one file by filename — unambiguous, wins.
2. The question has a specific focus beyond a bare "summarize this"
   (`_has_specific_focus`) — probe with the same hybrid search the factual
   route uses (`retrieve(query=question, top_k=TOP_K_DENSE)`) and pick
   whichever file's chunks dominate the top results. This exists because
   blindly picking "the most recently uploaded file" (the only rule before
   this) silently summarizes the *wrong* document whenever the focus
   actually belongs to an earlier upload — e.g. "summarize the hotel
   reimbursement rules" when a travel policy was uploaded before an expense
   policy that happens to be the most recent file. The `"deep"` judge (see
   below) checks a summary's *content* against the focus, but can't recover
   from having summarized the wrong file in the first place; this fixes the
   selection itself, upstream of the judge.
3. No signal either way (a bare "summarize this") — most recently uploaded,
   same default as before this fix.
- **multi** — decomposes the question into 2–3 sub-questions (mock: regex
  split on `לעומת`/`compared to`/`vs`/...; real: `VLLM_ROUTER_URL`), runs the
  full hybrid-search pipeline above for each sub-question independently,
  merges and de-duplicates the resulting chunks by `chunk_id`, then
  generates one answer over the unified context.

## `thinking_level` and judge+replan (`"deep"` only)

`thinking_level` (`"fast"|"standard"|"deep"`, user-selectable in the client)
does two independent things — see `shared/CONTRACTS.md` "thinking_level" for
the full picture across all services:

1. Forwarded as `reasoning_effort` on every real-mode LLM call this service
   makes (`generate_stream`, `decompose_query`) — a documented assumption
   about the field name a reasoning-capable model behind `VLLM_*_URL`
   expects.
2. `"deep"` only: **`judge_multi_coverage`** (`app/retrieval.py`) — the
   "multi" route's judge. Unlike structured-data-agent's judge (which reacts
   to a result that ran but looks numerically wrong), this one is a **purely
   mechanical check, no LLM call even in real mode**: `decompose_query`'s
   sub-questions are independent (fan-out, not a dependent chain), so it's
   possible for two of them to both end up closest, in embedding space, to
   the same document — the judge just checks whether every embedding file in
   the conversation is actually represented in the merged chunk set. If one
   is missing, `_handle_multi` retrieves directly against that specific
   `file_id` (`retrieve(..., file_id=...)`, which scopes both the dense and
   BM25 search with a Milvus `file_id ==` filter) using the *original*
   question, bypassing decomposition entirely for that document, and merges
   the result in. One judge check, one corrective retrieval pass — not an
   open loop. Not part of the SSE wire contract; logged for observability.

## Mock mode

`embed_query()` / `generate_stream()` / reranking all follow the repo-wide
`MOCK_LLM` contract (deterministic hash-based embeddings, template text
generation that echoes back the retrieved chunks, word-overlap reranking).
Flip to `MOCK_LLM=false` and point `VLLM_*_URL` at real OpenAI-compatible
vLLM endpoints — two separate client configs are kept apart on purpose: one
for the existing production models (`VLLM_GENERATION_URL`, e.g.
muse30b/gemma3) used for the final answer, one for the small support models
(`VLLM_EMBED_URL`, `VLLM_RERANK_URL`, `VLLM_ROUTER_URL`) — this service never
touches the existing models' deployment.

## Running

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
NFS_ROOT_PATH=/absolute/path/to/data/nfs-raw-files .venv/bin/uvicorn app.main:app --port 8001
```

Requires MSSQL and Milvus reachable; for the structured/summarize/multi
routes to do anything useful, `structured-data-agent` (:8002) and
`summarization-service` (:8003) should also be running — if they're not,
those routes fail gracefully (clear SSE `token` error message + `done`) 
rather than crashing the request.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/query` | `{conversation_id, question, history?, thinking_level?, system_prompt?, images?}` → `text/event-stream` (`token`/`citation`/`status`/`reasoning`/`done`) |
| GET | `/files/{file_id}/schema` | debug proxy to structured-data-agent's `/schema/{file_id}`; 502 with a clear message if that service is unreachable |
| GET | `/healthz` | liveness probe |

## Dependency pins worth knowing about

Same `pymilvus==2.5.18` (see ingestion-service's README for why it's no
longer `2.4.9`+pinned-`setuptools`/`marshmallow`), plus `rank_bm25` for the
BM25 stage.
