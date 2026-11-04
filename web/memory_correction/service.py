# -*- coding: utf-8 -*-
"""Propose / confirm / revoke / replay against memory_rag.store (in-process)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pipeline.block_cross_table.memory_rag.constants import (
    STATUS_REVOKED,
    TABLE_BINDING,
    TRACE_REVOKE,
    TRACE_WRITE,
)
from pipeline.block_cross_table.memory_rag.store import (
    insert_binding_confirmed,
    transition_lifecycle,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_trace(
    conn: sqlite3.Connection,
    *,
    kind: str,
    body: dict[str, Any],
) -> str:
    tid = str(uuid4())
    conn.execute(
        """
        INSERT INTO memory_edit_trace(trace_id, kind, body_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (tid, kind, json.dumps(body, ensure_ascii=False), _now()),
    )
    conn.commit()
    return tid


def binding_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {TABLE_BINDING}").fetchone()[0])


def list_bindings(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """
    List bindings. status filters lifecycle_status when provided
    (active / revoked / invalidated). None or empty / 'all' → no filter.
    """
    sql = f"""
        SELECT binding_id, utterance_span_fingerprint, binding_payload,
               lifecycle_status, confirmed_at, source_trace_id
        FROM {TABLE_BINDING}
    """
    params: list[Any] = []
    st = (status or "").strip().lower()
    if st and st not in ("all", "*"):
        sql += " WHERE lifecycle_status=?"
        params.append(st)
    sql += " ORDER BY confirmed_at DESC"
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        payload = r[2]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                pass
        out.append(
            {
                "binding_id": r[0],
                "utterance_span_fingerprint": r[1],
                "binding_payload": payload,
                "lifecycle_status": r[3],
                "confirmed_at": r[4],
                "source_trace_id": r[5],
            }
        )
    return out


def list_proposals(
    conn: sqlite3.Connection,
    *,
    status: str = "proposed",
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT proposal_id, payload_json, created_at, status,
               COALESCE(source_type, 'WIZARD') AS source_type
        FROM memory_edit_proposal
        WHERE status=?
        ORDER BY created_at DESC
        """,
        (status,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r[1])
        except Exception:
            payload = {"raw": r[1]}
        out.append(
            {
                "proposal_id": r[0],
                "payload": payload,
                "created_at": r[2],
                "status": r[3],
                "source_type": r[4],
            }
        )
    return out


def list_traces(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 500))
    rows = conn.execute(
        """
        SELECT trace_id, kind, body_json, created_at
        FROM memory_edit_trace
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (lim,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            body = json.loads(r[2])
        except Exception:
            body = {"raw": r[2]}
        out.append(
            {
                "trace_id": r[0],
                "kind": r[1],
                "body": body,
                "created_at": r[3],
            }
        )
    return out


def discard_proposal(conn: sqlite3.Connection, *, proposal_id: str) -> dict[str, Any]:
    """Drop a pending proposal without writing a binding."""
    row = conn.execute(
        """
        SELECT status FROM memory_edit_proposal WHERE proposal_id=?
        """,
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise ValueError("proposal_not_found")
    if row[0] != "proposed":
        raise ValueError("proposal_not_proposed")
    before = binding_count(conn)
    conn.execute(
        "DELETE FROM memory_edit_proposal WHERE proposal_id=? AND status='proposed'",
        (proposal_id,),
    )
    conn.commit()
    after = binding_count(conn)
    if after != before:
        raise RuntimeError("discard must not change binding count")
    return {
        "proposal_id": proposal_id,
        "status": "discarded",
        "binding_count_unchanged": True,
    }


def propose_memory_edit(
    conn: sqlite3.Connection,
    *,
    utterance_span_fingerprint: str,
    binding_payload: dict[str, Any] | str,
    scope: dict[str, Any] | str | None = None,
    source_type: str | None = None,
) -> dict[str, Any]:
    """Create proposal only — does NOT write MemoryBindingRecord."""
    from pipeline.block_cross_table.memory_rag.constants import (
        SOURCE_CHOICES,
        SOURCE_WIZARD,
    )

    before = binding_count(conn)
    proposal_id = str(uuid4())
    src = str(source_type or SOURCE_WIZARD).strip() or SOURCE_WIZARD
    if src not in SOURCE_CHOICES:
        src = SOURCE_WIZARD
    payload = {
        "utterance_span_fingerprint": utterance_span_fingerprint,
        "binding_payload": binding_payload,
        "scope": scope if scope is not None else {},
        "source_type": src,
    }
    conn.execute(
        """
        INSERT INTO memory_edit_proposal(
            proposal_id, payload_json, created_at, status, source_type
        )
        VALUES (?, ?, ?, 'proposed', ?)
        """,
        (proposal_id, json.dumps(payload, ensure_ascii=False), _now(), src),
    )
    conn.commit()
    after = binding_count(conn)
    if after != before:
        raise RuntimeError("propose must not change binding count")
    return {
        "proposal_id": proposal_id,
        "status": "proposed",
        "source_type": src,
        "binding_count_unchanged": True,
        "before": before,
        "after": after,
    }


def confirm_memory_edit(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT payload_json, status FROM memory_edit_proposal WHERE proposal_id=?
        """,
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise ValueError("proposal_not_found")
    if row[1] != "proposed":
        raise ValueError("proposal_not_proposed")
    payload = json.loads(row[0])
    source_trace_id = "web_confirm_%s" % proposal_id
    bid, traces = insert_binding_confirmed(
        conn,
        user_confirmed=True,
        utterance_span_fingerprint=str(payload["utterance_span_fingerprint"]),
        binding_payload=payload["binding_payload"],
        source_trace_id=source_trace_id,
        confirmed_at=_now(),
        scope=payload.get("scope") or {},
    )
    conn.execute(
        """
        UPDATE memory_edit_proposal SET status='confirmed' WHERE proposal_id=?
        """,
        (proposal_id,),
    )
    conn.commit()
    # M5-CPU-Merge: mirror confirmed term/description into filemate_jargons + FTS.
    try:
        bp = payload.get("binding_payload") or {}
        if isinstance(bp, str):
            bp = json.loads(bp)
        term = str((bp or {}).get("term") or "").strip()
        meaning = str((bp or {}).get("description") or "").strip()
        scope_text = ""
        if isinstance(bp, dict):
            scope_text = str(bp.get("scope") or "")
        if term and meaning:
            from pipeline.block_cross_table.memory_rag.fts_index import backfill_bindings_fts
            from pipeline.block_cross_table.memory_rag.jargon_store import (
                upsert_filemate_jargon,
            )

            upsert_filemate_jargon(
                conn,
                content=term,
                meaning=meaning,
                scope=scope_text,
                binding_id=str(bid),
                source="confirmed",
            )
            backfill_bindings_fts(conn)
    except Exception:
        pass
    tid = _append_trace(
        conn,
        kind=TRACE_WRITE,
        body={
            "proposal_id": proposal_id,
            "binding_id": bid,
            "store_traces": traces,
        },
    )
    return {
        "proposal_id": proposal_id,
        "binding_id": bid,
        "trace_id": tid,
        "kind": TRACE_WRITE,
    }


def revoke_memory_edit(
    conn: sqlite3.Connection,
    *,
    binding_id: str,
) -> dict[str, Any]:
    traces = transition_lifecycle(
        conn, binding_id=binding_id, new_status=STATUS_REVOKED
    )
    tid = _append_trace(
        conn,
        kind=TRACE_REVOKE,
        body={"binding_id": binding_id, "store_traces": traces},
    )
    return {"binding_id": binding_id, "trace_id": tid, "kind": TRACE_REVOKE}


def replay_memory_edit(
    conn: sqlite3.Connection,
    *,
    trace_id: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT kind, body_json, created_at FROM memory_edit_trace WHERE trace_id=?
        """,
        (trace_id,),
    ).fetchone()
    if row is None:
        raise ValueError("trace_not_found")
    body = json.loads(row[1])
    return {
        "trace_id": trace_id,
        "kind": row[0],
        "body": body,
        "created_at": row[2],
        "replay": True,
    }


def purge_local_test_data(conn: sqlite3.Connection) -> dict[str, Any]:
    """Clear FileMate-local learning residue; MaiBot untouched; settings kept."""
    from pipeline.block_cross_table.memory_rag.purge_local import (
        purge_filemate_local_memory,
    )

    return purge_filemate_local_memory(conn)
