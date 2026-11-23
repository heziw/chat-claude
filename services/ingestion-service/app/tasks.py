"""Celery app + ingestion tasks.

Broker and result backend are both REDIS_URL per CONTRACTS.md. We also use
a plain Redis key (separate from Celery's own result keys) to remember the
task_id -> file_id mapping, so GET /ingest/{task_id}/status can look up the
file_metadata row for a given task_id.

Two pipelines, selected by file_type (see CONTRACTS.md):
  - pdf | docx | image  -> parse_file() -> chunk -> embed -> Milvus
  - xlsx | csv | json   -> parse_structured() -> DataFrame -> parquet
                            side-store (no embedding, no Milvus at all)
"""
from __future__ import annotations

import logging
from pathlib import Path

import redis
from celery import Celery

from app import db, milvus_client
from app.config import settings
from app.embeddings import embed_texts
from app.parsing import parse_file, parse_structured

logger = logging.getLogger(__name__)

STRUCTURED_FILE_TYPES = {"xlsx", "csv", "json"}

celery_app = Celery(
    "ingestion_service",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    # Every service on this REDIS_URL shares one Redis instance/db (see
    # CONTRACTS.md). Celery's default queue name ("celery") is the same for
    # every app unless overridden, so two services' workers would otherwise
    # steal each other's tasks when both run concurrently against the same
    # broker (observed in practice against summarization-service). Give each
    # service its own default queue.
    task_default_queue="ingestion",
)

_redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
_TASK_FILE_KEY_PREFIX = "ingest:task_to_file:"
_TASK_FILE_TTL_SECONDS = 7 * 24 * 3600  # 1 week, plenty for a status check window


def remember_task_file(task_id: str, file_id: str) -> None:
    _redis_client.set(f"{_TASK_FILE_KEY_PREFIX}{task_id}", file_id, ex=_TASK_FILE_TTL_SECONDS)


def lookup_file_id_for_task(task_id: str) -> str | None:
    return _redis_client.get(f"{_TASK_FILE_KEY_PREFIX}{task_id}")


def _sanitize_for_filename(name: str) -> str:
    """Make a sheet name safe to embed in a filename (no slashes, spaces
    collapsed, etc). Best-effort - this is purely for on-disk naming, the
    original sheet name is preserved in the metadata json.
    """
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in str(name))
    return safe or "sheet"


def _write_structured_side_store(
    conversation_id: str, file_id: str, sheets: dict
) -> str:
    """Write one or more pandas DataFrames (keyed by sheet name) to parquet
    under NFS_ROOT_PATH/<conversation_id>/structured/, and return the path
    to record in file_metadata.structured_store_ref.

    - Single sheet (csv/json always, xlsx with one sheet): one parquet file
      named "<file_id>.parquet"; structured_store_ref points directly at it.
    - Multiple sheets (xlsx with several sheets): one parquet file per
      sheet, named "<file_id>__<sanitized_sheet_name>.parquet", plus a
      small "<file_id>.meta.json" listing every sheet's original name and
      its parquet path; structured_store_ref points at the meta.json so
      downstream readers (structured-data-agent) know where to look for
      each sheet.
    """
    structured_dir = Path(settings.NFS_ROOT_PATH) / conversation_id / "structured"
    structured_dir.mkdir(parents=True, exist_ok=True)

    sheet_names = list(sheets.keys())

    if len(sheet_names) == 1:
        only_name = sheet_names[0]
        out_path = structured_dir / f"{file_id}.parquet"
        sheets[only_name].to_parquet(out_path, index=False)
        return str(out_path)

    files_by_sheet = {}
    for sheet_name, df in sheets.items():
        safe_name = _sanitize_for_filename(sheet_name)
        out_path = structured_dir / f"{file_id}__{safe_name}.parquet"
        df.to_parquet(out_path, index=False)
        files_by_sheet[sheet_name] = str(out_path)

    meta_path = structured_dir / f"{file_id}.meta.json"
    import json

    meta_path.write_text(
        json.dumps({"file_id": file_id, "sheets": sheet_names, "files": files_by_sheet}, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(meta_path)


@celery_app.task(name="ingestion.process_file", bind=True)
def process_file_task(self, file_id: str, nfs_path: str, conversation_id: str, file_type: str):
    """Full pipeline for one file.

    Dispatches on file_type into one of two pipelines (see CONTRACTS.md):
      - xlsx/csv/json: parse_structured() -> DataFrame(s) -> parquet
        side-store. No embedding, no Milvus at all.
      - pdf/docx/image (everything else): parse_file() -> chunk -> embed ->
        write to Milvus, same as the existing pdf-only pipeline.
    """
    file_type_norm = (file_type or "").lower().lstrip(".")

    if file_type_norm in STRUCTURED_FILE_TYPES:
        return _process_structured_file(self, file_id, nfs_path, conversation_id, file_type_norm)
    return _process_embedding_file(self, file_id, nfs_path, conversation_id, file_type_norm)


def _process_embedding_file(self, file_id: str, nfs_path: str, conversation_id: str, file_type: str):
    """pdf/docx/image pipeline: parse -> chunk -> embed -> write to Milvus.

    Reports its real sub-stages through file_metadata.index_status as it
    moves through them ("parsing" -> "chunking" -> "embedding" ->
    "indexing" -> "ready"/"failed") so the client's upload badge can show
    more than a single opaque "indexing" the whole time -- see
    CONTRACTS.md's index_status enum and client/README.md. These are real
    sequential stages this function already goes through, not invented
    granularity: `index_status == "ready"` is still the only state anything
    else in the system (routing, judges, etc.) checks for, so widening the
    in-progress vocabulary here can't affect that logic.
    """
    try:
        db.update_index_status(file_id, "parsing")
        chunks = parse_file(file_type, nfs_path)

        if not chunks:
            logger.warning(
                "No extractable text/chunks for file_id=%s (nfs_path=%s, "
                "file_type=%s); marking ready with zero chunks",
                file_id,
                nfs_path,
                file_type,
            )
            db.update_index_status(file_id, "ready", milvus_partition=None)
            return {"file_id": file_id, "status": "ready", "chunks": 0}

        db.update_index_status(file_id, "chunking")
        texts = [c.text for c in chunks]

        db.update_index_status(file_id, "embedding")
        vectors = embed_texts(texts, is_query=False)

        chunk_ids = [f"{file_id}_{c.chunk_index}" for c in chunks]
        source_refs = [c.source_ref for c in chunks]

        db.update_index_status(file_id, "indexing")
        partition_name = milvus_client.insert_chunks(
            conversation_id=conversation_id,
            file_id=file_id,
            file_type=file_type,
            chunk_ids=chunk_ids,
            source_refs=source_refs,
            texts=texts,
            dense_vectors=vectors,
        )

        db.update_index_status(file_id, "ready", milvus_partition=partition_name)
        return {"file_id": file_id, "status": "ready", "chunks": len(chunks)}

    except Exception as exc:
        logger.exception("process_file_task (embedding) failed for file_id=%s", file_id)
        try:
            db.update_index_status(file_id, "failed", error_message=str(exc))
        except Exception:
            logger.exception("Failed to persist failure status for file_id=%s", file_id)
        raise


def _process_structured_file(self, file_id: str, nfs_path: str, conversation_id: str, file_type: str):
    """xlsx/csv/json pipeline: parse -> DataFrame(s) -> parquet side-store.
    Never touches embeddings or Milvus. Reports "parsing" -> "writing" ->
    "ready"/"failed" through file_metadata.index_status, same rationale as
    the embedding pipeline above.
    """
    try:
        db.update_index_status(file_id, "parsing")
        sheets = parse_structured(file_type, nfs_path)

        if not sheets:
            logger.warning(
                "No sheets/tables parsed for file_id=%s (nfs_path=%s, "
                "file_type=%s); marking ready with zero sheets",
                file_id,
                nfs_path,
                file_type,
            )
            db.update_index_status(file_id, "ready", structured_store_ref=None)
            return {"file_id": file_id, "status": "ready", "sheets": 0}

        db.update_index_status(file_id, "writing")
        store_ref = _write_structured_side_store(conversation_id, file_id, sheets)

        db.update_index_status(file_id, "ready", structured_store_ref=store_ref)
        return {"file_id": file_id, "status": "ready", "sheets": len(sheets), "structured_store_ref": store_ref}

    except Exception as exc:
        logger.exception("process_file_task (structured) failed for file_id=%s", file_id)
        try:
            db.update_index_status(file_id, "failed", error_message=str(exc))
        except Exception:
            logger.exception("Failed to persist failure status for file_id=%s", file_id)
        raise
