"""Read-only Milvus access for summarization-service: fetch *all* chunks for
a given file_id, correctly ordered, so the map-reduce pipeline can walk the
document top to bottom.

Schema / partition naming per shared/CONTRACTS.md and
ingestion-service/app/milvus_client.py:
    collection:  doc_chunks
    partition:   conv_<conversation_id without dashes>
    chunk_id:    "<file_id>_<chunk_index>"  (file_id is a dashed UUID, so
                 splitting on the *last* underscore safely isolates the
                 numeric chunk_index)

We deliberately query (not search) -- this is a scalar filter on file_id
within one partition, not a vector similarity lookup.
"""
from __future__ import annotations

import logging
from typing import Any

from pymilvus import Collection, connections, utility

from app.config import settings

logger = logging.getLogger(__name__)

_CONNECTION_ALIAS = "default"
_CONNECTED = False

# Chunk text can be up to 8192 chars (VARCHAR(8192) in the schema); Milvus
# query() has its own internal batch handling, but we still pass a generous
# limit so one call covers realistically-sized documents. Chunks beyond this
# are silently fetched again in a follow-up page (Milvus query does not
# support unbounded result sets without a limit).
_QUERY_PAGE_LIMIT = 1000


def _ensure_connected() -> None:
    global _CONNECTED
    if _CONNECTED:
        return
    connections.connect(alias=_CONNECTION_ALIAS, host=settings.MILVUS_HOST, port=str(settings.MILVUS_PORT))
    _CONNECTED = True
    logger.info("Connected to Milvus at %s:%s", settings.MILVUS_HOST, settings.MILVUS_PORT)


def _chunk_index(chunk_id: str) -> int:
    """Extract the numeric chunk_index suffix from a "<file_id>_<chunk_index>"
    chunk_id so chunks can be sorted in true document order.

    A plain string sort of chunk_id would be wrong once a document has 10+
    chunks (lexicographic "...10" sorts before "...2"), so we parse out the
    integer suffix instead. Falls back to 0 (stable, won't crash) if the
    suffix isn't parseable for some reason.
    """
    try:
        return int(chunk_id.rsplit("_", 1)[-1])
    except (ValueError, AttributeError):
        logger.warning("Could not parse numeric chunk index out of chunk_id=%r", chunk_id)
        return 0


def fetch_chunks_for_file(partition_name: str, file_id: str) -> list[dict[str, Any]]:
    """Fetch every chunk belonging to `file_id`, scoped to `partition_name`,
    sorted by their true numeric chunk order.

    Returns an empty list (never raises) if the collection or partition
    doesn't exist -- callers treat that as "nothing to summarize".
    """
    _ensure_connected()

    collection_name = settings.MILVUS_COLLECTION
    if not utility.has_collection(collection_name, using=_CONNECTION_ALIAS):
        logger.warning("Milvus collection %s does not exist", collection_name)
        return []

    collection = Collection(collection_name, using=_CONNECTION_ALIAS)

    if not collection.has_partition(partition_name):
        logger.warning("Partition %s does not exist in collection %s", partition_name, collection_name)
        return []

    collection.load()

    rows = collection.query(
        expr=f'file_id == "{file_id}"',
        partition_names=[partition_name],
        output_fields=["chunk_id", "file_id", "source_ref", "text"],
        limit=_QUERY_PAGE_LIMIT,
    )

    chunks = [
        {
            "chunk_id": r.get("chunk_id"),
            "file_id": r.get("file_id"),
            "source_ref": r.get("source_ref"),
            "text": r.get("text"),
        }
        for r in rows
    ]
    chunks.sort(key=lambda c: _chunk_index(c["chunk_id"]))
    return chunks
