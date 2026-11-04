# -*- coding: utf-8 -*-
"""
Build recalled_memories for QP from hybrid_recall (no local synonym expansion).
Local code only packages high-priority reference material; QP/LLM decides.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Sequence

from pipeline.block_cross_table.memory_rag.hybrid_recall import hybrid_recall
from pipeline.block_cross_table.memory_rag.merge_settings import (
    MergeRuntimeSettings,
    load_merge_runtime_settings,
)


def _payload_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            obj = json.loads(payload)
            if isinstance(obj, dict):
                return obj
        except Exception:
            return {"description": payload}
    return {}


def normalize_recalled_memory_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """
    Normalize one hybrid_recall accepted row into QP recalled_memories item.
    Prefer binding_payload.term/description; else jargon content/meaning.
    """
    payload = _payload_dict(row.get("binding_payload"))
    term = str(payload.get("term") or row.get("content") or "").strip()
    description = str(
        payload.get("description")
        or payload.get("meaning")
        or row.get("meaning")
        or ""
    ).strip()
    if not term and not description:
        return None
    values = payload.get("values")
    if not isinstance(values, list):
        values = []
    return {
        "term": term,
        "description": description,
        "values": [str(v) for v in values if str(v).strip()],
        "binding_id": row.get("binding_id"),
        "source": str(row.get("source") or "hybrid_recall"),
        "origin": str(row.get("origin") or ""),
        "score": row.get("rrf_score", row.get("score")),
        "memory_ref": str(
            row.get("binding_id")
            or row.get("id")
            or term
            or description
        ),
    }


def normalize_recalled_memories(
    accepted: Sequence[Mapping[str, Any]] | None,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Deduplicate by term (casefold); preserve recall order."""
    lim = 20 if limit is None else max(0, int(limit))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in list(accepted or []):
        item = normalize_recalled_memory_row(row)
        if item is None:
            continue
        key = (item.get("term") or item.get("description") or "").casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= lim:
            break
    return out


def recall_memories_for_qp(
    conn: sqlite3.Connection,
    *,
    utterance: str,
    settings: MergeRuntimeSettings | None = None,
    query_embedding: Sequence[float] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """
    Run hybrid_recall and package recalled_memories for QP inject.
    Does not expand inclusion/exclusion locally.
    """
    settings = settings or load_merge_runtime_settings()
    result = hybrid_recall(
        conn,
        query=utterance,
        query_embedding=query_embedding,
        settings=settings,
        auto_local_embed=bool((settings.embedded_model_path or "").strip()),
    )
    memories = normalize_recalled_memories(
        result.get("accepted") or [], limit=limit
    )
    return {
        "recalled_memories": memories,
        "retrieval_mode": result.get("retrieval_mode"),
        "sparse_only": result.get("sparse_only"),
        "hybrid_traces": result.get("traces") or [],
    }
