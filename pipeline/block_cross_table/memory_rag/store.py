# -*- coding: utf-8 -*-
"""MemoryBindingRecord persistence: confirm-before-write; lifecycle rules."""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from pipeline.block_cross_table.memory_rag.constants import (
    REEMBED_DONE,
    REEMBED_FAILED,
    REEMBED_QUEUED,
    STATUS_ACTIVE,
    STATUS_INVALIDATED,
    STATUS_REVOKED,
    STATUS_TERMINAL,
    TABLE_BINDING,
    TABLE_EMBEDDING_META,
    TABLE_REEMBED_QUEUE,
    TRACE_INVALIDATE,
    TRACE_REEMBED,
    TRACE_REVOKE,
    TRACE_WRITE,
)
from pipeline.block_cross_table.memory_rag.errors import (
    LOCUS_LIFECYCLE_ILLEGAL,
    LOCUS_WRITE_UNCONFIRMED,
    MemoryRagError,
)
from pipeline.block_cross_table.memory_rag.meta import EmbeddingVectorMeta


def _require_confirmed(
    *,
    user_confirmed: bool,
    source_trace_id: str | None,
    confirmed_at: str | None,
) -> None:
    if not user_confirmed:
        raise MemoryRagError(
            "write refused: user confirmation required",
            fail_locus=LOCUS_WRITE_UNCONFIRMED,
            subreason="user_confirmed_false",
        )
    if not source_trace_id:
        raise MemoryRagError(
            "write refused: source_trace_id required",
            fail_locus=LOCUS_WRITE_UNCONFIRMED,
            subreason="missing_source_trace_id",
        )
    if not confirmed_at:
        raise MemoryRagError(
            "write refused: confirmed_at required",
            fail_locus=LOCUS_WRITE_UNCONFIRMED,
            subreason="missing_confirmed_at",
        )


def insert_embedding_meta(
    conn: sqlite3.Connection,
    *,
    meta: EmbeddingVectorMeta,
    created_at: str,
    meta_id: str | None = None,
) -> str:
    mid = meta_id or str(uuid.uuid4())
    conn.execute(
        f"""
        INSERT INTO {TABLE_EMBEDDING_META}
            (meta_id, model_id, endpoint_fingerprint, embedding_schema_version, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            mid,
            meta.model_id,
            meta.endpoint_fingerprint,
            meta.embedding_schema_version,
            created_at,
        ),
    )
    conn.commit()
    return mid


def insert_binding_confirmed(
    conn: sqlite3.Connection,
    *,
    user_confirmed: bool,
    utterance_span_fingerprint: str,
    binding_payload: dict[str, Any] | str,
    source_trace_id: str | None,
    confirmed_at: str | None,
    scope: dict[str, Any] | str,
    embedding_meta_ref: str | None = None,
    binding_id: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Persist only after explicit user confirmation (M2 iron rule)."""
    _require_confirmed(
        user_confirmed=user_confirmed,
        source_trace_id=source_trace_id,
        confirmed_at=confirmed_at,
    )
    bid = binding_id or str(uuid.uuid4())
    payload_s = (
        binding_payload
        if isinstance(binding_payload, str)
        else json.dumps(binding_payload, ensure_ascii=False)
    )
    scope_s = scope if isinstance(scope, str) else json.dumps(scope, ensure_ascii=False)
    conn.execute(
        f"""
        INSERT INTO {TABLE_BINDING} (
            binding_id,
            utterance_span_fingerprint,
            binding_payload,
            source_trace_id,
            confirmed_at,
            scope,
            lifecycle_status,
            embedding_meta_ref
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bid,
            utterance_span_fingerprint,
            payload_s,
            source_trace_id,
            confirmed_at,
            scope_s,
            STATUS_ACTIVE,
            embedding_meta_ref,
        ),
    )
    conn.commit()
    traces = [
        {
            "kind": TRACE_WRITE,
            "binding_id": bid,
            "source_trace_id": source_trace_id,
            "lifecycle_status": STATUS_ACTIVE,
        }
    ]
    return bid, traces


def transition_lifecycle(
    conn: sqlite3.Connection,
    *,
    binding_id: str,
    new_status: str,
) -> list[dict[str, Any]]:
    """active→invalidated|revoked only; terminal→active blocked by trigger + check."""
    row = conn.execute(
        f"SELECT lifecycle_status FROM {TABLE_BINDING} WHERE binding_id=?",
        (binding_id,),
    ).fetchone()
    if row is None:
        raise MemoryRagError(
            "binding not found",
            fail_locus=LOCUS_LIFECYCLE_ILLEGAL,
            subreason="binding_missing",
            detail={"binding_id": binding_id},
        )
    old = row[0]
    if old in STATUS_TERMINAL and new_status == STATUS_ACTIVE:
        raise MemoryRagError(
            "lifecycle_status cannot return to active on same binding_id",
            fail_locus=LOCUS_LIFECYCLE_ILLEGAL,
            subreason="reactivate_forbidden",
            detail={"binding_id": binding_id, "old": old, "new": new_status},
        )
    if old != STATUS_ACTIVE or new_status not in (
        STATUS_INVALIDATED,
        STATUS_REVOKED,
    ):
        raise MemoryRagError(
            "illegal lifecycle transition",
            fail_locus=LOCUS_LIFECYCLE_ILLEGAL,
            subreason="illegal_edge",
            detail={"binding_id": binding_id, "old": old, "new": new_status},
        )
    try:
        conn.execute(
            f"""
            UPDATE {TABLE_BINDING}
            SET lifecycle_status=?
            WHERE binding_id=?
            """,
            (new_status, binding_id),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise MemoryRagError(
            "lifecycle_status cannot return to active on same binding_id",
            fail_locus=LOCUS_LIFECYCLE_ILLEGAL,
            subreason="trigger_abort",
            detail={"binding_id": binding_id, "error": str(exc)},
        ) from exc
    kind = TRACE_INVALIDATE if new_status == STATUS_INVALIDATED else TRACE_REVOKE
    return [{"kind": kind, "binding_id": binding_id, "lifecycle_status": new_status}]


def enqueue_reembed(
    conn: sqlite3.Connection,
    *,
    binding_id: str,
    old_meta_id: str | None,
    reason: str,
    enqueued_at: str,
    queue_id: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    qid = queue_id or str(uuid.uuid4())
    conn.execute(
        f"""
        INSERT INTO {TABLE_REEMBED_QUEUE}
            (queue_id, binding_id, old_meta_id, reason, enqueued_at, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (qid, binding_id, old_meta_id, reason, enqueued_at, REEMBED_QUEUED),
    )
    conn.commit()
    traces = [
        {
            "kind": TRACE_REEMBED,
            "queue_id": qid,
            "binding_id": binding_id,
            "reason": reason,
            "status": REEMBED_QUEUED,
        }
    ]
    return qid, traces


def complete_reembed_queue_item(
    conn: sqlite3.Connection,
    *,
    queue_id: str,
    status: str,
) -> list[dict[str, Any]]:
    """Mark a reembed queue item done|failed (dequeue / complete link)."""
    if status not in (REEMBED_DONE, REEMBED_FAILED):
        raise MemoryRagError(
            "illegal reembed queue completion status",
            fail_locus=LOCUS_LIFECYCLE_ILLEGAL,
            subreason="reembed_status_illegal",
            detail={"queue_id": queue_id, "status": status},
        )
    row = conn.execute(
        f"SELECT status FROM {TABLE_REEMBED_QUEUE} WHERE queue_id=?",
        (queue_id,),
    ).fetchone()
    if row is None:
        raise MemoryRagError(
            "reembed queue item missing",
            fail_locus=LOCUS_LIFECYCLE_ILLEGAL,
            subreason="reembed_queue_missing",
            detail={"queue_id": queue_id},
        )
    if row[0] != REEMBED_QUEUED:
        raise MemoryRagError(
            "reembed queue item not queued",
            fail_locus=LOCUS_LIFECYCLE_ILLEGAL,
            subreason="reembed_not_queued",
            detail={"queue_id": queue_id, "status": row[0]},
        )
    conn.execute(
        f"UPDATE {TABLE_REEMBED_QUEUE} SET status=? WHERE queue_id=?",
        (status, queue_id),
    )
    conn.commit()
    return [
        {
            "kind": TRACE_REEMBED,
            "queue_id": queue_id,
            "status": status,
            "action": "complete",
        }
    ]


def try_reactivate_binding(conn: sqlite3.Connection, binding_id: str) -> None:
    """
    Explicit illegal path for tests: attempt UPDATE terminal → active.
    Must fail (application check and/or SQLite trigger).
    """
    transition_lifecycle(
        conn, binding_id=binding_id, new_status=STATUS_ACTIVE
    )
