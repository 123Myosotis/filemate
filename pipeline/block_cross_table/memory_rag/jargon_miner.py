# -*- coding: utf-8 -*-
"""
TXT import → term frequency → thresholded dual-path inference → dual-DB dedup.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pipeline.block_cross_table.memory_rag.constants import (
    DEDUP_CONFLICT,
    DEDUP_PENDING_CONFIRM,
    DEDUP_SILENT_SKIP,
    TABLE_JARGON_MINING_JOBS,
    TABLE_JARGON_TERM_STATS,
    TRACE_JARGON_MINING,
)
from pipeline.block_cross_table.memory_rag.errors import LOCUS_TXT_IMPORT, MemoryRagError
from pipeline.block_cross_table.memory_rag.constants import SOURCE_TXT_MINING
from pipeline.block_cross_table.memory_rag.jargon_dedup import apply_dedup_after_inference
from pipeline.block_cross_table.memory_rag.jargon_inference import (
    ChatJsonFn,
    infer_jargon_meaning,
)
from pipeline.block_cross_table.memory_rag.merge_settings import (
    MergeRuntimeSettings,
    load_merge_runtime_settings,
)
from pipeline.block_cross_table.memory_rag.text_tokenize import (
    context_windows,
    count_terms,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def should_infer_meaning(
    *,
    count: int,
    last_inference_count: int,
    is_complete: bool,
    thresholds: tuple[int, ...],
) -> bool:
    """MaiBot-compatible threshold gate (parameterized thresholds)."""
    if is_complete:
        return False
    c = int(count)
    last = int(last_inference_count)
    th = tuple(sorted(int(x) for x in thresholds))
    if not th or c < th[0]:
        return False
    if c <= last:
        return False
    next_th = next((t for t in th if t > last), None)
    if next_th is None:
        return False
    return c >= next_th


def _upsert_term_stat(
    conn: sqlite3.Connection,
    *,
    content: str,
    add_count: int,
    snippet: str,
) -> dict[str, Any]:
    term = str(content).strip()
    row = conn.execute(
        f"""
        SELECT count, last_inference_count, is_complete
        FROM {TABLE_JARGON_TERM_STATS}
        WHERE content = ?
        """,
        (term,),
    ).fetchone()
    now = _now()
    if row is None:
        total = int(add_count)
        conn.execute(
            f"""
            INSERT INTO {TABLE_JARGON_TERM_STATS}(
                content, count, last_inference_count, is_complete,
                last_context_snippet, updated_at
            ) VALUES (?, ?, 0, 0, ?, ?)
            """,
            (term, total, snippet[:2000], now),
        )
        return {
            "content": term,
            "count": total,
            "last_inference_count": 0,
            "is_complete": False,
        }
    total = int(row[0] or 0) + int(add_count)
    conn.execute(
        f"""
        UPDATE {TABLE_JARGON_TERM_STATS}
        SET count = ?, last_context_snippet = ?, updated_at = ?
        WHERE content = ?
        """,
        (total, snippet[:2000], now, term),
    )
    return {
        "content": term,
        "count": total,
        "last_inference_count": int(row[1] or 0),
        "is_complete": bool(row[2]),
    }


def import_txt_and_mine(
    conn: sqlite3.Connection,
    *,
    text: str,
    source_name: str,
    settings: MergeRuntimeSettings | None = None,
    chat_json_fn: ChatJsonFn | None = None,
    max_infer_terms: int | None = None,
    cjk_n_min: int = 2,
    cjk_n_max: int = 4,
) -> dict[str, Any]:
    """
    Full mining job:
      tokenize → accumulate counts → infer when thresholds hit → dual-db dedup.
    """
    settings = settings or load_merge_runtime_settings()
    body = str(text or "")
    if not body.strip():
        raise MemoryRagError(
            "empty txt",
            fail_locus=LOCUS_TXT_IMPORT,
            subreason="empty_text",
        )
    job_id = str(uuid4())
    now = _now()
    conn.execute(
        f"""
        INSERT INTO {TABLE_JARGON_MINING_JOBS}(
            job_id, status, source_name, term_count, inferred_count,
            silent_skip_count, conflict_count, pending_count,
            detail_json, created_at, updated_at
        ) VALUES (?, 'running', ?, 0, 0, 0, 0, 0, '{{}}', ?, ?)
        """,
        (job_id, str(source_name or "upload.txt"), now, now),
    )
    conn.commit()

    counts = count_terms(
        body,
        cjk_n_min=int(cjk_n_min),
        cjk_n_max=int(cjk_n_max),
        min_token_len=2,
        stopwords=settings.jargon_stopwords,
    )
    silent_n = conflict_n = pending_n = inferred_n = 0
    decisions: list[dict[str, Any]] = []
    infer_budget = (
        int(max_infer_terms)
        if max_infer_terms is not None
        else len(counts)
    )

    # Process high-frequency first.
    for term, add_n in counts.most_common():
        windows = context_windows(body, term)
        snippet = windows[0] if windows else ""
        stat = _upsert_term_stat(
            conn, content=term, add_count=int(add_n), snippet=snippet
        )
        if inferred_n >= infer_budget:
            continue
        if not should_infer_meaning(
            count=int(stat["count"]),
            last_inference_count=int(stat["last_inference_count"]),
            is_complete=bool(stat["is_complete"]),
            thresholds=settings.jargon_inference_thresholds,
        ):
            continue
        try:
            result = infer_jargon_meaning(
                content=term,
                context_snippets=windows,
                previous_meaning="",
                chat_json_fn=chat_json_fn,
            )
        except MemoryRagError as exc:
            decisions.append(
                {
                    "content": term,
                    "error": exc.as_report(),
                }
            )
            # Advance last_inference_count to avoid tight retry loops on hard fail.
            conn.execute(
                f"""
                UPDATE {TABLE_JARGON_TERM_STATS}
                SET last_inference_count = ?, updated_at = ?
                WHERE content = ?
                """,
                (int(stat["count"]), _now(), term),
            )
            conn.commit()
            continue

        conn.execute(
            f"""
            UPDATE {TABLE_JARGON_TERM_STATS}
            SET last_inference_count = ?,
                is_complete = ?,
                updated_at = ?
            WHERE content = ?
            """,
            (
                int(stat["count"]),
                1 if int(stat["count"]) >= max(settings.jargon_inference_thresholds) else 0,
                _now(),
                term,
            ),
        )
        conn.commit()
        inferred_n += 1

        if result.no_info or not result.meaning:
            decisions.append(
                {
                    "content": term,
                    "disposition": "no_info",
                    "count": stat["count"],
                }
            )
            continue
        if not result.is_jargon:
            decisions.append(
                {
                    "content": term,
                    "disposition": "not_jargon",
                    "count": stat["count"],
                    "comparison": result.comparison,
                }
            )
            continue

        decision = apply_dedup_after_inference(
            conn,
            content=term,
            meaning=result.meaning,
            context_snippet=snippet,
            settings=settings,
            chat_json_fn=chat_json_fn,
            source_type=SOURCE_TXT_MINING,
        )
        if decision.disposition == DEDUP_SILENT_SKIP:
            silent_n += 1
        elif decision.disposition == DEDUP_CONFLICT:
            conflict_n += 1
        elif decision.disposition == DEDUP_PENDING_CONFIRM:
            pending_n += 1
        decisions.append(
            {
                "content": term,
                "disposition": decision.disposition,
                "meaning": decision.meaning,
                "conflict_id": decision.conflict_id,
                "proposal_id": decision.proposal_id,
                "count": stat["count"],
            }
        )

    detail = {
        "unique_terms": len(counts),
        "decisions": decisions[:200],
        "thresholds": list(settings.jargon_inference_thresholds),
        "self_learning_engine": settings.self_learning_engine,
        "meaning_equivalence_mode": settings.meaning_equivalence_mode,
    }
    conn.execute(
        f"""
        UPDATE {TABLE_JARGON_MINING_JOBS}
        SET status = 'done',
            term_count = ?,
            inferred_count = ?,
            silent_skip_count = ?,
            conflict_count = ?,
            pending_count = ?,
            detail_json = ?,
            updated_at = ?
        WHERE job_id = ?
        """,
        (
            len(counts),
            inferred_n,
            silent_n,
            conflict_n,
            pending_n,
            json.dumps(detail, ensure_ascii=False),
            _now(),
            job_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO memory_edit_trace(trace_id, kind, body_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            str(uuid4()),
            TRACE_JARGON_MINING,
            json.dumps(
                {
                    "job_id": job_id,
                    "source_name": source_name,
                    "term_count": len(counts),
                    "inferred_count": inferred_n,
                    "silent_skip_count": silent_n,
                    "conflict_count": conflict_n,
                    "pending_count": pending_n,
                },
                ensure_ascii=False,
            ),
            _now(),
        ),
    )
    conn.commit()
    return {
        "job_id": job_id,
        "status": "done",
        "term_count": len(counts),
        "inferred_count": inferred_n,
        "silent_skip_count": silent_n,
        "conflict_count": conflict_n,
        "pending_count": pending_n,
        "decisions": decisions,
    }


def list_mining_jobs(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT job_id, status, source_name, term_count, inferred_count,
               silent_skip_count, conflict_count, pending_count,
               detail_json, created_at, updated_at
        FROM {TABLE_JARGON_MINING_JOBS}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    out = []
    for r in rows:
        try:
            detail = json.loads(r[8] or "{}")
        except Exception:
            detail = {}
        out.append(
            {
                "job_id": r[0],
                "status": r[1],
                "source_name": r[2],
                "term_count": r[3],
                "inferred_count": r[4],
                "silent_skip_count": r[5],
                "conflict_count": r[6],
                "pending_count": r[7],
                "detail": detail,
                "created_at": r[9],
                "updated_at": r[10],
            }
        )
    return out


def mining_status_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    jobs = list_mining_jobs(conn, limit=1)
    open_conflicts = conn.execute(
        "SELECT COUNT(*) FROM filemate_jargon_conflicts WHERE status='open'"
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM memory_edit_proposal WHERE status='proposed'"
    ).fetchone()[0]
    jargon_n = conn.execute(
        "SELECT COUNT(*) FROM filemate_jargons"
    ).fetchone()[0]
    settings = load_merge_runtime_settings()
    return {
        "latest_job": jobs[0] if jobs else None,
        "open_conflict_count": int(open_conflicts),
        "pending_proposal_count": int(pending),
        "filemate_jargon_count": int(jargon_n),
        "self_learning_engine": settings.self_learning_engine,
        "meaning_equivalence_mode": settings.meaning_equivalence_mode,
        "egress_policy": settings.egress_policy,
        "dialogue_review_enabled": settings.dialogue_review_enabled,
    }
