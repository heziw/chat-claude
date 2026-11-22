# summarization-service

FastAPI service that answers "summarize this document" with **hierarchical
map-reduce** instead of stuffing the whole document into one prompt — the
point of the whole exercise is that a document can be larger than any
context window. See `/shared/CONTRACTS.md` for the full system contract.
Listens on `:8003`.

## Where the text comes from

This service never re-parses the raw file. It reads the chunks
ingestion-service already wrote to Milvus for that `file_id`
(`app/milvus_reader.py: fetch_chunks_for_file`), in true document order —
`chunk_id` is `"<file_id>_<chunk_index>"`, and since `file_id` is a dashed
UUID, the numeric suffix is extracted by splitting on the *last* underscore
(a plain lexicographic sort of `chunk_id` would put chunk "...10" before
chunk "...2" once a document has 10+ chunks). The partition/`file_id` to
look under comes from Postgres `file_metadata.milvus_partition`. If the file
has no chunks yet (still indexing, or structured data with nothing in
Milvus), the task fails cleanly and `GET /summarize/{task_id}` reports it.

## Map-reduce (`app/summarizer.py`)

1. **Map**: chunks are grouped (`MAP_GROUP_SIZE=6` chunks or
   `MAP_GROUP_MAX_CHARS=6000` chars per group, whichever hits first), each
   group summarized independently (`summarize_text`, same `MOCK_LLM`
   contract as every other service — mock: template sentence that stays
   shorter than its input, important so the recursion below actually
   converges; real: `VLLM_ROUTER_URL`, `llama-3.1-8b` — also the designated
   router model, reused here for its cheap-and-fast profile). If the
   request's `question` carries a specific focus rather than a bare
   "summarize this" (`_focus_text`), the map instruction is built to center
   on it instead of producing a generic summary.
2. **Reduce**: if more than one map-summary came out, they're grouped again
   (`REDUCE_GROUP_SIZE=5` / `REDUCE_GROUP_MAX_CHARS=6000`) and merged,
   recursively, until one summary remains — capped at `REDUCE_MAX_DEPTH=3`
   levels, at which point everything left is force-merged in a single call
   regardless of size, so a pathological input can't recurse forever.

## `thinking_level` and judge+replan (`"deep"` only)

`thinking_level` (`"fast"|"standard"|"deep"`, user-selectable in the client,
forwarded from retrieval-orchestrator) does two things — see
`shared/CONTRACTS.md` "thinking_level" for the full picture across all
services:

1. Forwarded as `reasoning_effort` on every real-mode map/reduce/judge call.
2. `"deep"` only: after a summary is produced, **`judge_summary`**
   (`app/summarizer.py`) checks it. Unlike structured-data-agent's judge
   (which reacts to a *numerically* wrong result), there's no exception
   equivalent here — `summarize_text` always returns *some* string — so the
   judge instead checks for: (a) a degenerate/empty final summary (e.g. a
   file whose chunks all came back with blank text after filtering — the one
   failure mode genuinely reachable in mock mode), and (b) when the question
   named a specific focus, whether the summary visibly addresses it (mock:
   keyword presence — meaningfully testable mostly in real mode, since the
   mock summarizer embeds its own instruction text verbatim and will
   trivially "pass" its own focus back to itself; documented as a known mock
   blind spot in `_mock_judge`'s docstring rather than silently glossed
   over). On an unsatisfactory verdict, `app/tasks.py` re-runs the full
   map-reduce **exactly once** with the judge's reason folded into the map
   instruction (`hierarchical_summarize(..., extra_context=...)`) — one
   judge call, at most one full re-run, never an open loop. The original
   summary is kept if the re-run doesn't produce anything better.

## Async execution

`POST /summarize` returns `{task_id}` immediately (202); the actual
map-reduce (+ judge/replan, when applicable) runs in a Celery task
(`app/tasks.py`, `REDIS_URL` broker+backend, default queue `summarization` —
see "Queue name" below). `GET /summarize/{task_id}` maps Celery's own state
machine to `pending`/`processing`/`ready`/`failed` and returns `{status,
summary?, judge?}` — `judge` only appears when `thinking_level=="deep"` was
requested, for transparency/debugging; retrieval-orchestrator only reads
`status`/`summary`, so this is purely additive.

## Queue name

Shares one `REDIS_URL` with ingestion-service. Celery's default queue name
is `"celery"` for every app unless overridden — running both services'
workers concurrently without doing so causes one to steal the other's tasks
(`NotRegistered` errors). This service sets
`task_default_queue="summarization"`; ingestion-service sets `"ingestion"`.

## Running

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8003
.venv/bin/celery -A app.tasks worker --loglevel=info
```

Requires Postgres, Redis, and Milvus reachable, and at least one file
already `ready` (ingested) in some conversation to summarize.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/summarize` | `{file_id, thinking_level?, question?}` → `{task_id}` (202) |
| GET | `/summarize/{task_id}` | `{status, summary?, judge?}` |
| GET | `/health` | liveness probe |

## Dependency pins worth knowing about

Same `pymilvus==2.4.9` pins as ingestion-service (`setuptools<81`,
`marshmallow==3.23.1`) and the same `redis>=6.1.0` requirement for
Celery/kombu.
