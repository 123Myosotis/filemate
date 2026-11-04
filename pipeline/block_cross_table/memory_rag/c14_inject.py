# -*- coding: utf-8 -*-
"""C.14 reconfirm inject: E / QR / QP — accepted only; local does not decide."""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from pipeline.block_cross_table.memory_rag.constants import TRACE_C14_INJECT
from pipeline.block_cross_table.memory_rag.errors import MemoryRagError

C14_TARGETS = frozenset({"E", "QR", "QP"})


def inject_c14_reference(
    *,
    target: str,
    accepted_candidates: Sequence[Mapping[str, Any]],
    top_k: int,
    context_summary: str | None = None,
) -> dict[str, Any]:
    """
    Inject top-k accepted recall candidates as reference material.
    Rejected list must not be injected for judgment.
    """
    t = str(target).upper()
    if t not in C14_TARGETS:
        raise MemoryRagError(
            "unknown C14 inject target",
            fail_locus="memory_c14_inject",
            subreason="bad_target",
            detail={"target": target},
        )
    refs = [dict(x) for x in list(accepted_candidates)[: int(top_k)]]
    traces = [
        {
            "kind": TRACE_C14_INJECT,
            "target": t,
            "injected_binding_ids": [r.get("binding_id") for r in refs],
            "top_k": int(top_k),
            "context_summary": context_summary,
        }
    ]
    return {
        "target": t,
        "memory_recall_candidates": refs,
        "traces": traces,
    }


def inject_c14_all_targets(
    *,
    accepted_candidates: Sequence[Mapping[str, Any]],
    top_k: int,
) -> dict[str, Any]:
    """Convenience: build inject packages for E, QR, QP."""
    out: dict[str, Any] = {}
    all_traces: list[dict[str, Any]] = []
    for target in ("E", "QR", "QP"):
        pack = inject_c14_reference(
            target=target,
            accepted_candidates=accepted_candidates,
            top_k=top_k,
        )
        out[target] = pack
        all_traces.extend(pack["traces"])
    return {"by_target": out, "traces": all_traces}
