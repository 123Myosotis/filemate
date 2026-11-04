# -*- coding: utf-8 -*-
"""Local memory recall: accepted[] + rejected[]; mechanical only."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from pipeline.block_cross_table.memory_rag.constants import (
    REJECT_INCOMPATIBLE_META,
    REJECT_LIFECYCLE_NOT_ACTIVE,
    REJECT_TOP_K_TRUNCATED,
    STATUS_ACTIVE,
    TABLE_BINDING,
    TABLE_EMBEDDING_META,
    TRACE_RECALL,
)
from pipeline.block_cross_table.memory_rag.meta import (
    EmbeddingVectorMeta,
    metas_mechanically_compatible,
)


def _load_meta(
    conn: sqlite3.Connection,
    meta_id: str | None,
) -> EmbeddingVectorMeta | None:
    if not meta_id:
        return None
    row = conn.execute(
        f"""
        SELECT model_id, endpoint_fingerprint, embedding_schema_version
        FROM {TABLE_EMBEDDING_META}
        WHERE meta_id=?
        """,
        (meta_id,),
    ).fetchone()
    if row is None:
        return None
    return EmbeddingVectorMeta(row[0], row[1], row[2])


def recall_bindings(
    conn: sqlite3.Connection,
    *,
    top_k: int,
    query_meta: EmbeddingVectorMeta | None = None,
) -> dict[str, Any]:
    """
    Recall all bindings mechanically.
    Non-active → rejected lifecycle_not_active.
    Vector signal with incompatible meta → rejected incompatible_meta.
    Accepted truncated by top_k → excess rejected top_k_truncated.
    """
    rows = conn.execute(
        f"""
        SELECT binding_id, utterance_span_fingerprint, binding_payload,
               scope, lifecycle_status, embedding_meta_ref
        FROM {TABLE_BINDING}
        """
    ).fetchall()
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows:
        bid, ufp, payload, scope, life, meta_ref = row
        if life != STATUS_ACTIVE:
            rejected.append(
                {
                    "binding_id": bid,
                    "reject_reason": REJECT_LIFECYCLE_NOT_ACTIVE,
                    "lifecycle_status": life,
                }
            )
            continue
        row_meta = _load_meta(conn, meta_ref)
        if query_meta is not None and row_meta is not None:
            if not metas_mechanically_compatible(query_meta, row_meta):
                rejected.append(
                    {
                        "binding_id": bid,
                        "reject_reason": REJECT_INCOMPATIBLE_META,
                        "embedding_meta_ref": meta_ref,
                    }
                )
                continue
        candidate = {
            "binding_id": bid,
            "utterance_span_fingerprint": ufp,
            "mechanical_signals": {
                "lifecycle_status": life,
                "scope_present": bool(scope),
                "has_embedding_meta": row_meta is not None,
            },
            "embedding_meta_ref": meta_ref,
            "binding_payload": payload,
        }
        if row_meta is not None:
            candidate["embedding_meta"] = row_meta.as_dict()
        accepted.append(candidate)

    if len(accepted) > int(top_k):
        keep = accepted[: int(top_k)]
        for extra in accepted[int(top_k) :]:
            rejected.append(
                {
                    "binding_id": extra["binding_id"],
                    "reject_reason": REJECT_TOP_K_TRUNCATED,
                }
            )
        accepted = keep

    traces = [
        {
            "kind": TRACE_RECALL,
            "accepted": [{"binding_id": a["binding_id"]} for a in accepted],
            "rejected": list(rejected),
            "top_k": int(top_k),
        }
    ]
    return {"accepted": accepted, "rejected": rejected, "traces": traces}


def recall_by_knn(
    conn: sqlite3.Connection,
    *,
    query_embedding: list[float],
    top_k: int,
    query_meta: EmbeddingVectorMeta | None = None,
    max_distance: float | None = None,
) -> dict[str, Any]:
    """
    Vector KNN recall (M5). Mechanical lifecycle/meta filters still apply.
    max_distance: if set, farther neighbors → rejected (no silent match).
    """
    from pipeline.block_cross_table.memory_rag.vec_index import (
        binding_id_for_rowid,
        knn_rowids,
    )

    knn = knn_rowids(conn, query_embedding=query_embedding, top_k=int(top_k))
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    # Load active bindings by id for payload/meta.
    by_id: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        f"""
        SELECT binding_id, utterance_span_fingerprint, binding_payload,
               scope, lifecycle_status, embedding_meta_ref
        FROM {TABLE_BINDING}
        """
    ).fetchall():
        by_id[str(row[0])] = {
            "binding_id": row[0],
            "utterance_span_fingerprint": row[1],
            "binding_payload": row[2],
            "scope": row[3],
            "lifecycle_status": row[4],
            "embedding_meta_ref": row[5],
        }

    for rowid, distance in knn:
        bid = binding_id_for_rowid(conn, rowid)
        if not bid or bid not in by_id:
            rejected.append(
                {
                    "rowid": rowid,
                    "reject_reason": "unmapped_vec_row",
                    "distance": distance,
                }
            )
            continue
        item = by_id[bid]
        if item["lifecycle_status"] != STATUS_ACTIVE:
            rejected.append(
                {
                    "binding_id": bid,
                    "reject_reason": REJECT_LIFECYCLE_NOT_ACTIVE,
                    "distance": distance,
                }
            )
            continue
        row_meta = _load_meta(conn, item["embedding_meta_ref"])
        if query_meta is not None and row_meta is not None:
            if not metas_mechanically_compatible(query_meta, row_meta):
                rejected.append(
                    {
                        "binding_id": bid,
                        "reject_reason": REJECT_INCOMPATIBLE_META,
                        "distance": distance,
                    }
                )
                continue
        if max_distance is not None and float(distance) > float(max_distance):
            rejected.append(
                {
                    "binding_id": bid,
                    "reject_reason": "distance_above_threshold",
                    "distance": distance,
                    "max_distance": float(max_distance),
                }
            )
            continue
        candidate = {
            "binding_id": bid,
            "utterance_span_fingerprint": item["utterance_span_fingerprint"],
            "mechanical_signals": {
                "lifecycle_status": item["lifecycle_status"],
                "scope_present": bool(item["scope"]),
                "has_embedding_meta": row_meta is not None,
                "distance": distance,
            },
            "embedding_meta_ref": item["embedding_meta_ref"],
            "binding_payload": item["binding_payload"],
            "distance": distance,
        }
        if row_meta is not None:
            candidate["embedding_meta"] = row_meta.as_dict()
        accepted.append(candidate)

    traces = [
        {
            "kind": TRACE_RECALL,
            "mode": "knn",
            "accepted": [
                {"binding_id": a["binding_id"], "distance": a.get("distance")}
                for a in accepted
            ],
            "rejected": list(rejected),
            "top_k": int(top_k),
            "max_distance": max_distance,
        }
    ]
    return {"accepted": accepted, "rejected": rejected, "traces": traces}
