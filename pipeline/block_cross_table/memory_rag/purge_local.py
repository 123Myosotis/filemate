# -*- coding: utf-8 -*-
"""
Purge FileMate-local memory learning data only.
Never touches MaiBot RO paths or files. Keeps settings_kv + backend_switch.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from pipeline.block_cross_table.memory_rag.constants import (
    TABLE_BINDING,
    TABLE_BINDINGS_FTS,
    TABLE_CALL_COUNTER,
    TABLE_DIALOGUE_SESSION,
    TABLE_DIALOGUE_TURN,
    TABLE_EGRESS_AUDIT,
    TABLE_EMBEDDING_META,
    TABLE_FILEMATE_JARGONS,
    TABLE_JARGON_CONFLICTS,
    TABLE_JARGON_MINING_JOBS,
    TABLE_JARGON_TERM_STATS,
    TABLE_REEMBED_QUEUE,
    TABLE_SETTINGS_KV,
    TABLE_VEC,
    TABLE_VECTOR_CACHE,
)
from pipeline.block_cross_table.memory_rag.fts_index import ensure_bindings_fts
from pipeline.block_cross_table.memory_rag.vec_index import (
    ensure_vec0,
    load_sqlite_vec,
    stored_embedding_dim,
)


# Ordered for FK-safe clears. Settings / backend switch intentionally omitted.
_PURGE_DELETE_ORDER: tuple[str, ...] = (
    "memory_edit_proposal",
    "memory_edit_trace",
    TABLE_JARGON_TERM_STATS,
    TABLE_JARGON_MINING_JOBS,
    TABLE_JARGON_CONFLICTS,
    TABLE_FILEMATE_JARGONS,
    TABLE_DIALOGUE_TURN,
    TABLE_DIALOGUE_SESSION,
    TABLE_EGRESS_AUDIT,
    TABLE_REEMBED_QUEUE,
    TABLE_VECTOR_CACHE,
    "memory_vec_row_map",
    TABLE_BINDING,
    TABLE_EMBEDDING_META,
)

_KEEP_TABLES: frozenset[str] = frozenset(
    {
        TABLE_SETTINGS_KV,
        "memory_backend_switch",
        TABLE_CALL_COUNTER,
        "memory_vec_layout",
        "sqlite_sequence",
    }
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection, name: str) -> int:
    if not _table_exists(conn, name):
        return 0
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
    except sqlite3.Error:
        return 0


def purge_filemate_local_memory(conn: sqlite3.Connection) -> dict[str, Any]:
    """
    One-shot clear of FileMate local memory learning / test residue.
    Does not open or modify any MaiBot database.
    """
    before: dict[str, int] = {}
    for name in _PURGE_DELETE_ORDER:
        before[name] = _count(conn, name)
    before[TABLE_BINDINGS_FTS] = _count(conn, TABLE_BINDINGS_FTS)
    before[TABLE_VEC] = _count(conn, TABLE_VEC)

    deleted: dict[str, int] = {}
    for name in _PURGE_DELETE_ORDER:
        if not _table_exists(conn, name):
            deleted[name] = 0
            continue
        cur = conn.execute(f"DELETE FROM {name}")
        deleted[name] = int(cur.rowcount if cur.rowcount is not None else 0)

    if _table_exists(conn, TABLE_BINDINGS_FTS):
        try:
            conn.execute(f"DELETE FROM {TABLE_BINDINGS_FTS}")
        except sqlite3.Error:
            conn.execute(f"DROP TABLE IF EXISTS {TABLE_BINDINGS_FTS}")
            ensure_bindings_fts(conn)

    dim_before = stored_embedding_dim(conn)
    if _table_exists(conn, TABLE_VEC):
        try:
            conn.execute(f"DELETE FROM {TABLE_VEC}")
        except sqlite3.Error:
            # Virtual vec0 may reject DELETE; drop then recreate below.
            conn.execute(f"DROP TABLE IF EXISTS {TABLE_VEC}")

    if _table_exists(conn, TABLE_CALL_COUNTER):
        conn.execute(
            f"UPDATE {TABLE_CALL_COUNTER} SET call_count=0 WHERE singleton=1"
        )

    conn.commit()
    ensure_bindings_fts(conn)
    # Recreate vec0 so post-purge writes/recall never hit a missing virtual table.
    if dim_before is not None:
        try:
            load_sqlite_vec(conn)
            ensure_vec0(conn, embedding_dim=int(dim_before))
        except Exception:
            pass

    after: dict[str, int] = {n: _count(conn, n) for n in before}
    settings_kept = _count(conn, TABLE_SETTINGS_KV)
    return {
        "ok": True,
        "scope": "filemate_local_only",
        "maibot_untouched": True,
        "settings_rows_kept": settings_kept,
        "kept_tables": sorted(_KEEP_TABLES),
        "before": before,
        "deleted": deleted,
        "after": after,
        "pending_proposals_remaining": _count(conn, "memory_edit_proposal"),
    }
