# -*- coding: utf-8 -*-
"""
Dialogue-review jargon mining (M6).

Collects dialogue text only (no table dumps) → chat extract → jargon_dedup gate.
Replaces M5.2 directory-scan auto_learning.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from pipeline.block_cross_table.memory_rag.constants import (
    DEDUP_CONFLICT,
    DEDUP_PENDING_CONFIRM,
    DEDUP_SILENT_SKIP,
    DIALOGUE_SESSION_DONE,
    DIALOGUE_SESSION_FAILED,
    DIALOGUE_SESSION_OPEN,
    DIALOGUE_SESSION_REVIEWING,
    SOURCE_DIALOGUE_REVIEW,
    TABLE_DIALOGUE_SESSION,
    TABLE_DIALOGUE_TURN,
    TRACE_DIALOGUE_REVIEW,
)
from pipeline.block_cross_table.memory_rag.errors import (
    LOCUS_JARGON_INFERENCE,
    MemoryRagError,
)
from pipeline.block_cross_table.memory_rag.jargon_dedup import apply_dedup_after_inference
from pipeline.block_cross_table.memory_rag.merge_settings import (
    MergeRuntimeSettings,
    load_merge_runtime_settings,
)

ChatJsonFn = Callable[..., dict | None]

# Structural extract prompt — no business-term branching.
_REVIEW_PROMPT = """你是一个业务语义分析专家。请分析以下对话记录，提取出用户可能使用的「黑话」、缩写或特定业务逻辑称谓。
只依据对话文本；不要臆造表格字段或内部库表内容。

对话记录：
{dialogue}

以 JSON 输出：
{{"items":[{{"term":"词条","meaning":"词义","evidence":"依据的对话片段"}}]}}
若无可提取项：{{"items":[]}}
"""

_ROLE_ALLOWED = frozenset({"user", "assistant", "ai", "system"})
# Heuristic: reject obvious tabular dumps (not dialogue).
_TABULAR_LINE = re.compile(r".+\t.+\t.+")
_CSV_HEAVY = re.compile(r"^[^,\n]+(?:,[^,\n]+){5,}$", re.M)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_chat_json(prompt: str, **_kwargs: Any) -> dict | None:
    import config
    from ai.client import chat_json

    key = config.get_api_key()
    if not key:
        raise MemoryRagError(
            "chat api key missing for dialogue review",
            fail_locus=LOCUS_JARGON_INFERENCE,
            subreason="api_key_missing",
        )
    return chat_json(prompt, key, temperature=0.1)


def assert_dialogue_text_safe(text: str, *, max_chars: int = 12000) -> str:
    """
    Only dialogue text may leave for review.
    Reject oversized or table-like payloads.
    """
    body = str(text or "").strip()
    if not body:
        raise MemoryRagError(
            "empty dialogue",
            fail_locus=LOCUS_JARGON_INFERENCE,
            subreason="empty_dialogue",
        )
    if len(body) > int(max_chars):
        raise MemoryRagError(
            "dialogue too large for egress",
            fail_locus=LOCUS_JARGON_INFERENCE,
            subreason="dialogue_too_large",
            detail={"chars": len(body), "max": max_chars},
        )
    lines = body.splitlines()
    tab_hits = sum(1 for ln in lines if _TABULAR_LINE.match(ln))
    if tab_hits >= 3:
        raise MemoryRagError(
            "refusing tabular payload in dialogue review",
            fail_locus=LOCUS_JARGON_INFERENCE,
            subreason="tabular_payload_forbidden",
        )
    csv_hits = len(_CSV_HEAVY.findall(body))
    if csv_hits >= 3:
        raise MemoryRagError(
            "refusing csv-like table dump in dialogue review",
            fail_locus=LOCUS_JARGON_INFERENCE,
            subreason="table_dump_forbidden",
        )
    return body


@dataclass(frozen=True)
class DialogueTurn:
    role: str
    content: str
    created_at: str


def start_dialogue_session(
    conn: sqlite3.Connection, *, title: str = ""
) -> dict[str, Any]:
    sid = str(uuid4())
    now = _now()
    conn.execute(
        f"""
        INSERT INTO {TABLE_DIALOGUE_SESSION}(
            session_id, title, status, created_at, updated_at, ended_at, detail_json
        ) VALUES (?, ?, ?, ?, ?, NULL, '{{}}')
        """,
        (sid, str(title or "").strip(), DIALOGUE_SESSION_OPEN, now, now),
    )
    conn.commit()
    return {"session_id": sid, "status": DIALOGUE_SESSION_OPEN, "created_at": now}


def append_dialogue_turn(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    role: str,
    content: str,
) -> dict[str, Any]:
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id required")
    row = conn.execute(
        f"SELECT status FROM {TABLE_DIALOGUE_SESSION} WHERE session_id=?",
        (sid,),
    ).fetchone()
    if row is None:
        raise KeyError(sid)
    if row[0] != DIALOGUE_SESSION_OPEN:
        raise ValueError(f"session not open: {row[0]}")
    r = str(role or "").strip().lower()
    if r == "ai":
        r = "assistant"
    if r not in _ROLE_ALLOWED:
        raise ValueError(f"invalid role: {role}")
    text = str(content or "").strip()
    if not text:
        raise ValueError("empty turn content")
    # Per-turn safety (no table dumps)
    assert_dialogue_text_safe(text, max_chars=4000)
    tid = str(uuid4())
    now = _now()
    conn.execute(
        f"""
        INSERT INTO {TABLE_DIALOGUE_TURN}(
            turn_id, session_id, role, content, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (tid, sid, r, text, now),
    )
    conn.execute(
        f"UPDATE {TABLE_DIALOGUE_SESSION} SET updated_at=? WHERE session_id=?",
        (now, sid),
    )
    conn.commit()
    return {"turn_id": tid, "session_id": sid, "role": r, "created_at": now}


def list_session_turns(conn: sqlite3.Connection, session_id: str) -> list[DialogueTurn]:
    rows = conn.execute(
        f"""
        SELECT role, content, created_at FROM {TABLE_DIALOGUE_TURN}
        WHERE session_id=?
        ORDER BY created_at ASC, rowid ASC
        """,
        (str(session_id),),
    ).fetchall()
    return [DialogueTurn(role=r[0], content=r[1], created_at=r[2]) for r in rows]


def format_dialogue_for_review(turns: list[DialogueTurn]) -> str:
    lines = []
    for t in turns:
        label = "用户" if t.role == "user" else ("助手" if t.role in ("assistant", "ai") else t.role)
        lines.append(f"{label}: {t.content}")
    return "\n".join(lines)


def extract_terms_from_dialogue(
    dialogue_text: str,
    *,
    chat_json_fn: ChatJsonFn | None = None,
) -> list[dict[str, str]]:
    safe = assert_dialogue_text_safe(dialogue_text)
    chat = chat_json_fn or _default_chat_json
    prompt = _REVIEW_PROMPT.format(dialogue=safe)
    raw = chat(prompt) or {}
    if not isinstance(raw, dict):
        raise MemoryRagError(
            "dialogue review returned non-object",
            fail_locus=LOCUS_JARGON_INFERENCE,
            subreason="bad_review_response",
        )
    items = raw.get("items") or []
    out: list[dict[str, str]] = []
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        term = str(it.get("term") or "").strip()
        meaning = str(it.get("meaning") or "").strip()
        evidence = str(it.get("evidence") or "").strip()
        if term and meaning:
            out.append({"term": term, "meaning": meaning, "evidence": evidence})
    return out


def run_dialogue_review(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    settings: MergeRuntimeSettings | None = None,
    chat_json_fn: ChatJsonFn | None = None,
) -> dict[str, Any]:
    """
    Sync review: extract → dual-db dedup → pending/conflict/silent.
    Never writes confirmed bindings.
    """
    settings = settings or load_merge_runtime_settings()
    sid = str(session_id)
    turns = list_session_turns(conn, sid)
    if not turns:
        raise MemoryRagError(
            "no turns to review",
            fail_locus=LOCUS_JARGON_INFERENCE,
            subreason="no_turns",
        )
    now = _now()
    conn.execute(
        f"""
        UPDATE {TABLE_DIALOGUE_SESSION}
        SET status=?, updated_at=? WHERE session_id=?
        """,
        (DIALOGUE_SESSION_REVIEWING, now, sid),
    )
    conn.commit()

    dialogue = format_dialogue_for_review(turns)
    decisions: list[dict[str, Any]] = []
    silent_n = conflict_n = pending_n = 0
    try:
        items = extract_terms_from_dialogue(dialogue, chat_json_fn=chat_json_fn)
        for it in items:
            decision = apply_dedup_after_inference(
                conn,
                content=it["term"],
                meaning=it["meaning"],
                context_snippet=it.get("evidence") or "",
                settings=settings,
                chat_json_fn=chat_json_fn,
                source_type=SOURCE_DIALOGUE_REVIEW,
            )
            if decision.disposition == DEDUP_SILENT_SKIP:
                silent_n += 1
            elif decision.disposition == DEDUP_CONFLICT:
                conflict_n += 1
            elif decision.disposition == DEDUP_PENDING_CONFIRM:
                pending_n += 1
            decisions.append(
                {
                    "term": it["term"],
                    "meaning": it["meaning"],
                    "disposition": decision.disposition,
                    "proposal_id": decision.proposal_id,
                    "conflict_id": decision.conflict_id,
                }
            )
        status = DIALOGUE_SESSION_DONE
        err = None
    except Exception as exc:
        status = DIALOGUE_SESSION_FAILED
        err = str(exc)
        items = []

    detail = {
        "turn_count": len(turns),
        "extracted_count": len(items) if status == DIALOGUE_SESSION_DONE else 0,
        "silent_skip_count": silent_n,
        "conflict_count": conflict_n,
        "pending_count": pending_n,
        "decisions": decisions[:100],
        "error": err,
    }
    ended = _now()
    conn.execute(
        f"""
        UPDATE {TABLE_DIALOGUE_SESSION}
        SET status=?, updated_at=?, ended_at=?, detail_json=?
        WHERE session_id=?
        """,
        (status, ended, ended, json.dumps(detail, ensure_ascii=False), sid),
    )
    conn.execute(
        """
        INSERT INTO memory_edit_trace(trace_id, kind, body_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            str(uuid4()),
            TRACE_DIALOGUE_REVIEW,
            json.dumps({"session_id": sid, **detail}, ensure_ascii=False),
            ended,
        ),
    )
    conn.commit()
    return {
        "session_id": sid,
        "status": status,
        "turn_count": len(turns),
        "extracted_count": detail["extracted_count"],
        "silent_skip_count": silent_n,
        "conflict_count": conflict_n,
        "pending_count": pending_n,
        "decisions": decisions,
        "error": err,
    }


def end_dialogue_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    settings: MergeRuntimeSettings | None = None,
    chat_json_fn: ChatJsonFn | None = None,
    async_review: bool = False,
) -> dict[str, Any]:
    """
    Mark session for review. If async_review, spawn daemon thread (Web).
    """
    if not async_review:
        return run_dialogue_review(
            conn,
            session_id=session_id,
            settings=settings,
            chat_json_fn=chat_json_fn,
        )

    def _job():
        try:
            run_dialogue_review(
                conn,
                session_id=session_id,
                settings=settings,
                chat_json_fn=chat_json_fn,
            )
        except Exception:
            pass

    threading.Thread(target=_job, name="dialogue-review", daemon=True).start()
    return {
        "session_id": session_id,
        "status": DIALOGUE_SESSION_REVIEWING,
        "async": True,
    }


def list_dialogue_sessions(
    conn: sqlite3.Connection, *, limit: int = 20
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT session_id, title, status, created_at, updated_at, ended_at, detail_json
        FROM {TABLE_DIALOGUE_SESSION}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    out = []
    for r in rows:
        try:
            detail = json.loads(r[6] or "{}")
        except Exception:
            detail = {}
        out.append(
            {
                "session_id": r[0],
                "title": r[1],
                "status": r[2],
                "created_at": r[3],
                "updated_at": r[4],
                "ended_at": r[5],
                "detail": detail,
            }
        )
    return out
