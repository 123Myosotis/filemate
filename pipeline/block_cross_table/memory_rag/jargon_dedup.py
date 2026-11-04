# -*- coding: utf-8 -*-
"""
Dual-DB jargon dedup after meaning inference (M5.2 dual engine).

- content exact match in filemate_jargons OR MaiBot RO
- meaning: engine-aware (chat AI-first / vector screen±AI)
- equivalent → silent_skip; conflict → conflict list; no hit → pending_confirm
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pipeline.block_cross_table.memory_rag.constants import (
    CONFLICT_STATUS_OPEN,
    DEDUP_CONFLICT,
    DEDUP_PENDING_CONFIRM,
    DEDUP_SILENT_SKIP,
    ENGINE_CHAT,
    SOURCE_MAIBOT_CONFLICT,
    SOURCE_TXT_MINING,
    TABLE_JARGON_CONFLICTS,
    TRACE_JARGON_DEDUP,
)
from pipeline.block_cross_table.memory_rag.jargon_store import (
    fetch_filemate_jargon_by_content,
)
from pipeline.block_cross_table.memory_rag.maibot_ro import (
    fetch_jargon_by_content,
    open_maibot_ro,
)
from pipeline.block_cross_table.memory_rag.meaning_equivalence import (
    ChatJsonFn,
    meanings_equivalent_for_engine,
    meanings_equivalent_smart,
)
from pipeline.block_cross_table.memory_rag.merge_settings import (
    MergeRuntimeSettings,
    load_merge_runtime_settings,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_trace(conn: sqlite3.Connection, *, kind: str, body: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO memory_edit_trace(trace_id, kind, body_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (str(uuid4()), kind, json.dumps(body, ensure_ascii=False), _now()),
    )


@dataclass(frozen=True)
class DedupDecision:
    disposition: str
    content: str
    meaning: str
    maibot_meaning: str | None
    similarity: float | None
    conflict_id: str | None
    proposal_id: str | None
    detail: dict[str, Any]


def _row_meaning(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("meaning") or "")
    return str(getattr(row, "meaning", "") or "")


def classify_against_maibot(
    *,
    content: str,
    meaning: str,
    maibot_rows: list[Any],
    threshold: float | None = None,
    fallback_threshold: float = 0.6,
    chat_json_fn: ChatJsonFn | None = None,
    prefer_ai: bool = True,
    engine: str | None = None,
    meaning_equivalence_mode: str | None = None,
) -> tuple[str, str | None, float | None, dict[str, Any]]:
    """
    Pure classification (no DB writes).
    Returns (disposition, matched_meaning_or_none, similarity_or_none, meta).
    When engine is set, uses meanings_equivalent_for_engine; else legacy prefer_ai path.
    """
    term = str(content or "").strip()
    inferred = str(meaning or "").strip()
    meta: dict[str, Any] = {}
    if not term or not inferred:
        return DEDUP_PENDING_CONFIRM, None, None, meta
    if not maibot_rows:
        return DEDUP_PENDING_CONFIRM, None, None, meta

    fb = float(fallback_threshold if threshold is None else threshold)
    if prefer_ai and engine is None:
        fb = float(fallback_threshold)

    best_sim = -1.0
    best_meaning: str | None = None
    any_conflict = False
    methods: list[str] = []

    for row in maibot_rows:
        m = _row_meaning(row)
        if engine is not None:
            verdict = meanings_equivalent_for_engine(
                inferred,
                m,
                engine=engine,
                meaning_equivalence_mode=meaning_equivalence_mode or "ai",
                fallback_threshold=fb,
                chat_json_fn=chat_json_fn,
            )
        else:
            verdict = meanings_equivalent_smart(
                inferred,
                m,
                fallback_threshold=fb if prefer_ai else float(threshold or fallback_threshold),
                chat_json_fn=chat_json_fn,
                prefer_ai=prefer_ai,
            )
        methods.append(verdict.method)
        sim = float(verdict.similarity if verdict.similarity is not None else -1.0)
        if sim > best_sim:
            best_sim = sim
            best_meaning = m
        if verdict.equivalent:
            meta = {
                "equivalence_method": verdict.method,
                "equivalence_detail": verdict.detail,
                "methods": methods,
                "match_source": (
                    row.get("match_source") if isinstance(row, dict) else "maibot"
                ),
            }
            return DEDUP_SILENT_SKIP, m, sim if sim >= 0 else None, meta
        any_conflict = True

    meta = {"equivalence_methods": methods}
    if any_conflict:
        return (
            DEDUP_CONFLICT,
            best_meaning,
            best_sim if best_sim >= 0 else None,
            meta,
        )
    return DEDUP_PENDING_CONFIRM, None, None, meta


def collect_content_match_rows(
    conn: sqlite3.Connection,
    *,
    content: str,
    maibot_data_dir: str,
) -> list[dict[str, Any]]:
    """Exact content hits from filemate_jargons + MaiBot RO."""
    rows: list[dict[str, Any]] = []
    for r in fetch_filemate_jargon_by_content(conn, content):
        rows.append(r)
    if maibot_data_dir:
        with open_maibot_ro(maibot_data_dir) as mconn:
            if mconn is not None:
                for r in fetch_jargon_by_content(mconn, content):
                    if isinstance(r, dict):
                        item = dict(r)
                    else:
                        item = {
                            "content": getattr(r, "content", content),
                            "meaning": getattr(r, "meaning", ""),
                        }
                    item["match_source"] = "maibot"
                    rows.append(item)
    return rows


def apply_dedup_after_inference(
    conn: sqlite3.Connection,
    *,
    content: str,
    meaning: str,
    context_snippet: str = "",
    settings: MergeRuntimeSettings | None = None,
    propose_fn: Any | None = None,
    chat_json_fn: ChatJsonFn | None = None,
    source_type: str = SOURCE_TXT_MINING,
) -> DedupDecision:
    """
    Look up filemate + MaiBot RO → silent skip / open conflict / enqueue pending.
    """
    settings = settings or load_merge_runtime_settings()
    term = str(content or "").strip()
    inferred = str(meaning or "").strip()
    match_rows = collect_content_match_rows(
        conn, content=term, maibot_data_dir=settings.maibot_data_dir
    )

    disposition, maibot_meaning, sim, eq_meta = classify_against_maibot(
        content=term,
        meaning=inferred,
        maibot_rows=match_rows,
        fallback_threshold=settings.meaning_fallback_threshold,
        chat_json_fn=chat_json_fn,
        prefer_ai=settings.prefer_ai_for_equivalence(),
        engine=settings.self_learning_engine or ENGINE_CHAT,
        meaning_equivalence_mode=settings.meaning_equivalence_mode,
    )

    conflict_id = None
    proposal_id = None
    detail: dict[str, Any] = {
        "content": term,
        "meaning": inferred,
        "maibot_hit_count": len(match_rows),
        "context_snippet_len": len(context_snippet or ""),
        "self_learning_engine": settings.self_learning_engine,
        "meaning_equivalence_mode": settings.meaning_equivalence_mode,
        **eq_meta,
    }

    if disposition == DEDUP_SILENT_SKIP:
        detail["maibot_meaning"] = maibot_meaning
        detail["similarity"] = sim
        _append_trace(
            conn,
            kind=TRACE_JARGON_DEDUP,
            body={"disposition": disposition, **detail},
        )
        conn.commit()
        return DedupDecision(
            disposition=disposition,
            content=term,
            meaning=inferred,
            maibot_meaning=maibot_meaning,
            similarity=sim,
            conflict_id=None,
            proposal_id=None,
            detail=detail,
        )

    if disposition == DEDUP_CONFLICT:
        conflict_id = str(uuid4())
        conn.execute(
            f"""
            INSERT INTO {TABLE_JARGON_CONFLICTS}(
                conflict_id, content, maibot_meaning, filemate_meaning,
                status, created_at, resolved_at, resolution_note
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                conflict_id,
                term,
                str(maibot_meaning or ""),
                inferred,
                CONFLICT_STATUS_OPEN,
                _now(),
            ),
        )
        detail["conflict_id"] = conflict_id
        detail["maibot_meaning"] = maibot_meaning
        detail["similarity"] = sim
        _append_trace(
            conn,
            kind=TRACE_JARGON_DEDUP,
            body={"disposition": disposition, **detail},
        )
        conn.commit()
        return DedupDecision(
            disposition=disposition,
            content=term,
            meaning=inferred,
            maibot_meaning=maibot_meaning,
            similarity=sim,
            conflict_id=conflict_id,
            proposal_id=None,
            detail=detail,
        )

    binding_payload = {
        "term": term,
        "description": inferred,
        "values": [],
        "scope": "",
    }
    scope_obj = {"source": "jargon_mining"}
    if propose_fn is None:
        from web.memory_correction.service import propose_memory_edit

        proposed = propose_memory_edit(
            conn,
            utterance_span_fingerprint=f"jargon:{term}",
            binding_payload=binding_payload,
            scope=scope_obj,
            source_type=source_type or SOURCE_TXT_MINING,
        )
        proposal_id = proposed.get("proposal_id")
    else:
        proposal_id = propose_fn(
            conn,
            utterance_span_fingerprint=f"jargon:{term}",
            binding_payload=binding_payload,
            scope=scope_obj,
            source_type=source_type or SOURCE_TXT_MINING,
        )
        if isinstance(proposal_id, dict):
            proposal_id = proposal_id.get("proposal_id")
    detail["proposal_id"] = proposal_id
    detail["source_type"] = source_type or SOURCE_TXT_MINING
    _append_trace(
        conn,
        kind=TRACE_JARGON_DEDUP,
        body={"disposition": disposition, **detail},
    )
    conn.commit()
    return DedupDecision(
        disposition=disposition,
        content=term,
        meaning=inferred,
        maibot_meaning=None,
        similarity=None,
        conflict_id=None,
        proposal_id=str(proposal_id) if proposal_id else None,
        detail=detail,
    )


def list_open_conflicts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT conflict_id, content, maibot_meaning, filemate_meaning,
               status, created_at, resolved_at, resolution_note
        FROM {TABLE_JARGON_CONFLICTS}
        WHERE status = ?
        ORDER BY created_at DESC
        """,
        (CONFLICT_STATUS_OPEN,),
    ).fetchall()
    return [
        {
            "conflict_id": r[0],
            "content": r[1],
            "maibot_meaning": r[2],
            "filemate_meaning": r[3],
            "status": r[4],
            "created_at": r[5],
            "resolved_at": r[6],
            "resolution_note": r[7],
            "source_type": SOURCE_MAIBOT_CONFLICT,
        }
        for r in rows
    ]


def resolve_conflict(
    conn: sqlite3.Connection,
    *,
    conflict_id: str,
    resolution: str,
    note: str = "",
    confirm_filemate_fn: Any | None = None,
) -> dict[str, Any]:
    """
    resolution ∈ keep_maibot | keep_filemate | coexist | dismissed
    keep_filemate / coexist → propose with source_type=MAIBOT_CONFLICT.
    """
    from pipeline.block_cross_table.memory_rag.constants import (
        CONFLICT_STATUS_COEXIST,
        CONFLICT_STATUS_DISMISSED,
        CONFLICT_STATUS_KEEP_FILEMATE,
        CONFLICT_STATUS_KEEP_MAIBOT,
    )

    allowed = {
        CONFLICT_STATUS_KEEP_MAIBOT,
        CONFLICT_STATUS_KEEP_FILEMATE,
        CONFLICT_STATUS_COEXIST,
        CONFLICT_STATUS_DISMISSED,
    }
    if resolution not in allowed:
        raise ValueError(f"invalid resolution: {resolution}")
    row = conn.execute(
        f"""
        SELECT conflict_id, content, maibot_meaning, filemate_meaning, status
        FROM {TABLE_JARGON_CONFLICTS}
        WHERE conflict_id = ?
        """,
        (conflict_id,),
    ).fetchone()
    if row is None:
        raise KeyError(conflict_id)
    if row[4] != CONFLICT_STATUS_OPEN:
        return {"conflict_id": conflict_id, "status": row[4], "already_resolved": True}

    proposal_id = None
    if resolution in (CONFLICT_STATUS_KEEP_FILEMATE, CONFLICT_STATUS_COEXIST):
        binding_payload = {
            "term": row[1],
            "description": row[3],
            "values": [],
            "scope": "",
        }
        scope_obj = {
            "source": "jargon_conflict",
            "coexist_with_maibot": resolution == CONFLICT_STATUS_COEXIST,
            "maibot_meaning": row[2],
        }
        if confirm_filemate_fn is not None:
            proposal_id = confirm_filemate_fn(
                conn,
                utterance_span_fingerprint=f"jargon:{row[1]}",
                binding_payload=binding_payload,
                scope=scope_obj,
                source_type=SOURCE_MAIBOT_CONFLICT,
            )
        else:
            from web.memory_correction.service import propose_memory_edit

            proposed = propose_memory_edit(
                conn,
                utterance_span_fingerprint=f"jargon:{row[1]}",
                binding_payload=binding_payload,
                scope=scope_obj,
                source_type=SOURCE_MAIBOT_CONFLICT,
            )
            proposal_id = proposed.get("proposal_id")
        if isinstance(proposal_id, dict):
            proposal_id = proposal_id.get("proposal_id")

    conn.execute(
        f"""
        UPDATE {TABLE_JARGON_CONFLICTS}
        SET status = ?, resolved_at = ?, resolution_note = ?
        WHERE conflict_id = ?
        """,
        (resolution, _now(), note or "", conflict_id),
    )
    conn.commit()
    return {
        "conflict_id": conflict_id,
        "status": resolution,
        "proposal_id": proposal_id,
    }
