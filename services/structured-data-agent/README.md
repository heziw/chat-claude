# structured-data-agent

FastAPI service that answers precise/aggregate questions over tabular data
(xlsx/csv/json) by generating and running pandas code, instead of forcing
structured data through semantic search (which is bad at "what's the sum of
column X" questions). See `/shared/CONTRACTS.md` for the full system
contract. Listens on `:8002`.

## Why this exists separately from retrieval-orchestrator

ingestion-service never embeds xlsx/csv/json files or writes them to Milvus
— it writes them to a parquet side-store instead (see
`ingestion-service/README.md`). This service is the only thing that reads
that side-store. retrieval-orchestrator's "structured" route is just a thin
proxy to `POST /structured-query` here.

## Request flow (`POST /structured-query`) — plan, then execute step-by-step

A single aggregation question ("what's the sum of revenue") is just the
common case of a **1-step plan** — there's no separate "simple" code path
to keep in sync with the multi-step one. This matters for questions that
need a *sequence* of dependent operations (e.g. "which department has the
highest revenue-per-headcount ratio?" = compute a ratio column, *then*
pick the max row) — a single one-shot code generation call struggles with
that kind of question; a short explicit plan doesn't.

1. `app/data_store.py` loads the right DataFrame(s) from parquet, resolved
   via `file_metadata.structured_store_ref` in MSSQL (handles a single
   parquet file, a directory of per-sheet parquet files, or a JSON index —
   see ingestion-service's side-store format).
2. `app/llm.py: generate_plan()` turns the question into an ordered list of
   1-4 `{description, code}` steps, where later steps may reference
   variables/columns earlier steps created, and only the *last* step is
   required to assign the final answer to `result`.
   - **Mock mode**: the original keyword heuristic (סכום/sum, ממוצע/average,
     מקסימום/max, מינימום/min, כמה/count against the first numeric column)
     still handles the common single-step case unchanged. A question that
     mentions two numeric columns *and* a ratio/superlative phrasing ("X
     per Y", "highest ... ratio") gets a genuine 2-step plan instead
     (`app/llm.py: _mock_plan`).
   - **Real mode**: asks `VLLM_ROUTER_URL` for a JSON plan
     (`{"steps": [{"description", "code"}, ...]}`); falls back to the
     original single-step generator if the router is unreachable or
     returns something unparseable. If the request carries a `system_prompt`
     (the conversation's custom instruction, forwarded from
     retrieval-orchestrator — see CONTRACTS.md "system_prompt"), it's folded
     in as an extra instruction line in the plan prompt; mock mode ignores
     it (the keyword heuristic has no use for free-text instructions).
3. `app/sandbox.py: SandboxSession` executes the steps **in order, in one
   persistent namespace** — step 2 can see the column step 1 added. This is
   the piece that makes multi-step plans actually work: a plain sequence of
   independent `exec()` calls (one per step, each starting fresh) would
   lose step 1's work before step 2 could use it.
4. If a step raises (`SyntaxError`/`KeyError`/anything), **that step alone**
   gets one retry — `app/llm.py: regenerate_step()` feeds the error (and
   the already-succeeded prior steps' code, for context) back to the LLM
   for a corrected single step, which then runs against the *same* session
   (so steps that already succeeded are never redone or lost). If the retry
   also fails, the endpoint returns **422** with a clear message naming
   which step failed (never a bare 500). If every step runs without raising
   but none of them set `result` (a model forgetting the one hard
   requirement), that's treated the same way — one retry of the last step.
5. The final `result` is coerced to JSON (`.to_dict()`/`.to_json()` for a
   DataFrame/Series, passed through as-is for a scalar) and returned as
   `{answer, code_executed, result_table, steps}` — `steps` lists every
   step that actually ran (post-correction, if any), for transparency/
   debugging; `code_executed` is the same information flattened into one
   string. Existing consumers (retrieval-orchestrator) only read `answer`,
   so this is purely additive.

## Sandboxing — two layers, by design

- **App-level (this service, `app/sandbox.py`)**: `exec()` with a
  deliberately small builtins allow-list (`df`/`dfs`, `pd`, `len`, `sum`,
  `min`, `max`, `round`, `abs`, ... — **no** `__import__`, `open`, `exec`,
  `eval`, `os`, `sys`, network access), and a hard wall-clock timeout
  (`SANDBOX_TIMEOUT_SECONDS=5`, enforced by running the code in a daemon
  thread and giving up on it rather than blocking the request forever — the
  thread itself is an accepted leak on timeout, since the alternative is an
  unkillable request). This is **defense-in-depth only**, not the real
  isolation boundary.
- **OS-level (Kubernetes, not this codebase)**: the Helm chart runs this
  service's pod under a `gVisor`/`Kata` `runtimeClassName` (see
  `infra/helm/chat-system/templates/structured-data-agent/`). *That's* what
  actually contains a sandbox escape — the app-level restrictions exist so a
  buggy (not necessarily malicious) generated snippet fails fast and
  predictably even before the pod-level isolation would matter.

## Mock mode

Same `MOCK_LLM` contract as every other service — `true` uses the keyword
heuristics above (good enough to sanity-check the pipeline with real
arithmetic, e.g. "what's the sum of revenue" actually sums the revenue
column, and a genuine ratio question actually computes and compares the
ratio), `false` calls `VLLM_ROUTER_URL`.

## Why a hand-rolled plan loop, not LangChain/LangGraph

This *is* the one place in the system where a multi-step, error-correcting
chain actually exists (every other service's "chain" is a fixed, static
sequence of calls decided in code, not something an LLM re-plans at
runtime) — so it's the most defensible place to reach for an agent
framework. It was deliberately not used here anyway: the chain is still
bounded and small (1-4 steps, one retry per step, no open-ended looping or
dynamic tool selection), so a plain Python loop (`app/main.py`) over a
stateful sandbox (`SandboxSession`) covers it without a new dependency that
would need its own approval in the air-gapped image registry, and without
losing the direct, linear debuggability every other service in this repo
already has. If this ever grows into something that needs arbitrary
dynamic branching, resumable/checkpointed state, or human-in-the-loop
steps, that's the point where a graph-based framework's complexity starts
paying for itself — it doesn't yet.

## Running

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
NFS_ROOT_PATH=/absolute/path/to/data/nfs-raw-files .venv/bin/uvicorn app.main:app --port 8002
```

Requires MSSQL reachable, and `NFS_ROOT_PATH` pointed at the **same**
absolute directory ingestion-service is writing parquet files to (see the
"Known environment gotcha" note in `ingestion-service/README.md` — this is
the service that will 404 if the two disagree).

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/schema/{file_id}` | columns/dtypes/sample rows for a structured file |
| POST | `/structured-query` | `{file_id, question, thinking_level?, system_prompt?}` → `{answer, code_executed, result_table, steps, judge?}`; 404 if no side-store found, 422 if any plan step fails twice (original + 1 retry) |
| GET | `/health` | liveness probe |
