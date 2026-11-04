# -*- coding: utf-8 -*-
"""
M5: confirm write + real external embed + vec0 insert (one pipeline).
"""
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
from pipeline.block_cross_table.memory_rag.constants import TABLE_BINDING
from pipeline.block_cross_table.memory_rag.store import (
    insert_binding_confirmed,
    insert_embedding_meta,
)
from pipeline.block_cross_table.memory_rag.vec_index import (
    allocate_vec_rowid,
    insert_vector_row,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_binding_with_external_embed(
    conn: sqlite3.Connection,
    *,
    utterance_span_fingerprint: str,
    binding_payload: dict[str, Any] | str,
    scope: dict[str, Any] | str,
    source_trace_id: str,
    settings: ExternalEmbeddingSettings | None = None,
    user_confirmed: bool = True,
    confirmed_at: str | None = None,
    http_transport=None,
) -> dict[str, Any]:
    """
    Confirm → external embed → meta → vec0 row.
    Failures surface as memory_external_call_failed (no empty vector store).
    """
    s = settings or load_external_embedding_settings()
    require_api_key(s)
    # Shared stack may still be chat model (glm-4.5-flash); refuse silent /embeddings.
    if settings is None:
        require_embedding_capable_model(s)
    ts = confirmed_at or _now()
    bid, write_traces = insert_binding_confirmed(
        conn,
        user_confirmed=user_confirmed,
        utterance_span_fingerprint=utterance_span_fingerprint,
        binding_payload=binding_payload,
        source_trace_id=source_trace_id,
        confirmed_at=ts,
        scope=scope,
    )
    emb, meta, call_traces = embed_binding_external(
        conn,
        binding_id=bid,
        binding_payload=binding_payload,
        base_url=s.base_url,
        api_key=s.api_key,
        model_id=s.model_id,
        embedding_schema_version=s.embedding_schema_version,
        allowlist=list(s.allowlist),
        call_count_cap=s.call_count_cap,
        timeout_ms=s.timeout_ms,
        max_retries=s.max_retries,
        created_at=ts,
        http_transport=http_transport,
        expected_dim=s.embedding_dim,
    )
    meta_id = insert_embedding_meta(conn, meta=meta, created_at=ts)
    conn.execute(
        f"""
        UPDATE {TABLE_BINDING}
        SET embedding_meta_ref=?
        WHERE binding_id=?
        """,
        (meta_id, bid),
    )
    conn.commit()
    rowid = allocate_vec_rowid(conn, binding_id=bid)
    insert_vector_row(
        conn,
        rowid=rowid,
        embedding=emb,
        expected_dim=s.embedding_dim,
        query_meta=meta,
        row_meta=meta,
    )
    return {
        "binding_id": bid,
        "meta_id": meta_id,
        "rowid": rowid,
        "embedding_dim": len(emb),
        "meta": meta.as_dict(),
        "write_traces": write_traces,
        "call_traces": call_traces,
    }
