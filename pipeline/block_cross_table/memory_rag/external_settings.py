# -*- coding: utf-8 -*-
"""
M5 external API settings — share the same Zhipu stack as chat (ai/client).

Until a later batch splits models: base_url / model_id come from config
``api.base_url`` / ``api.model`` (default glm-4.5-flash). Secrets: ``ZHIPU_API_KEY`` only.

Note: chat models are not embedding-capable on ``/embeddings``; calling that path
with a chat model fails closed with memory_external_call_failed. Dedicated
embedding model_id (embedding-2/3) may be split out after subsequent tests.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


ENV_API_KEY = "ZHIPU_API_KEY"
ENV_BASE_URL = "MEMORY_RAG_EMBEDDING_BASE_URL"
ENV_MODEL_ID = "MEMORY_RAG_EMBEDDING_MODEL_ID"
ENV_EMBEDDING_DIM = "MEMORY_RAG_EMBEDDING_DIM"
ENV_SCHEMA_VERSION = "MEMORY_RAG_EMBEDDING_SCHEMA_VERSION"
ENV_CALL_CAP = "MEMORY_RAG_EXTERNAL_CALL_COUNT_CAP"
ENV_TIMEOUT_MS = "MEMORY_RAG_EXTERNAL_CALL_TIMEOUT_MS"
ENV_MAX_RETRIES = "MEMORY_RAG_EXTERNAL_CALL_MAX_RETRIES"
ENV_TOP_K = "MEMORY_RAG_RECALL_TOP_K"
ENV_MAX_DISTANCE = "MEMORY_RAG_RECALL_MAX_DISTANCE"

# Same defaults as config.PARAM_DEFAULTS / ai.client (unified Zhipu stack).
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_CHAT_MODEL_ID = "glm-4.5-flash"

# Known embedding dims when (later) an embedding model_id is selected.
DIM_BY_EMBEDDING_MODEL = {
    "embedding-3": 2048,
    "embedding-2": 1024,
}
# Shared-stack interim: no separate embedding model; dim only via env or known map.
DEFAULT_INTERIM_DIM = 2048

DEFAULT_SCHEMA_VERSION = "zhipu-shared-stack-v1"
DEFAULT_CALL_CAP = 5000
DEFAULT_TIMEOUT_MS = 30000
DEFAULT_MAX_RETRIES = 2
DEFAULT_TOP_K = 10


@dataclass(frozen=True)
class ExternalEmbeddingSettings:
    base_url: str
    api_key: str
    model_id: str
    embedding_dim: int
    embedding_schema_version: str
    call_count_cap: int
    timeout_ms: int
    max_retries: int
    top_k: int
    recall_max_distance: float | None
    allowlist: tuple[str, ...]

    @property
    def endpoint_fingerprint(self) -> str:
        from pipeline.block_cross_table.memory_rag.external_call import (
            endpoint_fingerprint,
        )

        return endpoint_fingerprint(self.base_url)

    @property
    def is_embedding_capable_model(self) -> bool:
        mid = str(self.model_id or "").strip().lower()
        return mid in DIM_BY_EMBEDDING_MODEL or mid.startswith("embedding-")


def _env(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    return str(raw).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return int(default)
    return int(raw)


def _env_float(name: str) -> float | None:
    raw = _env(name)
    if raw is None:
        return None
    return float(raw)


def _config_param(key: str) -> str | None:
    try:
        import config

        val = config.get_param(key)
    except Exception:
        return None
    if val is None or str(val).strip() == "":
        return None
    return str(val).strip()


def resolve_api_key() -> str:
    """Product path: only ZHIPU_API_KEY. Empty → caller must fail closed."""
    return _env(ENV_API_KEY) or ""


def resolve_base_url() -> str:
    # Prefer shared chat stack; MEMORY_RAG_* only if explicitly set (later split).
    return _env(ENV_BASE_URL) or _config_param("api.base_url") or DEFAULT_BASE_URL


def resolve_model_id() -> str:
    # Unified with chat until a later batch splits embedding model.
    return (
        _env(ENV_MODEL_ID)
        or _config_param("api.model")
        or DEFAULT_CHAT_MODEL_ID
    )


def resolve_embedding_dim(*, model_id: str | None = None) -> int:
    override = _env(ENV_EMBEDDING_DIM)
    if override is not None:
        return int(override)
    mid = model_id or resolve_model_id()
    if mid in DIM_BY_EMBEDDING_MODEL:
        return int(DIM_BY_EMBEDDING_MODEL[mid])
    # Shared chat model: dim is not defined by provider for /embeddings;
    # keep interim placeholder until split — callers must not silent-mix.
    return int(DEFAULT_INTERIM_DIM)


def resolve_schema_version(*, model_id: str | None = None) -> str:
    override = _env(ENV_SCHEMA_VERSION)
    if override is not None:
        return override
    mid = model_id or resolve_model_id()
    if mid in DIM_BY_EMBEDDING_MODEL:
        return "zhipu-%s-v1" % mid
    return DEFAULT_SCHEMA_VERSION


def load_external_embedding_settings() -> ExternalEmbeddingSettings:
    model_id = resolve_model_id()
    return ExternalEmbeddingSettings(
        base_url=resolve_base_url(),
        api_key=resolve_api_key(),
        model_id=model_id,
        embedding_dim=resolve_embedding_dim(model_id=model_id),
        embedding_schema_version=resolve_schema_version(model_id=model_id),
        call_count_cap=_env_int(ENV_CALL_CAP, DEFAULT_CALL_CAP),
        timeout_ms=_env_int(ENV_TIMEOUT_MS, DEFAULT_TIMEOUT_MS),
        max_retries=_env_int(ENV_MAX_RETRIES, DEFAULT_MAX_RETRIES),
        top_k=_env_int(ENV_TOP_K, DEFAULT_TOP_K),
        recall_max_distance=_env_float(ENV_MAX_DISTANCE),
        allowlist=(),
    )


def require_api_key(settings: ExternalEmbeddingSettings | None = None) -> str:
    from pipeline.block_cross_table.memory_rag.errors import (
        LOCUS_EXTERNAL_CALL_FAILED,
        MemoryRagError,
    )

    s = settings or load_external_embedding_settings()
    if not s.api_key:
        raise MemoryRagError(
            "ZHIPU_API_KEY missing",
            fail_locus=LOCUS_EXTERNAL_CALL_FAILED,
            subreason="missing_api_key",
        )
    return s.api_key


def require_embedding_capable_model(
    settings: ExternalEmbeddingSettings | None = None,
) -> ExternalEmbeddingSettings:
    """
    Fail closed when shared stack still points at a chat model.
    Split embedding model_id in a later batch; until then this guards /embeddings.
    """
    from pipeline.block_cross_table.memory_rag.errors import (
        LOCUS_EXTERNAL_CALL_FAILED,
        MemoryRagError,
    )

    s = settings or load_external_embedding_settings()
    if not s.is_embedding_capable_model:
        raise MemoryRagError(
            "shared chat model cannot call /embeddings; split embedding model later",
            fail_locus=LOCUS_EXTERNAL_CALL_FAILED,
            subreason="model_not_embedding_capable",
            detail={
                "model_id": s.model_id,
                "hint": "use embedding-2/embedding-3 after split, or set MEMORY_RAG_EMBEDDING_MODEL_ID",
            },
        )
    return s
