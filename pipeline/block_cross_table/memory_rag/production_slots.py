# -*- coding: utf-8 -*-
"""Production numeric slot reader for memory_rag (CURRENT only; no test inject)."""
from __future__ import annotations

from typing import Any

from pipeline.block_cross_table.memory_rag.errors import (
    LOCUS_PRODUCTION_NUMERIC_NULL,
    MemoryRagError,
)
from pipeline.synth_contract.registry import load_contract


# Keys that must remain absent/null in production until merge batch.
MEMORY_NUMERIC_SLOT_KEYS = (
    "memory_recall_top_k",
    "embedding_dim",
    "embedded_model_id",
)


def load_memory_numeric_slots_from_current() -> dict[str, Any]:
    """Production path: CURRENT cross_table only. Null until merge batch."""
    cv = load_contract()
    ct = cv.data.get("cross_table") or {}
    out: dict[str, Any] = {}
    for key in MEMORY_NUMERIC_SLOT_KEYS:
        out[key] = ct.get(key)  # absent → None
    return out


def require_production_embedding_dim() -> int:
    """Production cannot run vec layout until CURRENT merge fills embedding_dim."""
    slots = load_memory_numeric_slots_from_current()
    dim = slots.get("embedding_dim")
    if dim is None:
        raise MemoryRagError(
            "production embedding_dim is null; CURRENT merge required",
            fail_locus=LOCUS_PRODUCTION_NUMERIC_NULL,
            subreason="embedding_dim_null",
        )
    return int(dim)


def require_production_recall_top_k() -> int:
    slots = load_memory_numeric_slots_from_current()
    top_k = slots.get("memory_recall_top_k")
    if top_k is None:
        raise MemoryRagError(
            "production memory_recall_top_k is null; CURRENT merge required",
            fail_locus=LOCUS_PRODUCTION_NUMERIC_NULL,
            subreason="memory_recall_top_k_null",
        )
    return int(top_k)
