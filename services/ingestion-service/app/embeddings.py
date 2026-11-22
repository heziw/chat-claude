"""Embedding generation, with MOCK_LLM support per CONTRACTS.md.

`embed_texts` is the single entry point used by the ingestion pipeline.
When MOCK_LLM=true it returns deterministic pseudo-random vectors (seeded
from a hash of each text) so the whole pipeline can be exercised without a
GPU/vLLM. When MOCK_LLM=false it calls the real e5 embedding endpoint
(VLLM_EMBED_URL, OpenAI-compatible /v1/embeddings, model="e5-embed"),
applying the "query: " / "passage: " prefix convention required by e5-style
models.
"""
from __future__ import annotations

import hashlib
import logging
import random

import requests

from app.config import settings

logger = logging.getLogger(__name__)


def _mock_embedding(text: str, dim: int) -> list[float]:
    """Deterministic pseudo-random embedding: seed PRNG from a hash of the
    text so the same text always yields the same vector. This is only for
    exercising the pipeline end-to-end — it has no semantic relevance.
    """
    seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16) % (2**32)
    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(dim)]


def embed_texts(
    texts: list[str],
    is_query: bool = False,
    dim: int | None = None,
) -> list[list[float]]:
    """Embed a batch of texts.

    Args:
        texts: list of raw text strings to embed.
        is_query: True for a search query, False for a document/passage
            being indexed. Controls the e5 "query: " / "passage: " prefix.
        dim: override embedding dimensionality (defaults to settings.EMBED_DIM).
    """
    dim = dim or settings.EMBED_DIM

    if settings.MOCK_LLM:
        return [_mock_embedding(t, dim) for t in texts]

    prefix = "query: " if is_query else "passage: "
    prefixed = [f"{prefix}{t}" for t in texts]

    url = f"{settings.VLLM_EMBED_URL.rstrip('/')}/embeddings"
    try:
        resp = requests.post(
            url,
            json={"model": "e5-embed", "input": prefixed},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        # OpenAI-compatible response: {"data": [{"embedding": [...], "index": 0}, ...]}
        items = sorted(data["data"], key=lambda d: d.get("index", 0))
        return [item["embedding"] for item in items]
    except Exception as exc:
        logger.error("embed_texts: call to %s failed: %s", url, exc)
        raise
