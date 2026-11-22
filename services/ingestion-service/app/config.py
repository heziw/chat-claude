"""Centralized configuration for ingestion-service, loaded from env vars.

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

    # Mock mode / vLLM endpoints
    MOCK_LLM: bool = True
    VLLM_GENERATION_URL: str = "http://localhost:9000/v1"
    VLLM_EMBED_URL: str = "http://localhost:9001/v1"
    VLLM_RERANK_URL: str = "http://localhost:9002/v1"
    VLLM_ROUTER_URL: str = "http://localhost:9003/v1"
    VLLM_VISION_URL: str = "http://localhost:9004/v1"

    # Chunking knobs
    CHUNK_SIZE_WORDS: int = 500
    CHUNK_OVERLAP_WORDS: int = 50

    # Embedding dim per CONTRACTS.md (intfloat/multilingual-e5-large)
    EMBED_DIM: int = 1024

    # Milvus collection/index naming
    MILVUS_COLLECTION: str = "doc_chunks"


settings = Settings()
