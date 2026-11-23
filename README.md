# Enterprise RAG Chat (Air-Gapped)

A local, air-gapped chat system that lets users upload large files
(PDF/DOCX/XLSX/CSV/JSON/images) into a conversation and ask questions about
them — exact/structured queries, semantic search, summarization, and
cross-document comparison — without being limited by the LLM's context
window. Built around advanced RAG: hybrid (dense+BM25) retrieval with
reranking, 5-way query routing, and a dedicated NL→pandas path for tabular
data instead of forcing it through semantic search.

`/shared/CONTRACTS.md` is the single source of truth for every API contract,
the MSSQL/Milvus schema, env vars, and the mock-mode convention — read it
first. Each service folder below has its own README with implementation
specifics; this file is just the map.

## Components

| Folder | What it is |
|---|---|
| [`client/`](client/README.md) | Angular SPA (the chat UI) |
| [`gateway/`](gateway/README.md) | NestJS BFF — auth, conversation/message persistence, proxies file upload and SSE streaming to the Python services |
| [`services/ingestion-service/`](services/ingestion-service/README.md) | Parses every file type, chunks+embeds into Milvus (pdf/docx/image) or writes a parquet side-store (xlsx/csv/json) |
| [`services/retrieval-orchestrator/`](services/retrieval-orchestrator/README.md) | Answers `/query` — 5-way routing, hybrid search + reranking, SSE streaming |
| [`services/structured-data-agent/`](services/structured-data-agent/README.md) | NL→pandas over the xlsx/csv/json side-store, sandboxed |
| [`services/summarization-service/`](services/summarization-service/README.md) | Hierarchical map-reduce summarization |
| [`infra/helm/chat-system/`](infra/helm/chat-system/README.md) | Kubernetes deployment for all of the above, plus MSSQL/Redis/Milvus/NFS and the 4 extra vLLM models |
| `infra/docker/` | A `*.Dockerfile` per service (gateway, client, and the 4 Python services) + the MSSQL init SQL |
| `data/nfs-raw-files/` | Local stand-in for the shared NFS/PVC mount (raw uploads + structured side-stores) |

## The 5 query routes, in plain terms

Every question sent to `POST /query` is classified (`classify_query` in
`services/retrieval-orchestrator/app/retrieval.py`) into one of five
processing paths before anything else happens. Each can optionally run with
`thinking_level: "deep"` (user-selectable in the client), which asks the
model to reason harder about its answer — three of the five additionally
run a bounded **judge + one-shot replan** when `"deep"` is set: a cheap
check for "did this actually work", and if not, exactly one corrective
attempt (never an open-ended retry loop).

### 1. `general` — plain conversation, no files uploaded yet
**Purpose**: behave like a normal chat assistant when the conversation has
no files at all — nothing to retrieve, summarize, or compute on.
- **Standard**: send the question (+ history) straight to the model, with
  none of the "no relevant context was found" framing the other routes use
  (that framing could otherwise bias the model into refusing to just chat).
- **Deep**: same flow, model is just asked to think harder. No judge —
  plain conversation has no ground truth to check an answer against.

### 2. `factual` — a question about the uploaded documents' content
**Purpose**: answer from what's actually written in the PDFs/DOCX/images.
- **Standard**: hybrid search (dense + BM25 in parallel) → merge the two
  ranked lists (Reciprocal Rank Fusion) → rerank for real relevance → feed
  the top chunks to the model, which answers with citations.
- **Deep**: same retrieval, model just reasons harder over the same
  evidence. No judge on this route either.

### 3. `structured` — a precise question over a table (XLSX/CSV/JSON)
**Purpose**: exact answers ("what's the sum/average of X") by running real
code against the data, not guessing semantically.
- **Standard**: the model plans a short sequence of pandas steps (usually
  one, sometimes 2–3 for a dependent calculation like "compute a ratio,
  then find the max"), each step runs in a sandbox, and a step that fails
  gets one targeted fix-attempt before giving up.
- **Deep**: one extra step at the end — a judge checks whether the result
  is actually sound (e.g. a silent `NaN`/`Inf` from a zero-denominator
  division that "succeeded" without raising). If not, one full replan is
  attempted; the original result is kept if that doesn't help.

### 4. `summarize` — "summarize this document"
**Purpose**: summarize a document that may be larger than the model's
context window — generally, or centered on a specific focus if asked.
- **Standard**: pick which file to summarize (explicit filename → inferred
  by relevance → most recently uploaded, in that order), then map (summarize
  chunks in batches) → reduce (merge those summaries, recursively, into one).
- **Deep**: a judge checks the result isn't empty/degenerate, and — if a
  specific focus was requested — whether it's visibly addressed. If not,
  one re-run with the judge's complaint folded into the instructions.

### 5. `multi` — comparing two or more documents
**Purpose**: answer comparison questions ("what's the difference between
the travel policy and the expense policy") when several documents exist.
- **Standard**: split the question into 2–3 sub-questions (one per
  document/aspect), run hybrid search for each independently, merge the
  results (no duplicates), generate one answer over the combined context.
- **Deep**: a judge does a purely mechanical check — are all the documents
  that should be compared actually represented in the merged results, or
  did two sub-questions accidentally land on the same document? If
  something's missing, that document is fetched directly (bypassing the
  decomposition) and merged in before generating the answer.

## Beyond routing: system prompts, image chat, live progress, richer documents

A few capabilities layered on top of the 5 routes above, all documented in
full in `shared/CONTRACTS.md`:

- **Per-conversation system prompt** — an optional custom instruction set via
  a "⚙️ settings" panel in the client, stored in `conversations.system_prompt`,
  and forwarded on every question in that conversation to whichever route
  actually generates the answer (a real system-role message for
  general/factual/multi; folded into the prompt-builder for structured/
  summarize, which have no system-role concept of their own).
- **Image chat attachments** — a photo attached to a message in a
  conversation with no files yet is sent straight to a dedicated vision model
  (`VLLM_VISION_URL`) for genuine multimodal Q&A ("what's in this picture?").
  This is unrelated to document OCR (below): it's synchronous, ephemeral, and
  never indexed.
- **Live status + reasoning panel** — the client shows a progress line while
  a question is being answered (`status` SSE events: "searching...",
  "planning a query over the data...", ...) and, once answered, an
  expandable "🧠" panel showing the route's actual work — the pandas plan's
  steps, the sub-questions a comparison was split into, or which file got
  summarized — plus a judge's verdict when `thinking_level="deep"` ran one.
- **One attach button, routed by extension** — `pdf`/`docx`/`xlsx`/`csv`/
  `json`/`tif`/`tiff` go through ingestion/RAG; any other image goes through
  the chat-attachment flow above instead. TIFF is treated as a document
  because it's almost always scanner/fax output, unlike a camera photo.
- **Embedded-image captioning in PDFs** — a photo/chart/diagram embedded
  alongside real text on a PDF page (not a whole-page scan, which OCR
  already handles) is captioned by the same vision model and indexed as its
  own citable chunk — otherwise that visual content would be completely
  invisible to RAG. Tiny images (likely logos/icons) are skipped.

## Local dev (no GPU required)

Every LLM call (embeddings, generation, reranking, routing) goes through a
`MOCK_LLM` toggle — `true` gives deterministic fake output so the whole
pipeline can be exercised end-to-end without vLLM or a GPU; `false` points
at real OpenAI-compatible vLLM endpoints (see `.env.example`).

```bash
docker compose up -d mssql           # wait for it to report healthy...
docker compose up mssql-init         # one-shot: creates chatdb + schema (idempotent)
docker compose up -d                 # MSSQL, Redis, Milvus (+ etcd/minio-compatible storage)

# each service, in its own venv/node_modules:
services/ingestion-service:        uvicorn app.main:app --port 8000   +   celery -A app.tasks worker
services/retrieval-orchestrator:   uvicorn app.main:app --port 8001
services/structured-data-agent:    uvicorn app.main:app --port 8002
services/summarization-service:    uvicorn app.main:app --port 8003   +   celery -A app.tasks worker
gateway:                           npm run start:dev                  # :3000
client:                            npm start                          # :4200
```

Or build/run any service as a container via its `infra/docker/*.Dockerfile`
(build context is the repo root, e.g. `docker build -f
infra/docker/gateway.Dockerfile -t chat-gateway .`).

**Important for local dev**: export one consistent **absolute**
`NFS_ROOT_PATH` (its default is a relative path) to every Python service
that touches files — see `ingestion-service/README.md`'s "known environment
gotcha" for what goes wrong otherwise. In Kubernetes this is a non-issue
(shared PVC, same mount path everywhere).

## Status

All 6 file types, all 5 query routes (general/factual/structured/summarize/
multi, with `thinking_level` + judge+replan on the three that benefit from
it), and the full upload→index→ask→stream flow have been exercised
end-to-end through the actual browser UI, not just unit-tested in
isolation. The relational store was migrated from a local Postgres to
**MSSQL** (to match an existing enterprise SQL Server in production — see
`shared/CONTRACTS.md`), verified against a live container. The Helm chart
renders clean (`helm lint --strict`) for default/dev/prod value sets. Open
items before a real deployment (GPU allocation mechanism, retention policy
duration, NFS provisioner choice, image-registry approval process) are
tracked in the project plan and in `infra/helm/chat-system/README.md`.
