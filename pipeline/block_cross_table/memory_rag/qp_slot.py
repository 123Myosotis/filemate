# -*- coding: utf-8 -*-
"""QP extension: memory_recall_candidates + recalled_memories slots."""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from pipeline.block_cross_table.memory_rag.qp_memory_inject import (
    normalize_recalled_memories,
)
from pipeline.block_cross_table.query_engine.qp import assemble_qp_input


def assemble_qp_input_with_memory_candidates(
    *,
    utterance: str,
    portraits: list[dict[str, Any]],
    catalog: dict[str, Any],
    ask_history: list[dict[str, Any]] | None = None,
    confirmed_bindings: list[dict[str, Any]] | None = None,
    query_time_basis_candidates: list[str] | None = None,
    memory_recall_candidates: Sequence[Mapping[str, Any]] | None = None,
    recalled_memories: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Wrap assemble_qp_input; add memory slots without altering
    existing envelope fields' semantics.

    - memory_recall_candidates: C14 raw candidates (legacy)
    - recalled_memories: high-priority jargon/binding material for QP prompt
    """
    env = assemble_qp_input(
        utterance=utterance,
        portraits=portraits,
        catalog=catalog,
        ask_history=ask_history,
        confirmed_bindings=confirmed_bindings,
        query_time_basis_candidates=query_time_basis_candidates,
    )
    raw = [dict(x) for x in list(memory_recall_candidates or [])]
    env["memory_recall_candidates"] = raw
    if recalled_memories is not None:
        env["recalled_memories"] = [dict(x) for x in list(recalled_memories)]
    else:
        env["recalled_memories"] = normalize_recalled_memories(raw)
    return env
