"""Centralized configuration for structured-data-agent, loaded from env vars.

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
    NFS_ROOT_PATH: str = "./data/nfs-raw-files"

    # Mock mode / vLLM endpoint (same contract as the other services' llm_clients)
    MOCK_LLM: bool = True
    VLLM_ROUTER_URL: str = "http://localhost:9003/v1"

    # Model name used in the OpenAI-compatible request payload when
    # MOCK_LLM=false. Not in CONTRACTS.md's env var list (implementation
    # detail, mirrors VLLM_GENERATION_MODEL in retrieval-orchestrator);
    # overridable via env.
    VLLM_ROUTER_MODEL: str = "router"

    # Outbound HTTP timeout (seconds) for calls to the vLLM router.
    HTTP_TIMEOUT_SECONDS: float = 60.0

    # Wall-clock budget for a single exec() of LLM-generated pandas code
    # inside the restricted sandbox (app/sandbox.py). Short on purpose: a
    # legitimate aggregation over a side-store DataFrame should finish well
    # under this, and anything that doesn't is treated as a bug/hang, not a
    # slow-but-valid computation.
    SANDBOX_TIMEOUT_SECONDS: float = 5.0

    # This service's own port, per CONTRACTS.md (`structured-data-agent (:8002)`).
    SERVICE_PORT: int = 8002


settings = Settings()
