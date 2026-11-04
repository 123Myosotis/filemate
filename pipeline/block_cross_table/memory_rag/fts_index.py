# -*- coding: utf-8 -*-
"""FTS5 sparse index over memory bindings (bindings_fts)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from pipeline.block_cross_table.memory_rag.constants import (
    STATUS_ACTIVE,
    TABLE_BINDING,
    TABLE_BINDINGS_FTS,
)


def _payload_search_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return payload.strip()
    if not isinstance(payload, dict):
        return str(payload)
    parts: list[str] = []
    for key in ("term", "description", "scope"):
        val = payload.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            parts.append(text)
    values = payload.get("values")
    if isinstance(values, (list, tuple)):
        for item in values:
            text = str(item).strip()
            if text:
                parts.append(text)
    return " ".join(parts)


def ensure_bindings_fts(conn: sqlite3.Connection) -> None:
    """Create FTS5 virtual table + sync triggers (idempotent)."""
    conn.executescript(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE_BINDINGS_FTS}
        USING fts5(
            binding_id UNINDEXED,
            content,
            tokenize='unicode61'
        );

        CREATE TRIGGER IF NOT EXISTS memory_binding_fts_ai
        AFTER INSERT ON {TABLE_BINDING}
        BEGIN
            INSERT INTO {TABLE_BINDINGS_FTS}(binding_id, content)
            VALUES (
                new.binding_id,
                COALESCE(new.binding_payload, '') || ' ' || COALESCE(new.scope, '')
            );
        END;

        CREATE TRIGGER IF NOT EXISTS memory_binding_fts_ad
        AFTER DELETE ON {TABLE_BINDING}
        BEGIN
            DELETE FROM {TABLE_BINDINGS_FTS} WHERE binding_id = old.binding_id;
        END;

        CREATE TRIGGER IF NOT EXISTS memory_binding_fts_au
        AFTER UPDATE OF binding_payload, scope, lifecycle_status ON {TABLE_BINDING}
        BEGIN
            DELETE FROM {TABLE_BINDINGS_FTS} WHERE binding_id = new.binding_id;
            INSERT INTO {TABLE_BINDINGS_FTS}(binding_id, content)
            VALUES (
                new.binding_id,
                COALESCE(new.binding_payload, '') || ' ' || COALESCE(new.scope, '')
            );
        END;
        """
    )
    conn.commit()
    backfill_bindings_fts(conn)


def backfill_bindings_fts(conn: sqlite3.Connection) -> int:
    """Rebuild FTS rows from active bindings; returns inserted count."""
    conn.execute(f"DELETE FROM {TABLE_BINDINGS_FTS}")
    rows = conn.execute(
        f"""
        SELECT binding_id, binding_payload, scope
        FROM {TABLE_BINDING}
        WHERE lifecycle_status = ?
        """,
        (STATUS_ACTIVE,),
    ).fetchall()
    n = 0
    for row in rows:
        bid, payload, scope = row[0], row[1], row[2]
        text = _payload_search_text(payload)
        if scope:
            text = (text + " " + str(scope)).strip()
        conn.execute(
            f"INSERT INTO {TABLE_BINDINGS_FTS}(binding_id, content) VALUES (?, ?)",
            (bid, text),
        )
        n += 1
    conn.commit()
    return n


def sparse_search_bindings(
    conn: sqlite3.Connection,
    *,
    query: str,
    top_k: int,
) -> list[dict[str, Any]]:
    """
    FTS5 BM25-style sparse recall. Lower bm25 rank is better in SQLite FTS5;
    we invert to a positive score for RRF ranking lists.
    """
    q = (query or "").strip()
    if not q or int(top_k) <= 0:
        return []
    # Quote tokens for FTS5; fall back to raw if empty after sanitize.
    tokens = [t for t in q.replace('"', " ").split() if t]
    if not tokens:
        return []
    match = " ".join(f'"{t}"' for t in tokens)
    try:
        # bm25() requires the FTS table name (aliases are rejected by SQLite).
        rows = conn.execute(
            f"""
            SELECT b.binding_id, b.binding_payload, b.scope,
                   bm25({TABLE_BINDINGS_FTS}) AS bm25_rank
            FROM {TABLE_BINDINGS_FTS}
            JOIN {TABLE_BINDING} AS b
              ON b.binding_id = {TABLE_BINDINGS_FTS}.binding_id
            WHERE {TABLE_BINDINGS_FTS} MATCH ?
              AND b.lifecycle_status = ?
            ORDER BY bm25_rank
            LIMIT ?
            """,
            (match, STATUS_ACTIVE, int(top_k)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows, start=1):
        bm25_rank = float(row[3] if row[3] is not None else i)
        # Convert FTS5 bm25 (lower better) to a descending score.
        score = 1.0 / (1.0 + max(0.0, bm25_rank))
        payload = row[1]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                pass
        out.append(
            {
                "binding_id": row[0],
                "binding_payload": payload,
                "scope": row[2],
                "score": score,
                "rank": i,
                "source": "sparse_fts",
                "bm25_rank": bm25_rank,
            }
        )
    return out
