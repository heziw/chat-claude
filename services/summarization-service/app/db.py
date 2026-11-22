"""Postgres access for the `file_metadata` table (see postgres-init.sql /
CONTRACTS.md for the schema). Uses psycopg2 with a short-lived connection
per call, same pattern as ingestion-service/app/db.py -- safe to use both
from the FastAPI process and from Celery worker processes.

summarization-service only ever *reads* file_metadata: it needs
conversation_id / milvus_partition for a given file_id so it knows where to
read chunks from in Milvus.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.extras

from app.config import settings

logger = logging.getLogger(__name__)


@contextmanager
def get_conn():
    conn = psycopg2.connect(settings.POSTGRES_URL)
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
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM file_metadata WHERE id = %s", (file_id,))
            row = cur.fetchone()
            return dict(row) if row else None
