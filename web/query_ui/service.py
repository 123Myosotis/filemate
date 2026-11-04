# -*- coding: utf-8 -*-
"""
P2-B UI thin wrapper around shared run_query Tool.

Core orchestration lives in
pipeline.block_cross_table.query_engine.run_query_tool.
"""
from __future__ import annotations

from typing import Any, Mapping

from pipeline.block_cross_table.query_engine.run_query_tool import (
    build_ask_history_continuation,
    load_tables_from_paths,
    run_query as _run_query_core,
)
from web.query_ui.export_store import save_result_xlsx

# Re-export for existing imports / tests
__all__ = [
    "build_ask_history_continuation",
    "load_tables_from_paths",
    "run_query",
]


def run_query(
    *,
    utterance: str,
    file_paths: list[str],
    ask_history: list[dict[str, Any]] | None = None,
    dense_provider: str | None = None,
    seed_binding_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """UI entry: same contract as shared Tool, with web export paths."""
    return _run_query_core(
        utterance=utterance,
        file_paths=file_paths,
        ask_history=ask_history,
        dense_provider=dense_provider,
        seed_binding_payload=seed_binding_payload,
        save_export=save_result_xlsx,
    )
