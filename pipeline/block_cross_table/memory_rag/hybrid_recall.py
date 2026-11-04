# -*- coding: utf-8 -*-
"""
Hybrid recall: FTS5 sparse + dual-DB jargon + at most one dense (XOR) + weighted_rrf.

Arch v2: dense_provider off|local|external — never both dense channels.
FTS+jargon always kept. No cross-provider fallback. No new hybrid_parallel.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Sequence
from uuid import uuid4

from pipeline.block_cross_table.memory_rag.constants import (
    TABLE_FILEMATE_JARGONS,
    TRACE_HYBRID_RECALL,
)
from pipeline.block_cross_table.memory_rag.dense_provider import (
    CHANNEL_EXTERNAL,
    CHANNEL_LOCAL,
    CHANNEL_SPARSE,
    DENSE_EXTERNAL,
    DENSE_LOCAL,
    DENSE_OFF,
    MODE_SPARSE_ONLY,
    assert_at_most_one_dense,
    assert_writable_retrieval_mode,
    effective_external_attempt_allowed,
    normalize_dense_provider,
    pick_fallback_state,
    resolve_dense_provider,
    retrieval_mode_for_state,
)
from pipeline.block_cross_table.memory_rag.external_call import HttpTransport
from pipeline.block_cross_table.memory_rag.external_circuit import (
    CaseExternalEmbedSidecar,
    get_default_sidecar,
)
from pipeline.block_cross_table.memory_rag.external_settings import (
    ExternalEmbeddingSettings,
)
from pipeline.block_cross_table.memory_rag.fts_index import sparse_search_bindings
from pipeline.block_cross_table.memory_rag.maibot_ro import (
    list_jargons_for_merge,
    open_maibot_ro,
)
from pipeline.block_cross_table.memory_rag.meaning_similarity import meanings_equivalent
from pipeline.block_cross_table.memory_rag.merge_settings import (
    MergeRuntimeSettings,
    load_merge_runtime_settings,
)
from pipeline.block_cross_table.memory_rag.meta import EmbeddingVectorMeta
from pipeline.block_cross_table.memory_rag.recall import recall_by_knn
from pipeline.block_cross_table.memory_rag.weighted_rrf import dedupe_text_rows, weighted_rrf


def _now_trace(conn: sqlite3.Connection, body: dict[str, Any]) -> None:
    from datetime import datetime, timezone

    conn.execute(
        """
        INSERT INTO memory_edit_trace(trace_id, kind, body_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            str(uuid4()),
            TRACE_HYBRID_RECALL,
            json.dumps(body, ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def _filemate_jargon_rows(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT content, meaning, count, scope, is_global, binding_id
        FROM {TABLE_FILEMATE_JARGONS}
        ORDER BY count DESC, updated_at DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    return [
        {
            "content": r[0],
            "meaning": r[1],
            "count": int(r[2] or 0),
            "scope": r[3],
            "is_global": bool(r[4]),
            "binding_id": r[5],
            "origin": "filemate",
        }
        for r in rows
    ]


def merge_jargon_corpora(
    conn: sqlite3.Connection,
    *,
    settings: MergeRuntimeSettings,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Load FileMate + MaiBot jargons and dedupe by content/meaning."""
    lim = int(limit or max(settings.sparse_top_k, settings.dense_top_k) * 5)
    local = _filemate_jargon_rows(conn, limit=lim)
    remote: list[dict[str, Any]] = []
    if settings.maibot_data_dir:
        try:
            with open_maibot_ro(settings.maibot_data_dir) as mconn:
                if mconn is not None:
                    for row in list_jargons_for_merge(mconn, limit=lim):
                        remote.append(
                            {
                                "content": row.content,
                                "meaning": row.meaning,
                                "count": row.count,
                                "scope": "",
                                "is_global": row.is_global,
                                "binding_id": None,
                                "origin": "maibot",
                            }
                        )
        except Exception as exc:
            # Soft-fail merge path: local-only if RO open fails mid-query.
            remote = []
            _now_trace(
                conn,
                {
                    "event": "maibot_merge_skipped",
                    "error": str(exc),
                },
            )
    threshold = settings.meaning_similarity_threshold

    def _eq(a: str, b: str) -> bool:
        return meanings_equivalent(a, b, threshold=threshold)

    return dedupe_text_rows(
        [*local, *remote],
        content_key="content",
        meaning_key="meaning",
        equivalent=_eq,
    )


def _jargon_text_rank(
    merged: Sequence[dict[str, Any]],
    *,
    query: str,
    top_k: int,
) -> list[dict[str, Any]]:
    """Simple sparse-over-merged-text: substring / token overlap rank."""
    q = (query or "").strip().casefold()
    if not q:
        return []
    scored: list[dict[str, Any]] = []
    for row in merged:
        content = str(row.get("content") or "")
        meaning = str(row.get("meaning") or "")
        blob = f"{content} {meaning}".casefold()
        if not blob:
            continue
        hit = 0.0
        if content.casefold() in q or q in content.casefold():
            hit += 2.0
        if q in blob:
            hit += 1.0
        for tok in q.split():
            if tok and tok in blob:
                hit += 0.5
        if hit <= 0:
            continue
        item = dict(row)
        item["id"] = str(row.get("id") or content)
        item["score"] = hit + float(row.get("count") or 0) * 0.01
        item["source"] = "jargon_merge"
        scored.append(item)
    scored.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)
    return scored[: int(top_k)]


def _try_local_query_embedding(
    *,
    query: str,
    embedded_model_path: str,
) -> tuple[list[float] | None, str | None]:
    """
    Soft-load local embedding when path configured.
    Failure never blocks sparse recall (chat / FTS-first).
    """
    path = (embedded_model_path or "").strip()
    if not path or not query.strip():
        return None, None
    try:
        from pathlib import Path

        if not Path(path).exists():
            return None, "local_model_path_missing"
        from sentence_transformers import SentenceTransformer  # type: ignore

        model = SentenceTransformer(path)
        vec = model.encode([query.strip()], normalize_embeddings=True)[0]
        return [float(x) for x in list(vec)], None
    except Exception as exc:  # noqa: BLE001 — soft parallel path
        return None, f"local_embed_skipped:{type(exc).__name__}"


def _knn_channel(
    conn: sqlite3.Connection,
    *,
    emb: Sequence[float],
    top_k: int,
    query_meta: EmbeddingVectorMeta | None,
    max_distance: float | None,
    source: str,
) -> list[dict[str, Any]]:
    knn = recall_by_knn(
        conn,
        query_embedding=list(emb),
        top_k=top_k,
        query_meta=query_meta,
        max_distance=max_distance,
    )
    out: list[dict[str, Any]] = []
    for i, cand in enumerate(knn.get("accepted") or [], start=1):
        out.append(
            {
                "id": str(cand.get("binding_id") or ""),
                "binding_id": cand.get("binding_id"),
                "binding_payload": cand.get("binding_payload"),
                "score": float(cand.get("distance_or_score") or 0.0),
                "rank": i,
                "source": source,
                "embedding_meta_ref": cand.get("embedding_meta_ref"),
            }
        )
    return out


def _normalize_weights(weights: list[float]) -> list[float]:
    total = sum(float(w) for w in weights)
    if total <= 0:
        n = len(weights) or 1
        return [1.0 / n] * len(weights)
    return [float(w) / total for w in weights]


def hybrid_recall(
    conn: sqlite3.Connection,
    *,
    query: str,
    query_embedding: Sequence[float] | None = None,
    query_meta: EmbeddingVectorMeta | None = None,
    settings: MergeRuntimeSettings | None = None,
    max_distance: float | None = None,
    auto_local_embed: bool = True,  # legacy kw; ignored for decisions (XOR uses dense_provider)
    case_id: str | None = None,
    external_sidecar: CaseExternalEmbedSidecar | None = None,
    external_settings: ExternalEmbeddingSettings | None = None,
    http_transport: HttpTransport | None = None,
    external_call_counter: list[int] | None = None,
    dense_provider: str | None = None,
) -> dict[str, Any]:
    """
    Pipeline (arch v2 XOR):
      1) sparse FTS + jargon (always)
      2) at most one dense: local XOR external (soft-fail → sparse_only)
      3) weighted_rrf over available channels (dense≤1)
    """
    del auto_local_embed  # unused — decisions never read legacy auto_local_embed
    settings = settings or load_merge_runtime_settings()
    q = (query or "").strip()

    provider_raw = dense_provider
    if provider_raw is None:
        provider_raw = getattr(settings, "dense_provider", None)
    provider = normalize_dense_provider(provider_raw)
    if provider is None:
        provider = resolve_dense_provider(persist_migration=True).dense_provider

    local_embed_note: str | None = None
    local_ok: bool | None = None
    emb: Sequence[float] | None = None
    dense: list[dict[str, Any]] = []

    # Local dense only when provider=local (never when external/off).
    if provider == DENSE_LOCAL:
        emb = query_embedding
        if emb is None and (settings.embedded_model_path or "").strip():
            emb, local_embed_note = _try_local_query_embedding(
                query=q, embedded_model_path=settings.embedded_model_path
            )
        if emb is not None:
            dense = _knn_channel(
                conn,
                emb=emb,
                top_k=settings.dense_top_k,
                query_meta=query_meta,
                max_distance=max_distance,
                source="dense_knn",
            )
            local_ok = True
        else:
            local_ok = False
            if not local_embed_note:
                local_embed_note = "local_dense_unavailable"

    sparse_bind = sparse_search_bindings(
        conn, query=q, top_k=settings.sparse_top_k
    )
    for item in sparse_bind:
        item["id"] = str(item.get("binding_id") or "")

    # --- external branch: only when provider=external AND weight>0 ---
    external_audit: dict[str, Any] = {
        "external_attempted": False,
        "external_used": False,
        "skipped_reason": None,
        "circuit_open": False,
        "elapsed_ms": 0.0,
        "model_id": None,
        "embedding_dim": None,
        "external_calls": 0,
    }
    dense_external: list[dict[str, Any]] = []
    external_ok: bool | None = None
    want_external = effective_external_attempt_allowed(
        dense_provider=provider,  # type: ignore[arg-type]
        external_weight=float(settings.external_weight),
    )

    if provider != DENSE_EXTERNAL:
        # Hard: never attempt external when not in external mode.
        external_audit["external_attempted"] = False
        external_audit["external_used"] = False
    elif not want_external:
        # weight 0/empty → treat as off (no call)
        external_ok = None
        external_audit = {
            "external_attempted": False,
            "external_used": False,
            "skipped_reason": "external_weight_zero_treated_as_off",
            "circuit_open": False,
            "elapsed_ms": 0.0,
            "model_id": None,
            "embedding_dim": None,
            "external_calls": 0,
        }
    else:
        from pipeline.block_cross_table.memory_rag.external_embedding_config import (
            resolve_external_embedding,
        )
        from pipeline.block_cross_table.memory_rag.external_query_soft import (
            soft_embed_query_text,
        )

        resolved = resolve_external_embedding()
        call_settings = external_settings
        if call_settings is None:
            call_settings = resolved.as_external_settings()
        if call_settings is None:
            external_ok = False
            external_audit = {
                "external_attempted": True,
                "external_used": False,
                "skipped_reason": "missing_external_api_key"
                if not resolved.api_key_present
                else "missing_external_endpoint_or_model",
                "circuit_open": True,
                "elapsed_ms": 0.0,
                "model_id": resolved.model or None,
                "embedding_dim": None,
                "external_calls": 0,
                "api_key_present": resolved.api_key_present,
                "api_key_source": resolved.api_key_source,
            }
        else:
            cid = str(case_id or "").strip() or "_anonymous"
            soft = soft_embed_query_text(
                conn,
                text=q,
                case_id=cid,
                sidecar=external_sidecar or get_default_sidecar(),
                settings=call_settings,
                http_transport=http_transport,
                call_counter=external_call_counter,
            )
            external_audit = soft.audit_dict()
            external_audit["api_key_present"] = resolved.api_key_present
            external_audit["api_key_source"] = resolved.api_key_source
            if soft.used and soft.embedding is not None:
                dense_external = _knn_channel(
                    conn,
                    emb=soft.embedding,
                    top_k=settings.dense_top_k,
                    query_meta=soft.meta or query_meta,
                    max_distance=max_distance,
                    source="dense_knn_external",
                )
                external_ok = True
            else:
                external_ok = False

    merged_jargons = merge_jargon_corpora(conn, settings=settings)
    jargon_hits = _jargon_text_rank(
        merged_jargons, query=q, top_k=settings.sparse_top_k
    )

    sparse_channel: list[dict[str, Any]] = []
    for item in sparse_bind:
        sparse_channel.append(item)
    for item in jargon_hits:
        sparse_channel.append(item)

    lists: list[list[dict[str, Any]]] = []
    weights: list[float] = []
    channel_list: list[str] = []

    # XOR: at most one dense channel
    if provider == DENSE_LOCAL and dense:
        lists.append(dense)
        weights.append(float(settings.vector_weight))
        channel_list.append(CHANNEL_LOCAL)

    external_vector_ready = (
        provider == DENSE_EXTERNAL
        and bool(external_audit.get("external_used"))
        and not external_audit.get("skipped_reason")
    )
    if external_vector_ready:
        lists.append(dense_external)
        weights.append(float(settings.external_weight))
        channel_list.append(CHANNEL_EXTERNAL)

    if sparse_channel:
        lists.append(sparse_channel)
        weights.append(float(settings.bm25_weight))
        channel_list.append(CHANNEL_SPARSE)

    assert_at_most_one_dense(channel_list)

    if external_vector_ready and CHANNEL_EXTERNAL in channel_list:
        external_used = True
    else:
        external_used = False
        if external_audit.get("external_attempted") and external_audit.get(
            "external_used"
        ):
            external_audit["external_used"] = False
            if not external_audit.get("skipped_reason"):
                external_audit["skipped_reason"] = "not_fused_in_rrf"

    # When provider≠external, hard-force attempted false
    if provider != DENSE_EXTERNAL:
        external_audit["external_attempted"] = False
        external_used = False
        external_audit["external_used"] = False

    norm_weights = _normalize_weights(weights) if weights else []

    state = pick_fallback_state(
        dense_provider=provider,  # type: ignore[arg-type]
        external_weight=float(settings.external_weight),
        local_ok=local_ok,
        external_ok=external_ok,
    )
    # If dense fused with sparse, use success modes; else sparse_only from state machine.
    if CHANNEL_LOCAL in channel_list and CHANNEL_SPARSE in channel_list:
        retrieval_mode = retrieval_mode_for_state("local_success")
    elif CHANNEL_EXTERNAL in channel_list and CHANNEL_SPARSE in channel_list:
        retrieval_mode = retrieval_mode_for_state("external_success")
    elif CHANNEL_LOCAL in channel_list or CHANNEL_EXTERNAL in channel_list:
        # dense alone (no sparse hits) — still label as dense_*_plus_sparse family
        # but writable modes require *_plus_sparse; keep success label when dense present
        retrieval_mode = (
            retrieval_mode_for_state("local_success")
            if CHANNEL_LOCAL in channel_list
            else retrieval_mode_for_state("external_success")
        )
    else:
        retrieval_mode = retrieval_mode_for_state(state)

    retrieval_mode = assert_writable_retrieval_mode(retrieval_mode)

    if len(lists) >= 2:
        fused = weighted_rrf(
            lists,
            weights=norm_weights,
            rrf_k=settings.rrf_k,
            id_key="id",
        )
    elif lists:
        fused = lists[0]
        for i, item in enumerate(fused, start=1):
            item.setdefault("rrf_score", float(item.get("score") or 0.0))
            item["rank"] = i
    else:
        fused = []
        retrieval_mode = MODE_SPARSE_ONLY

    if retrieval_mode == MODE_SPARSE_ONLY and external_used:
        external_used = False
        external_audit["external_used"] = False
        if not external_audit.get("skipped_reason"):
            external_audit["skipped_reason"] = "not_fused_in_rrf"

    final_k = settings.hybrid_final_top_k
    accepted = fused[:final_k]
    rejected = [
        {"id": x.get("id"), "reject_reason": "hybrid_top_k_truncated"}
        for x in fused[final_k:]
    ]
    sparse_only = retrieval_mode == MODE_SPARSE_ONLY
    body = {
        "query_len": len(q),
        "dense_n": len(dense),
        "dense_external_n": len(dense_external),
        "sparse_bind_n": len(sparse_bind),
        "jargon_merge_n": len(merged_jargons),
        "jargon_hit_n": len(jargon_hits),
        "accepted_n": len(accepted),
        "retrieval_mode": retrieval_mode,
        "sparse_only": sparse_only,
        "dense_provider": provider,
        "vector_branch_used": bool(dense) or bool(dense_external),
        "local_embed_note": local_embed_note,
        "engine": settings.self_learning_engine,
        "note": (
            "无向量也能出结果（FTS + 黑话文本）"
            if sparse_only
            else "稠密一路（XOR）+ 稀疏融合"
        ),
        "fusion": {
            "rrf_k": settings.rrf_k,
            "vector_weight": settings.vector_weight,
            "bm25_weight": settings.bm25_weight,
            "external_weight": settings.external_weight,
            "dense_provider": provider,
            "channels": len(channel_list),
            "channel_list": list(channel_list),
            "rrf_weights": list(norm_weights),
        },
        "external_attempted": external_audit.get("external_attempted"),
        "external_used": external_used,
        "skipped_reason": external_audit.get("skipped_reason"),
        "elapsed_ms": external_audit.get("elapsed_ms"),
        "circuit_open": external_audit.get("circuit_open"),
        "external_model_id": external_audit.get("model_id"),
        "external_embedding_dim": external_audit.get("embedding_dim"),
        "external_calls": external_audit.get("external_calls"),
        "api_key_present": external_audit.get("api_key_present"),
        "api_key_source": external_audit.get("api_key_source"),
    }
    _now_trace(conn, body)
    return {
        "accepted": accepted,
        "rejected": rejected,
        "merged_jargons_preview": merged_jargons[:20],
        "retrieval_mode": retrieval_mode,
        "sparse_only": sparse_only,
        "dense_provider": provider,
        "vector_branch_used": bool(dense) or bool(dense_external),
        "channel_list": list(channel_list),
        "external_used": external_used,
        "external_attempted": bool(external_audit.get("external_attempted")),
        "skipped_reason": external_audit.get("skipped_reason"),
        "circuit_open": bool(external_audit.get("circuit_open")),
        "elapsed_ms": float(external_audit.get("elapsed_ms") or 0.0),
        "note": body["note"],
        "traces": [{"kind": TRACE_HYBRID_RECALL, **body}],
    }
