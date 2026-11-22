"""Milvus connection + collection/partition management for doc_chunks.

Schema (see shared/CONTRACTS.md):
    chunk_id          VARCHAR(64)   primary key, "<file_id>_<chunk_index>"
    conversation_id   VARCHAR(36)
    file_id           VARCHAR(36)
    file_type         VARCHAR(16)
    source_ref        VARCHAR(256)  e.g. "עמוד 12"
    text              VARCHAR(8192)
    dense_vector      FLOAT_VECTOR(1024)

NOTE on sparse_vector: CONTRACTS.md also specifies a `sparse_vector`
SPARSE_FLOAT_VECTOR field (BM25-style term weights) for future hybrid
search. We deliberately OMIT it in this phase: SPARSE_FLOAT_VECTOR support
varies across pymilvus/Milvus versions and wiring up a real BM25/sparse
encoder is out of scope for the PDF-only ingestion pipeline being built
here. Whoever adds hybrid search later should add the `sparse_vector`
field to COLLECTION_FIELDS below, add a sparse index, and backfill/re-embed
existing chunks.

Partition naming: `conv_<conversation_id without dashes>` (Milvus partition
names can't contain dashes).
"""
from __future__ import annotations

import logging

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    utility,
)

from app.config import settings

logger = logging.getLogger(__name__)

_CONNECTION_ALIAS = "default"
_CONNECTED = False


def partition_name_for(conversation_id: str) -> str:
    return f"conv_{conversation_id.replace('-', '')}"


def connect() -> None:
    global _CONNECTED
    if _CONNECTED:
        return
    connections.connect(
        alias=_CONNECTION_ALIAS,
        host=settings.MILVUS_HOST,
        port=str(settings.MILVUS_PORT),
    )
    _CONNECTED = True
    logger.info("Connected to Milvus at %s:%s", settings.MILVUS_HOST, settings.MILVUS_PORT)


def _build_schema() -> CollectionSchema:
    fields = [
        FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, max_length=64, is_primary=True),
        FieldSchema(name="conversation_id", dtype=DataType.VARCHAR, max_length=36),
        FieldSchema(name="file_id", dtype=DataType.VARCHAR, max_length=36),
        FieldSchema(name="file_type", dtype=DataType.VARCHAR, max_length=16),
        FieldSchema(name="source_ref", dtype=DataType.VARCHAR, max_length=256),
        FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=8192),
        FieldSchema(
            name="dense_vector",
            dtype=DataType.FLOAT_VECTOR,
            dim=settings.EMBED_DIM,
        ),
        # sparse_vector intentionally omitted - see module docstring.
    ]
    return CollectionSchema(fields=fields, description="RAG document chunks (dense-only, phase 1)")


def ensure_collection() -> Collection:
    """Create the doc_chunks collection (+ HNSW cosine index) if missing,
    and return a usable Collection handle.
    """
    connect()
    name = settings.MILVUS_COLLECTION
    if not utility.has_collection(name):
        schema = _build_schema()
        collection = Collection(name=name, schema=schema)
        collection.create_index(
            field_name="dense_vector",
            index_params={
                "index_type": "HNSW",
                "metric_type": "COSINE",
                "params": {"M": 16, "efConstruction": 200},
            },
        )
        logger.info("Created Milvus collection '%s' with HNSW/cosine index", name)
    else:
        collection = Collection(name=name)
    return collection


def ensure_partition(collection: Collection, partition_name: str) -> None:
    if not collection.has_partition(partition_name):
        collection.create_partition(partition_name)
        logger.info("Created Milvus partition '%s'", partition_name)


def insert_chunks(
    conversation_id: str,
    file_id: str,
    file_type: str,
    chunk_ids: list[str],
    source_refs: list[str],
    texts: list[str],
    dense_vectors: list[list[float]],
) -> str:
    """Insert chunks for one file into its conversation's partition.
    Returns the partition name used.
    """
    collection = ensure_collection()
    partition_name = partition_name_for(conversation_id)
    ensure_partition(collection, partition_name)

    n = len(chunk_ids)
    data = [
        chunk_ids,
        [conversation_id] * n,
        [file_id] * n,
        [file_type] * n,
        source_refs,
        texts,
        dense_vectors,
    ]
    collection.insert(data, partition_name=partition_name)
    collection.flush()
    logger.info("Inserted %d chunks for file_id=%s into partition=%s", n, file_id, partition_name)
    return partition_name


def drop_partition(conversation_id: str) -> None:
    """Drop the Milvus partition for a conversation (used on conversation
    deletion / retention sweep). No-op if the collection or partition
    doesn't exist.
    """
    connect()
    name = settings.MILVUS_COLLECTION
    if not utility.has_collection(name):
        return
    collection = Collection(name=name)
    partition_name = partition_name_for(conversation_id)
    if collection.has_partition(partition_name):
        try:
            collection.release()
        except Exception:
            pass
        collection.drop_partition(partition_name)
        logger.info("Dropped Milvus partition '%s'", partition_name)
