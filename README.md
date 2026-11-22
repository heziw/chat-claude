# Enterprise RAG Chat (Air-Gapped)

A local, air-gapped chat system that lets users upload large files
(PDF/DOCX/XLSX/CSV/JSON/images) into a conversation and ask questions about
them — exact/structured queries, semantic search, summarization, and
cross-document comparison — without being limited by the LLM's context
window. Built around advanced RAG: hybrid (dense+BM25) retrieval with
reranking, 4-way query routing, and a dedicated NL→pandas path for tabular
data instead of forcing it through semantic search.

`/shared/CONTRACTS.md` is the single source of truth for every API contract,
the Postgres/Milvus schema, env vars, and the mock-mode convention — read it
first. Each service folder below has its own README with implementation
specifics; this file is just the map.

## Components

| Folder | What it is |
|---|---|
| [`client/`](client/README.md) | Angular SPA (the chat UI) |
| [`gateway/`](gateway/README.md) | NestJS BFF — auth, conversation/message persistence, proxies file upload and SSE streaming to the Python services |
| [`services/ingestion-service/`](services/ingestion-service/README.md) | Parses every file type, chunks+embeds into Milvus (pdf/docx/image) or writes a parquet side-store (xlsx/csv/json) |
| [`services/retrieval-orchestrator/`](services/retrieval-orchestrator/README.md) | Answers `/query` — 4-way routing, hybrid search + reranking, SSE streaming |
| [`services/structured-data-agent/`](services/structured-data-agent/README.md) | NL→pandas over the xlsx/csv/json side-store, sandboxed |
| [`services/summarization-service/`](services/summarization-service/README.md) | Hierarchical map-reduce summarization |
| [`infra/helm/chat-system/`](infra/helm/chat-system/README.md) | Kubernetes deployment for all of the above, plus Postgres/Redis/Milvus/NFS and the 4 extra vLLM models |
| `infra/docker/` | Dockerfiles per service + the Postgres init SQL |
| `data/nfs-raw-files/` | Local stand-in for the shared NFS/PVC mount (raw uploads + structured side-stores) |

## Local dev (no GPU required)

Every LLM call (embeddings, generation, reranking, routing) goes through a
`MOCK_LLM` toggle — `true` gives deterministic fake output so the whole
pipeline can be exercised end-to-end without vLLM or a GPU; `false` points
at real OpenAI-compatible vLLM endpoints (see `.env.example`).

```bash
docker compose up -d                 # Postgres, Redis, Milvus (+ etcd/minio-compatible storage)

# each service, in its own venv/node_modules:
services/ingestion-service:        uvicorn app.main:app --port 8000   +   celery -A app.tasks worker
services/retrieval-orchestrator:   uvicorn app.main:app --port 8001
services/structured-data-agent:    uvicorn app.main:app --port 8002
services/summarization-service:    uvicorn app.main:app --port 8003   +   celery -A app.tasks worker
gateway:                           npm run start:dev                  # :3000
client:                            npm start                          # :4200
```

**Important for local dev**: export one consistent **absolute**
`NFS_ROOT_PATH` (its default is a relative path) to every Python service
that touches files — see `ingestion-service/README.md`'s "known environment
gotcha" for what goes wrong otherwise. In Kubernetes this is a non-issue
(shared PVC, same mount path everywhere).

## Status

All 6 file types, all 4 query routes (factual/structured/summarize/multi),
and the full upload→index→ask→stream flow have been exercised end-to-end
through the actual browser UI, not just unit-tested in isolation. The Helm
chart renders clean (`helm lint --strict`) for default/dev/prod value sets.
Open items before a real deployment (GPU allocation mechanism, retention
policy duration, NFS provisioner choice, image-registry approval process)
are tracked in the project plan and in `infra/helm/chat-system/README.md`.
