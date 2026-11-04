# -*- coding: utf-8 -*-
"""Embed arbitrary query text via the same external path (M5 recall)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from pipeline.block_cross_table.memory_rag.external_call import embed_binding_external
from pipeline.block_cross_table.memory_rag.external_settings import (
    ExternalEmbeddingSettings,
    load_external_embedding_settings,
    require_api_key,
    require_embedding_capable_model,
)
from pipeline.block_cross_table.memory_rag.meta import EmbeddingVectorMeta


def embed_query_text(
    conn: sqlite3.Connection,
    *,
    text: str,
    settings: ExternalEmbeddingSettings | None = None,
    http_transport=None,
) -> tuple[list[float], EmbeddingVectorMeta, list[dict[str, Any]]]:
    s = settings or load_external_embedding_settings()
    require_api_key(s)
    if settings is None:
        require_embedding_capable_model(s)
    return embed_binding_external(
        conn,
        binding_id=None,
        binding_payload=str(text),
        base_url=s.base_url,
        api_key=s.api_key,
        model_id=s.model_id,
        embedding_schema_version=s.embedding_schema_version,
        allowlist=list(s.allowlist),
        call_count_cap=s.call_count_cap,
        timeout_ms=s.timeout_ms,
        max_retries=s.max_retries,
        created_at=datetime.now(timezone.utc).isoformat(),
        http_transport=http_transport,
        expected_dim=s.embedding_dim,
    )
