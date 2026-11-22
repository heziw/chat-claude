"""Postgres access for structured-data-agent: read-only lookups against the
`file_metadata` table (see shared/CONTRACTS.md for the schema). Uses
psycopg2 with a short-lived connection per call, matching the pattern used
by ingestion-service's app/db.py.
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
    """Look up a single file_metadata row by id. Returns None if not found
    (callers should turn that into a 404, not a 500)."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM file_metadata WHERE id = %s", (file_id,))
            row = cur.fetchone()
            return dict(row) if row else None
