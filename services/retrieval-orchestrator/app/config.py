"""Centralized configuration for retrieval-orchestrator, loaded from env vars.

See /home/hezi/claude/chat/shared/CONTRACTS.md for the authoritative list of
env vars shared across services, and /home/hezi/claude/chat/.env.example for
local-dev defaults.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core infra
    POSTGRES_URL: str = "postgresql://chatuser:chatpass@localhost:5432/chatdb"
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530
    REDIS_URL: str = "redis://localhost:6379/0"
    NFS_ROOT_PATH: str = "./data/nfs-raw-files"

    # Mock mode / vLLM endpoints (same contract as ingestion-service's llm_clients)
    MOCK_LLM: bool = True
    VLLM_GENERATION_URL: str = "http://localhost:9000/v1"
    VLLM_EMBED_URL: str = "http://localhost:9001/v1"
    VLLM_RERANK_URL: str = "http://localhost:9002/v1"
    VLLM_ROUTER_URL: str = "http://localhost:9003/v1"
    VLLM_VISION_URL: str = "http://localhost:9004/v1"

    # Model names used in OpenAI-compatible request payloads when MOCK_LLM=false.
    # Not in CONTRACTS.md's env var list (implementation detail); overridable via env.
    VLLM_GENERATION_MODEL: str = "muse30b"
    VLLM_EMBED_MODEL: str = "multilingual-e5-large"
    # Model name for the reranker, sent in the /rerank request payload (see
    # app/retrieval.py's `_real_rerank_scores` docstring for the endpoint
    # shape assumption).
    VLLM_RERANK_MODEL: str = "rerank"
    # Model name for the router LLM, used both for query classification
    # (classify_query) and multi-query decomposition (decompose_query).
    VLLM_ROUTER_MODEL: str = "router"

    # Embedding dim per CONTRACTS.md (intfloat/multilingual-e5-large)
    EMBED_DIM: int = 1024

    # Milvus collection naming (per CONTRACTS.md)
    MILVUS_COLLECTION: str = "doc_chunks"

    # --- Hybrid retrieval knobs (app/retrieval.py: retrieve()) ---
    # Candidate-list size fetched independently from each of the dense
    # (Milvus HNSW) and BM25 (client-side rank_bm25 -- Milvus 2.4.9 has no
    # native full-text/BM25 search, that lands only in 2.5) retrieval
    # methods, before fusion.
    TOP_K_DENSE: int = 8
    # How many fused candidates (Reciprocal Rank Fusion over the dense +
    # BM25 candidate lists) survive to be sent to the reranker.
    RRF_TOP_N: int = 10
    # Standard RRF smoothing constant: score = sum(1/(k+rank)).
    RRF_K: int = 60
    # Final number of chunks returned by retrieve() after reranking, i.e.
    # what's actually fed into generation. This is the new default for
    # retrieve()'s `top_k` param (previously that was TOP_K_DENSE, back when
    # retrieval was dense-only).
    TOP_K_RERANK: int = 5

    # Downstream service this one proxies to for the debug schema endpoint,
    # and calls for the "structured" query-routing path.
    STRUCTURED_AGENT_URL: str = "http://localhost:8002"
    # Downstream service called for the "summarize" query-routing path.
    SUMMARIZATION_SERVICE_URL: str = "http://localhost:8003"

    # Outbound HTTP timeouts (seconds) for single request/response calls to
    # vLLM / structured-data-agent / summarization-service (NOT the
    # summarize-job polling budget below).
    HTTP_TIMEOUT_SECONDS: float = 60.0

    # Polling knobs for the "summarize" route: summarization-service's
    # POST /summarize is async (202 + task_id), so we poll GET
    # /summarize/{task_id} until status is ready/failed or this wall-clock
    # budget is exhausted (mock mode finishes almost immediately; a real
    # map-reduce job on a big document may legitimately take longer, in
    # which case the caller gets a "still processing" answer instead of
    # hanging the request forever).
    SUMMARIZE_POLL_INTERVAL_SECONDS: float = 1.0
    SUMMARIZE_POLL_TIMEOUT_SECONDS: float = 30.0

    # This service's own port, per CONTRACTS.md (`retrieval-orchestrator (:8001)`).
    SERVICE_PORT: int = 8001


settings = Settings()
