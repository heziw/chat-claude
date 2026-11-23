# ingestion-service

FastAPI service that owns everything that happens to a file between upload
and "ready to query": parsing, chunking, embedding, and writing to the right
store. See `/shared/CONTRACTS.md` for the full system contract. Listens on
`:8000`.

## Two pipelines, one entrypoint

Every file goes through `POST /ingest`, but `file_type` decides which of two
completely different pipelines runs (`app/tasks.py: process_file_task`):

- **pdf / docx / image** → `app/parsing.py: parse_file()` → chunk → embed
  (`app/embeddings.py`) → write to Milvus (`app/milvus_client.py`,
  collection `doc_chunks`, partition `conv_<conversation_id, no dashes>`).
  `file_metadata.milvus_partition` is set on success.
- **xlsx / csv / json** → `app/parsing.py: parse_structured()` → one
  `pandas.DataFrame` per sheet → parquet side-store under
  `NFS_ROOT_PATH/<conversation_id>/structured/` (single sheet:
  `<file_id>.parquet`; multiple sheets: one parquet per sheet plus a
  `<file_id>.meta.json` index). **No embedding, no Milvus at all** — these
  file types are meant to be queried by retrieval-orchestrator's
  "structured" route (NL→pandas in structured-data-agent), not by semantic
  search. `file_metadata.structured_store_ref` is set on success.

Both pipelines are dispatched asynchronously via Celery (`REDIS_URL` as
broker+backend, default queue `ingestion` — see "Queue name" below); `POST
/ingest` returns `{task_id, file_id}` immediately (202) after saving the raw
file to NFS and creating the `file_metadata` row with `index_status=pending`.

## Chunking (pdf/docx/image path)

- **PDF**: per-page text extraction (`pypdf` → `pdfplumber` fallback), OCR
  via `pytesseract` for pages with no extractable text. `source_ref` =
  `"עמוד N"`.
- **DOCX**: heading-aware — paragraphs are grouped into sections by Word's
  `Heading N` styles so a chunk never straddles a heading boundary.
  `source_ref` = `"סעיף: <heading>"` or `"פסקה N"` if there's no heading.
- **Image**: OCR only (`pytesseract`, `lang="heb+eng"`). `source_ref` =
  `"תמונה"`.
- All three share `_chunk_text()`: ~500 words per chunk, 50-word overlap,
  snapped to paragraph boundaries where possible.
- Any of the above is allowed to produce **zero chunks** (e.g. tesseract not
  installed, or a scanned page with no usable OCR output) — the file still
  gets marked `ready` with `chunks: 0`, never `failed`, and a warning is
  logged. `parse_file()` raises `NotImplementedError` for any file_type not
  in `{pdf, docx, image}`.

## Mock mode

`embed_texts()` (`app/embeddings.py`) follows the repo-wide `MOCK_LLM`
contract: `true` → a deterministic pseudo-random 1024-dim vector per text
(hash-seeded, for pipeline testing without a GPU); `false` → a real call to
`VLLM_EMBED_URL` (OpenAI-compatible `/v1/embeddings`, `intfloat/multilingual-e5-large`,
`query:`/`passage:` prefix per the model's convention).

## Queue name

Celery's default queue name is `"celery"` for every app unless overridden.
Since ingestion-service and summarization-service share one `REDIS_URL`
broker, running both workers at once caused one to steal the other's tasks
(`NotRegistered` errors) until each was given its own default queue
(`task_default_queue="ingestion"` here, `"summarization"` there). No `-Q`
flag needed at the CLI — the worker picks it up from `app/tasks.py`.

## Known environment gotcha (local dev only)

`NFS_ROOT_PATH` defaults to the **relative** path `./data/nfs-raw-files`.
If you start this service and `structured-data-agent` from different working
directories without an explicit absolute `NFS_ROOT_PATH`, they'll resolve to
different directories and structured-data-agent will 404 on a file this
service just wrote. In Kubernetes this can't happen (both mount the same PVC
at the same absolute path) — but locally, always export an absolute
`NFS_ROOT_PATH` shared by every service that touches files on disk.

## Running

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
NFS_ROOT_PATH=/absolute/path/to/data/nfs-raw-files .venv/bin/uvicorn app.main:app --port 8000
NFS_ROOT_PATH=/absolute/path/to/data/nfs-raw-files .venv/bin/celery -A app.tasks worker --loglevel=info
```

Requires MSSQL, Redis, and Milvus reachable (see repo-root
`docker-compose.yml`).

## Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/ingest` | multipart `file` + `conversation_id` → `{task_id, file_id}` (202) |
| GET | `/ingest/{task_id}/status` | `{status, file_id, error}` |
| DELETE | `/conversations/{conversation_id}/files` | drops the Milvus partition + NFS directory + `file_metadata` rows (manual delete and retention-sweeper both call this) |
| GET | `/health` | liveness probe |

## Dependency pins worth knowing about

`requirements.txt` pins `setuptools<81` and `marshmallow==3.23.1` —
`pymilvus==2.4.9` fails to import without them on a modern Python/pip
(`pkg_resources` removal, `marshmallow 4.x` incompatibility). `redis>=6.1.0`
is required by Celery/kombu's Redis transport; the older pin that ships with
some tutorials throws `VersionMismatch` at startup.
