"""MSSQL access for the `file_metadata` table (see infra/docker/mssql-init.sql /
CONTRACTS.md for the schema). Uses python-tds (pytds) with a short-lived
connection per call — simple and safe to use both from the FastAPI process
and from Celery worker processes (which don't share connections with the
API).

IDs are generated in the application (uuid4 strings) rather than relying on
a DB-side default/RETURNING, per CONTRACTS.md's "IDs created in the
application, not the DB" rule — MSSQL has no `RETURNING`, only
`OUTPUT INSERTED.*`, and generating the id up-front avoids needing that.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from typing import Any

import pytds

from app.config import settings

logger = logging.getLogger(__name__)


@contextmanager
def get_conn():
    conn = pytds.connect(
        server=settings.MSSQL_HOST,
        port=settings.MSSQL_PORT,
        database=settings.MSSQL_DATABASE,
        user=settings.MSSQL_USER,
        password=settings.MSSQL_PASSWORD,
        as_dict=True,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_file_metadata(
    conversation_id: str,
    original_name: str,
    file_type: str,
    nfs_path: str,
    size_bytes: int,
) -> str:
    """Insert a new file_metadata row with index_status='pending'.
    Returns the generated file_id (UUID string).
    """
    file_id = str(uuid.uuid4())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO file_metadata
                    (id, conversation_id, original_name, file_type, nfs_path, size_bytes, index_status)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                """,
                (file_id, conversation_id, original_name, file_type, nfs_path, size_bytes),
            )
    return file_id


def update_index_status(
    file_id: str,
    index_status: str,
    milvus_partition: str | None = None,
    structured_store_ref: str | None = None,
    error_message: str | None = None,
) -> None:
    """Update index_status and (optionally) milvus_partition /
    structured_store_ref. milvus_partition is used by the pdf/docx/image
    (embedding) pipeline; structured_store_ref is used by the xlsx/csv/json
    (side-store) pipeline. Both use COALESCE so passing None leaves the
    existing column value untouched.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE file_metadata
                SET index_status = %s,
                    milvus_partition = COALESCE(%s, milvus_partition),
                    structured_store_ref = COALESCE(%s, structured_store_ref),
                    error_message = %s
                WHERE id = %s
                """,
                (index_status, milvus_partition, structured_store_ref, error_message, file_id),
            )


def get_file_metadata(file_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM file_metadata WHERE id = %s", (file_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def list_files_for_conversation(conversation_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM file_metadata WHERE conversation_id = %s",
                (conversation_id,),
            )
            return [dict(r) for r in cur.fetchall()]


def delete_files_for_conversation(conversation_id: str) -> int:
    """Delete all file_metadata rows for a conversation. Returns row count."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM file_metadata WHERE conversation_id = %s",
                (conversation_id,),
            )
            return cur.rowcount
