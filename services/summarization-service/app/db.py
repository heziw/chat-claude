"""MSSQL access for the `file_metadata` table (see infra/docker/mssql-init.sql /
CONTRACTS.md for the schema). Uses python-tds (pytds) with a short-lived
connection per call, same pattern as ingestion-service/app/db.py -- safe to
use both from the FastAPI process and from Celery worker processes.

summarization-service only ever *reads* file_metadata: it needs
conversation_id / milvus_partition for a given file_id so it knows where to
read chunks from in Milvus.
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


def get_file_metadata(file_id: str) -> dict[str, Any] | None:
    """Fetch the file_metadata row for file_id, or None if it doesn't exist."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM file_metadata WHERE id = %s", (file_id,))
            row = cur.fetchone()
            return dict(row) if row else None
