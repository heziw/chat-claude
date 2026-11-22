"""Loads side-store parquet DataFrame(s) for a given file_id.

ingestion-service persists xlsx/csv/json files as pandas DataFrames via
``df.to_parquet(...)`` under:

    NFS_ROOT_PATH/<conversation_id>/structured/<file_id>.parquet

or, for a multi-sheet xlsx file, one parquet per sheet:

    NFS_ROOT_PATH/<conversation_id>/structured/<file_id>__<sheet_name>.parquet

The authoritative pointer is recorded in Postgres
``file_metadata.structured_store_ref``. Because the exact shape of that
pointer is an implementation detail of ingestion-service (built in
parallel), this loader is deliberately permissive about what
``structured_store_ref`` may contain:

  1. A direct path to a single ``.parquet`` file.
  2. A directory containing one or more ``*.parquet`` files (one per sheet).
  3. A JSON metadata file pointing at per-sheet paths, e.g.
     ``{"sheets": {"Sheet1": "/path/a.parquet", "Sheet2": "/path/b.parquet"}}``.

If ``structured_store_ref`` is missing, stale, or doesn't resolve to
anything, we fall back to scanning the conventional
``NFS_ROOT_PATH/<conversation_id>/structured/`` directory for
``<file_id>.parquet`` or ``<file_id>__*.parquet``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from app.config import settings

logger = logging.getLogger(__name__)

# Sheet name used for single-sheet files (csv/json, or an xlsx with exactly
# one sheet) where there's no real sheet name to report.
DEFAULT_SHEET_NAME = "default"


class StructuredDataNotFoundError(Exception):
    """Raised when no parquet side-store can be located for a file_id."""


def _structured_dir(conversation_id: str) -> Path:
    return Path(settings.NFS_ROOT_PATH) / conversation_id / "structured"


def _sheet_name_from_path(path: Path, file_id: str) -> str:
    stem = path.stem  # "<file_id>" or "<file_id>__<sheet_name>"
    prefix = f"{file_id}__"
    if stem.startswith(prefix):
        return stem[len(prefix):]
    return DEFAULT_SHEET_NAME


def _load_from_ref(structured_store_ref: str, file_id: str) -> dict[str, pd.DataFrame] | None:
    ref_path = Path(structured_store_ref)

    if ref_path.is_file() and ref_path.suffix == ".json":
        try:
            meta = json.loads(ref_path.read_text())
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not parse structured_store_ref JSON metadata at %s", ref_path, exc_info=True)
            return None
        sheets = meta.get("sheets", meta)
        dfs = {name: pd.read_parquet(Path(p)) for name, p in sheets.items()}
        return dfs or None

    if ref_path.is_file() and ref_path.suffix == ".parquet":
        return {_sheet_name_from_path(ref_path, file_id): pd.read_parquet(ref_path)}

    if ref_path.is_dir():
        # A directory ref may be shared by multiple files in the same
        # conversation (e.g. "<conv>/structured/"), so filter to parquet
        # files that actually belong to this file_id rather than globbing
        # everything in the directory.
        candidates = sorted(ref_path.glob(f"{file_id}.parquet")) or sorted(ref_path.glob(f"{file_id}__*.parquet"))
        if candidates:
            return {_sheet_name_from_path(p, file_id): pd.read_parquet(p) for p in candidates}

    return None


def load_dataframes(
    file_id: str, structured_store_ref: str | None, conversation_id: str
) -> dict[str, pd.DataFrame]:
    """Return ``{sheet_name: DataFrame}``. A single-sheet file yields
    ``{"default": df}``; a multi-sheet xlsx yields one entry per sheet.
    Raises ``StructuredDataNotFoundError`` if nothing can be located.
    """
    dfs: dict[str, pd.DataFrame] | None = None

    if structured_store_ref:
        dfs = _load_from_ref(structured_store_ref, file_id)

    if not dfs:
        structured_dir = _structured_dir(conversation_id)
        single = structured_dir / f"{file_id}.parquet"
        if single.exists():
            dfs = {DEFAULT_SHEET_NAME: pd.read_parquet(single)}
        else:
            candidates = sorted(structured_dir.glob(f"{file_id}__*.parquet"))
            if candidates:
                dfs = {_sheet_name_from_path(p, file_id): pd.read_parquet(p) for p in candidates}

    if not dfs:
        raise StructuredDataNotFoundError(
            f"No parquet side-store found for file_id={file_id!r} "
            f"(structured_store_ref={structured_store_ref!r}, conversation_id={conversation_id!r})"
        )

    return dfs


def schema_for_dataframes(dfs: dict[str, pd.DataFrame], sample_size: int = 5) -> dict:
    """Build the GET /schema/{file_id} response body. Keyed per-sheet so the
    same shape covers both single-sheet files (one key, "default") and
    multi-sheet xlsx files (one key per sheet name)."""
    sheets = list(dfs.keys())
    columns: dict[str, list[dict[str, str]]] = {}
    num_rows: dict[str, int] = {}
    sample_rows: dict[str, list[dict]] = {}
    for name, df in dfs.items():
        columns[name] = [{"name": str(col), "dtype": str(dtype)} for col, dtype in df.dtypes.items()]
        num_rows[name] = int(len(df))
        sample_rows[name] = json.loads(df.head(sample_size).to_json(orient="records", force_ascii=False))
    return {"sheets": sheets, "columns": columns, "num_rows": num_rows, "sample_rows": sample_rows}


def describe_schema_text(dfs: dict[str, pd.DataFrame]) -> str:
    """Render a compact textual schema description for LLM prompts (both
    the mock keyword heuristic and the real NL->pandas prompt use this)."""
    parts = []
    for name, df in dfs.items():
        cols = ", ".join(f"{col} ({dtype})" for col, dtype in df.dtypes.items())
        parts.append(f"גיליון '{name}': {len(df)} שורות, עמודות: {cols}")
    return "\n".join(parts)
