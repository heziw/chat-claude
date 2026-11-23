"""ingestion-service FastAPI app.

Endpoints (see shared/CONTRACTS.md):
    POST   /ingest
    GET    /ingest/{task_id}/status
    DELETE /conversations/{conversation_id}/files
"""
from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from celery.result import AsyncResult
from fastapi import File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi_offline import FastAPIOffline

from app import db, milvus_client
from app.config import settings
from app.tasks import celery_app, lookup_file_id_for_task, process_file_task, remember_task_file

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPIOffline(title="ingestion-service", version="0.1.0")


def _file_type_from_name(filename: str) -> str:
    suffix = Path(filename).suffix.lower().lstrip(".")
    return suffix or "unknown"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/ingest", status_code=202)
async def ingest(file: UploadFile = File(...), conversation_id: str = Form(...)):
    """Save the uploaded file to NFS, create a file_metadata row
    (index_status='pending'), kick off the async Celery pipeline, and
    return immediately with {task_id, file_id}.
    """
    file_type = _file_type_from_name(file.filename or "")

    conv_dir = Path(settings.NFS_ROOT_PATH) / conversation_id
    conv_dir.mkdir(parents=True, exist_ok=True)

    unique_name = f"{uuid.uuid4()}_{file.filename}"
    dest_path = conv_dir / unique_name

    size_bytes = 0
    try:
        with dest_path.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                out.write(chunk)
                size_bytes += len(chunk)
    finally:
        await file.close()

    try:
        file_id = db.create_file_metadata(
            conversation_id=conversation_id,
            original_name=file.filename or unique_name,
            file_type=file_type,
            nfs_path=str(dest_path),
            size_bytes=size_bytes,
        )
    except Exception as exc:
        logger.exception("Failed to create file_metadata row")
        # Clean up the orphaned file on disk since we couldn't record it.
        dest_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Failed to record file metadata: {exc}") from exc

    task = process_file_task.delay(
        file_id=file_id,
        nfs_path=str(dest_path),
        conversation_id=conversation_id,
        file_type=file_type,
    )
    remember_task_file(task.id, file_id)

    return {"task_id": task.id, "file_id": file_id}


@app.get("/ingest/{task_id}/status")
def ingest_status(task_id: str):
    file_id = lookup_file_id_for_task(task_id)
    if file_id is None:
        raise HTTPException(status_code=404, detail="Unknown task_id")

    fm = db.get_file_metadata(file_id)
    if fm is None:
        raise HTTPException(status_code=404, detail="file_metadata row not found")

    status = fm["index_status"]
    error = fm.get("error_message")

    # Cross-check against Celery's own task state in case the worker died
    # before it could persist a 'failed' status to MSSQL.
    result = AsyncResult(task_id, app=celery_app)
    if result.state == "FAILURE" and status != "failed":
        status = "failed"
        error = error or str(result.result)

    return {"status": status, "file_id": file_id, "error": error}


@app.delete("/conversations/{conversation_id}/files")
def delete_conversation_files(conversation_id: str):
    """Delete the Milvus partition, the conversation's NFS directory, and
    the related file_metadata rows for a conversation. Used both by the
    retention-sweeper and by manual deletion via the gateway.
    """
    errors = []

    try:
        milvus_client.drop_partition(conversation_id)
    except Exception as exc:
        logger.exception("Failed to drop Milvus partition for conversation %s", conversation_id)
        errors.append(f"milvus: {exc}")

    conv_dir = Path(settings.NFS_ROOT_PATH) / conversation_id
    try:
        if conv_dir.exists():
            shutil.rmtree(conv_dir)
    except Exception as exc:
        logger.exception("Failed to remove NFS directory for conversation %s", conversation_id)
        errors.append(f"nfs: {exc}")

    deleted_rows = 0
    try:
        deleted_rows = db.delete_files_for_conversation(conversation_id)
    except Exception as exc:
        logger.exception("Failed to delete file_metadata rows for conversation %s", conversation_id)
        errors.append(f"mssql: {exc}")

    if errors:
        return JSONResponse(
            status_code=500,
            content={"deleted_rows": deleted_rows, "errors": errors},
        )
    return {"deleted_rows": deleted_rows}
