# -*- coding: utf-8 -*-
"""sqlite-vec vec0 wiring; dimension from caller (test overrides / future CURRENT)."""
from __future__ import annotations

import sqlite3
from typing import Any

from pipeline.block_cross_table.memory_rag.constants import TABLE_VEC
from pipeline.block_cross_table.memory_rag.errors import (
    LOCUS_DIM_MISMATCH,
    LOCUS_VEC_SETUP,
    MemoryRagError,
)
from pipeline.block_cross_table.memory_rag.meta import (
    EmbeddingVectorMeta,
    require_compatible_metas,
)


def load_sqlite_vec(conn: sqlite3.Connection) -> None:
    """Load sqlite-vec extension on this connection."""
    try:
        import sqlite_vec
    except ImportError as exc:
        raise MemoryRagError(
            "sqlite-vec dependency missing",
            fail_locus=LOCUS_VEC_SETUP,
            subreason="sqlite_vec_import",
            detail={"error": str(exc)},
        ) from exc
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception as exc:  # noqa: BLE001 — surface as fail locus
        raise MemoryRagError(
            "sqlite-vec load failed",
            fail_locus=LOCUS_VEC_SETUP,
            subreason="sqlite_vec_load",
            detail={"error": str(exc)},
        ) from exc


def ensure_vec0(
    conn: sqlite3.Connection,
    *,
    embedding_dim: int,
    table_name: str = TABLE_VEC,
) -> None:
    """
    Create vec0 virtual table with the given dimension.
    Dimension must be passed explicitly (from test overrides or merged slots).
    """
    if not isinstance(embedding_dim, int) or embedding_dim <= 0:
        raise MemoryRagError(
            "embedding_dim invalid",
            fail_locus=LOCUS_VEC_SETUP,
            subreason="invalid_embedding_dim",
            detail={"embedding_dim": embedding_dim},
        )
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    if existing:
        # Detect dimension drift via stored pragma table if we keep a side table.
        row = conn.execute(
            "SELECT value FROM memory_vec_layout WHERE key='embedding_dim'"
        ).fetchone()
        if row is not None and int(row[0]) != int(embedding_dim):
            raise MemoryRagError(
                "embedding_dim changed; rebuild index required",
                fail_locus=LOCUS_DIM_MISMATCH,
                subreason="dim_changed_rebuild_required",
                detail={"stored_dim": int(row[0]), "requested_dim": embedding_dim},
            )
        return
    conn.execute(
        f"CREATE VIRTUAL TABLE {table_name} USING vec0(embedding float[{int(embedding_dim)}])"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_vec_layout (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO memory_vec_layout(key, value) VALUES ('embedding_dim', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(int(embedding_dim)),),
    )
    conn.commit()


def stored_embedding_dim(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT value FROM memory_vec_layout WHERE key='embedding_dim'"
    ).fetchone()
    return int(row[0]) if row else None


def insert_vector_row(
    conn: sqlite3.Connection,
    *,
    rowid: int,
    embedding: list[float],
    expected_dim: int,
    query_meta: EmbeddingVectorMeta,
    row_meta: EmbeddingVectorMeta,
    table_name: str = TABLE_VEC,
) -> None:
    """
    Insert one vector only when metas compatible and length matches expected_dim.
    Refuses silent mix / wrong dim.
    """
    require_compatible_metas(query_meta, row_meta)
    if len(embedding) != int(expected_dim):
        raise MemoryRagError(
            "embedding length != embedding_dim",
            fail_locus=LOCUS_DIM_MISMATCH,
            subreason="vector_length_mismatch",
            detail={"len": len(embedding), "embedding_dim": expected_dim},
        )
    stored = stored_embedding_dim(conn)
    if stored is not None and stored != int(expected_dim):
        raise MemoryRagError(
            "embedding_dim changed; rebuild index required",
            fail_locus=LOCUS_DIM_MISMATCH,
            subreason="dim_changed_rebuild_required",
            detail={"stored_dim": stored, "requested_dim": expected_dim},
        )
    # sqlite-vec accepts JSON array string or blob; use JSON list serialization.
    import json

    conn.execute(
        f"INSERT INTO {table_name}(rowid, embedding) VALUES (?, ?)",
        (int(rowid), json.dumps([float(x) for x in embedding])),
    )
    conn.commit()


def drop_vec0_for_rebuild(
    conn: sqlite3.Connection,
    *,
    table_name: str = TABLE_VEC,
) -> list[dict[str, Any]]:
    """Drop vec0 + layout so a new dim can be applied (explicit rebuild)."""
    conn.execute(f"DROP TABLE IF EXISTS {table_name}")
    conn.execute("DROP TABLE IF EXISTS memory_vec_layout")
    conn.execute("DROP TABLE IF EXISTS memory_vec_row_map")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_vec_row_map (
            rowid INTEGER PRIMARY KEY,
            binding_id TEXT NOT NULL UNIQUE
        )
        """
    )
    conn.commit()
    return [{"kind": "memory_reembed", "action": "vec0_dropped_for_rebuild"}]


def rebuild_vec0_to_dim(
    conn: sqlite3.Connection,
    *,
    new_embedding_dim: int,
    table_name: str = TABLE_VEC,
) -> dict[str, Any]:
    """
    Explicit dim change: drop vec0 + recreate at new_embedding_dim.
    Returns rebuild log (old→new). Does not re-embed rows (caller must).
    """
    old = stored_embedding_dim(conn)
    drop_traces = drop_vec0_for_rebuild(conn, table_name=table_name)
    ensure_vec0(conn, embedding_dim=int(new_embedding_dim), table_name=table_name)
    return {
        "old_embedding_dim": old,
        "new_embedding_dim": int(new_embedding_dim),
        "action": "drop_and_rebuild_vec0",
        "traces": drop_traces,
    }


def allocate_vec_rowid(conn: sqlite3.Connection, *, binding_id: str) -> int:
    """Allocate integer rowid for vec0 and map to binding_id."""
    existing = conn.execute(
        "SELECT rowid FROM memory_vec_row_map WHERE binding_id=?",
        (binding_id,),
    ).fetchone()
    if existing:
        return int(existing[0])
    row = conn.execute("SELECT COALESCE(MAX(rowid), 0) + 1 FROM memory_vec_row_map").fetchone()
    rid = int(row[0]) if row else 1
    conn.execute(
        "INSERT INTO memory_vec_row_map(rowid, binding_id) VALUES (?, ?)",
        (rid, binding_id),
    )
    conn.commit()
    return rid


def knn_rowids(
    conn: sqlite3.Connection,
    *,
    query_embedding: list[float],
    top_k: int,
    table_name: str = TABLE_VEC,
) -> list[tuple[int, float]]:
    """
    sqlite-vec KNN: returns [(rowid, distance), ...] ascending distance.
    """
    import json

    lim = max(1, int(top_k))
    q = json.dumps([float(x) for x in query_embedding])
    rows = conn.execute(
        f"""
        SELECT rowid, distance
        FROM {table_name}
        WHERE embedding MATCH ?
          AND k = ?
        ORDER BY distance
        """,
        (q, lim),
    ).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows]


def binding_id_for_rowid(conn: sqlite3.Connection, rowid: int) -> str | None:
    row = conn.execute(
        "SELECT binding_id FROM memory_vec_row_map WHERE rowid=?",
        (int(rowid),),
    ).fetchone()
    return str(row[0]) if row else None
