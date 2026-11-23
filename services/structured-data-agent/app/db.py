"""MSSQL access for structured-data-agent: read-only lookups against the
`file_metadata` table (see shared/CONTRACTS.md for the schema). Uses
python-tds (pytds) with a short-lived connection per call, matching the
pattern used by ingestion-service's app/db.py.
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
    """Look up a single file_metadata row by id. Returns None if not found
    (callers should turn that into a 404, not a 500)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM file_metadata WHERE id = %s", (file_id,))
            row = cur.fetchone()
            return dict(row) if row else None
