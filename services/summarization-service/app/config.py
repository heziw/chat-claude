"""Centralized configuration for summarization-service, loaded from env vars.

See /home/hezi/claude/chat/shared/CONTRACTS.md for the authoritative list of
env vars shared across services, and /home/hezi/claude/chat/.env.example for
local-dev defaults.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core infra
    MSSQL_HOST: str = "localhost"
    MSSQL_PORT: int = 1433
    MSSQL_DATABASE: str = "chatdb"
    MSSQL_USER: str = "sa"
    MSSQL_PASSWORD: str = "ChatApp!Dev123"
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530
    REDIS_URL: str = "redis://localhost:6379/0"

    # Mock mode / vLLM endpoints (same contract as the other services' llm_clients)
    MOCK_LLM: bool = True
    VLLM_ROUTER_URL: str = "http://localhost:9003/v1"

    # Model name used in the OpenAI-compatible chat/completions payload when
    # MOCK_LLM=false. Per the project plan, Llama-3.1-8B behind VLLM_ROUTER_URL
    # is also the map-summarization model. Not in CONTRACTS.md's env var list
    # (implementation detail); overridable via env like the other *_MODEL vars.
    VLLM_ROUTER_MODEL: str = "llama-3.1-8b"

    # Milvus collection naming (per CONTRACTS.md)
    MILVUS_COLLECTION: str = "doc_chunks"

    # Outbound HTTP timeout (seconds) for calls to the vLLM router.
    HTTP_TIMEOUT_SECONDS: float = 120.0

    # --- Hierarchical map-reduce knobs ---
    # Map stage: group this many chunks together per LLM call, but never
    # exceed MAP_GROUP_MAX_CHARS combined characters in one group (keeps each
    # map call within an efficient context size regardless of chunk count).
    MAP_GROUP_SIZE: int = 6
    MAP_GROUP_MAX_CHARS: int = 6000

    # Reduce stage: same idea, applied to intermediate summaries instead of
    # raw chunks.
    REDUCE_GROUP_SIZE: int = 5
    REDUCE_GROUP_MAX_CHARS: int = 6000

    # Safety cap on reduce recursion depth, so a pathological case (e.g. the
    # mock summarizer not shrinking text enough) can't loop forever. At this
    # depth we force-merge everything left into a single final LLM call.
    REDUCE_MAX_DEPTH: int = 3

    # This service's own port, per CONTRACTS.md (`summarization-service (:8003)`).
    SERVICE_PORT: int = 8003


settings = Settings()
