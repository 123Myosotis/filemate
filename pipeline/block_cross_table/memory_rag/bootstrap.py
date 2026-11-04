# -*- coding: utf-8 -*-
"""Open / migrate a memory_rag SQLite database (M2)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from pipeline.block_cross_table.memory_rag.schema import apply_core_schema
from pipeline.block_cross_table.memory_rag.vec_index import ensure_vec0, load_sqlite_vec


def open_memory_db(
    path: str | Path | None = None,
    *,
    embedding_dim: int | None = None,
    enable_vec: bool = True,
) -> sqlite3.Connection:
    """
    Open SQLite (file or :memory:), apply core schema, optionally wire vec0.

    embedding_dim must be supplied by caller when enable_vec=True
    (test overrides or production require_* — never hardcode here).
    """
    uri = ":memory:" if path is None else str(path)
    # check_same_thread=False: FastAPI may serve requests on worker threads.
    conn = sqlite3.connect(uri, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL + busy_timeout: safe for single-writer + web process-in-process access.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    apply_core_schema(conn)
    if enable_vec:
        if embedding_dim is None:
            conn.close()
            raise ValueError("embedding_dim required when enable_vec=True")
        load_sqlite_vec(conn)
        ensure_vec0(conn, embedding_dim=int(embedding_dim))
    return conn
