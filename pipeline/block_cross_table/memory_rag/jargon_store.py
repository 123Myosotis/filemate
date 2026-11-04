# -*- coding: utf-8 -*-
"""Local filemate_jargons persistence + confirm-to-binding bridge."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pipeline.block_cross_table.memory_rag.constants import TABLE_FILEMATE_JARGONS
from pipeline.block_cross_table.memory_rag.fts_index import backfill_bindings_fts


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_filemate_jargon(
    conn: sqlite3.Connection,
    *,
    content: str,
    meaning: str,
    count: int = 0,
    scope: str = "",
    is_global: bool = False,
    binding_id: str | None = None,
    source: str = "confirmed",
) -> str:
    term = str(content or "").strip()
    meaning_s = str(meaning or "").strip()
    scope_s = str(scope or "")
    if not term or not meaning_s:
        raise ValueError("content and meaning required")
    existing = conn.execute(
        f"""
        SELECT jargon_id FROM {TABLE_FILEMATE_JARGONS}
        WHERE content = ? AND scope = ?
        """,
        (term, scope_s),
    ).fetchone()
    now = _now()
    if existing:
        jid = existing[0]
        conn.execute(
            f"""
            UPDATE {TABLE_FILEMATE_JARGONS}
            SET meaning = ?, count = ?, is_global = ?, binding_id = COALESCE(?, binding_id),
                source = ?, updated_at = ?
            WHERE jargon_id = ?
            """,
            (
                meaning_s,
                int(count),
                1 if is_global else 0,
                binding_id,
                source,
                now,
                jid,
            ),
        )
    else:
        jid = str(uuid4())
        conn.execute(
            f"""
            INSERT INTO {TABLE_FILEMATE_JARGONS}(
                jargon_id, content, meaning, count, scope, is_global,
                binding_id, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                jid,
                term,
                meaning_s,
                int(count),
                scope_s,
                1 if is_global else 0,
                binding_id,
                source,
                now,
                now,
            ),
        )
    conn.commit()
    return jid


def fetch_filemate_jargon_by_content(
    conn: sqlite3.Connection, content: str
) -> list[dict[str, Any]]:
    """Exact content match against local filemate_jargons (dedup peer of MaiBot)."""
    term = str(content or "").strip()
    if not term:
        return []
    rows = conn.execute(
        f"""
        SELECT jargon_id, content, meaning, count, scope, is_global,
               binding_id, source, created_at, updated_at
        FROM {TABLE_FILEMATE_JARGONS}
        WHERE content = ?
        ORDER BY updated_at DESC
        """,
        (term,),
    ).fetchall()
    return [
        {
            "jargon_id": r[0],
            "content": r[1],
            "meaning": r[2],
            "count": r[3],
            "scope": r[4],
            "is_global": bool(r[5]),
            "binding_id": r[6],
            "source": r[7],
            "created_at": r[8],
            "updated_at": r[9],
            "match_source": "filemate",
        }
        for r in rows
    ]


def list_filemate_jargons(conn: sqlite3.Connection, *, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT jargon_id, content, meaning, count, scope, is_global,
               binding_id, source, created_at, updated_at
        FROM {TABLE_FILEMATE_JARGONS}
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    return [
        {
            "jargon_id": r[0],
            "content": r[1],
            "meaning": r[2],
            "count": r[3],
            "scope": r[4],
            "is_global": bool(r[5]),
            "binding_id": r[6],
            "source": r[7],
            "created_at": r[8],
            "updated_at": r[9],
        }
        for r in rows
    ]


def confirm_jargon_with_binding(
    conn: sqlite3.Connection,
    *,
    content: str,
    meaning: str,
    scope: str = "",
    count: int = 0,
    source_trace_id: str | None = None,
    utterance_span_fingerprint: str | None = None,
) -> dict[str, Any]:
    """
    User-confirmed path: write memory_binding + filemate_jargons + refresh FTS.
    Does not call external embed (caller / later batch may attach vec).
    """
    from pipeline.block_cross_table.memory_rag.store import insert_binding_confirmed

    term = str(content or "").strip()
    meaning_s = str(meaning or "").strip()
    payload = {
        "term": term,
        "description": meaning_s,
        "values": [],
        "scope": scope or "",
    }
    bid, _traces = insert_binding_confirmed(
        conn,
        user_confirmed=True,
        utterance_span_fingerprint=utterance_span_fingerprint or f"jargon:{term}",
        binding_payload=payload,
        source_trace_id=source_trace_id or f"jargon_confirm:{uuid4()}",
        confirmed_at=_now(),
        scope={"text": scope or ""},
    )
    jargon_id = upsert_filemate_jargon(
        conn,
        content=term,
        meaning=meaning_s,
        count=count,
        scope=scope or "",
        binding_id=str(bid),
        source="confirmed",
    )
    backfill_bindings_fts(conn)
    return {"binding_id": bid, "jargon_id": jargon_id, "payload": payload}
