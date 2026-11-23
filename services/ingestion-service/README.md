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
`index_status` progresses through real sub-stages as the task runs
(`pending → parsing → chunking → embedding → indexing` for pdf/docx/image,
`pending → parsing → writing` for xlsx/csv/json) so the client can show
actual progress rather than a single opaque "processing" spinner — only
`ready`/`failed` are ever branched on elsewhere in the system, the
in-progress vocabulary is free to keep growing.

## Chunking (pdf/docx/image path)

- **PDF**: per-page text extraction (`pypdf` → `pdfplumber` fallback), OCR
  via `pytesseract` for pages with **no extractable text at all** (a
  whole-page scan). `source_ref` = `"עמוד N"`. On top of that, every page
  where real text *was* extracted (not the OCR fallback) also gets its
  embedded images captioned — see "Embedded-image captioning" below.
- **DOCX**: heading-aware — paragraphs are grouped into sections by Word's
  `Heading N` styles so a chunk never straddles a heading boundary.
  `source_ref` = `"סעיף: <heading>"` or `"פסקה N"` if there's no heading.
- **Image**: OCR only, and **only for `tif`/`tiff`** (`pytesseract`,
  `lang="heb+eng"`). `source_ref` = `"תמונה"`. Any other image extension
  (`jpg`/`jpeg`/`png`/`bmp`/`gif`/`webp`) is treated as "not a scan" and
  produces zero chunks without attempting OCR at all — see "TIFF-only OCR"
  below. In practice the client no longer routes these to `/ingest` in the
  first place (they go through the chat-image-attachment/vision flow
  instead, see `retrieval-orchestrator/README.md`); this is a defensive
  no-op for anything that reaches the endpoint directly anyway.
- All of the above share `_chunk_text()`: ~500 words per chunk, 50-word
  overlap, snapped to paragraph boundaries where possible.
- Any of the above is allowed to produce **zero chunks** (e.g. tesseract not
  installed, a scanned page with no usable OCR output, or a non-scan image)
  — the file still gets marked `ready` with `chunks: 0`, never `failed`, and
  a warning is logged. `parse_file()` raises `NotImplementedError` for any
  file_type not in `{pdf, docx, image-family}`.

## TIFF-only OCR (`app/parsing.py: _OCR_SCAN_IMAGE_EXTENSIONS`)

OCR is now only attempted for `tif`/`tiff`, not every image extension.
Rationale: TIFF is overwhelmingly a scanner/fax/archival output format,
while a camera photo or a screenshot (png/jpg/gif/bmp/webp) almost never has
meaningful document text to recover — running OCR on those was pure wasted
work that always (or near-always) produced zero usable chunks anyway. Those
other formats are meant for genuine chat-image attachment instead (see
`retrieval-orchestrator/README.md`'s "`images`" section) — a fundamentally
different, synchronous, non-indexed flow.

## Embedded-image captioning (`app/vision.py`, `app/parsing.py: _caption_embedded_images`)

A photo/chart/diagram embedded **alongside real running text** on a PDF page
is otherwise completely invisible to RAG — the per-page OCR fallback above
only ever triggers when a page has *no* text layer at all (a whole-page
scan), which is a different situation entirely. For every page where real
text was extracted (via pypdf or pdfplumber, not OCR):

1. `page.images` (pypdf) enumerates the embedded images on that page.
2. Anything smaller than `_MIN_EMBEDDED_IMAGE_PX` (80px) in **both**
   dimensions is skipped — almost always a logo/bullet-icon/decorative
   marker, not worth a vision-model call.
3. Up to `_MAX_EMBEDDED_IMAGES_PER_PAGE` (5) remaining images per page are
   sent to `app/vision.py: caption_image()` — a short text description via
   `VLLM_VISION_URL`/`VLLM_VISION_MODEL` (OpenAI-style multimodal
   `/chat/completions`, same endpoint shape retrieval-orchestrator uses for
   chat-image attachments, but a separate call site/deployment-agnostic
   client). This is the first real use of `VLLM_VISION_URL` in this
   service — previously an unused placeholder.
4. Each caption becomes its own `Chunk` with a distinct
   `source_ref = "עמוד N (תמונה מוטבעת)"`, so a citation pointing at a
   described image reads clearly as such rather than looking like it came
   from the page's running text.

Best-effort throughout, same policy as OCR: a page pypdf can't read images
from, or a vision-model call that fails, just means fewer/no captions for
that page — never a failed ingestion. Mock mode (`caption_image`) returns a
deterministic `"[תיאור מדומה של תמונה מוטבעת, N bytes]"` placeholder, no
network call.

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
