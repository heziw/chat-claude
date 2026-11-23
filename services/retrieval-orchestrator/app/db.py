"""MSSQL access for retrieval-orchestrator's query-routing logic.

Read-only access to `file_metadata` (see shared/CONTRACTS.md). Modeled on
ingestion-service's app/db.py: a short-lived python-tds (pytds) connection
per call -- simple and safe to use from the FastAPI request-handling
process, no connection pool to manage.

Used by app/main.py to figure out, per conversation, which files exist and
of what type/status, so `classify_query()` can route between the
factual / structured / summarize / multi paths.
"""
from __future__ import annotations

import logging
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


def list_files_for_conversation(conversation_id: str) -> list[dict[str, Any]]:
    """All file_metadata rows for this conversation, newest-first by
    created_at. The newest-first ordering lets callers pick "the most
    recently uploaded file" with a simple `[0]` (used by the summarize
    route when a conversation has more than one non-structured file).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, original_name, file_type, index_status,
                       structured_store_ref, milvus_partition, created_at
                FROM file_metadata
                WHERE conversation_id = %s
                ORDER BY created_at DESC
                """,
                (conversation_id,),
            )
            return [dict(r) for r in cur.fetchall()]
