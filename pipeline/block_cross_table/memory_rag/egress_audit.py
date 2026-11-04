# -*- coding: utf-8 -*-
"""
External model egress privacy audit (M6).

- Logs every outbound chat/embed attempt (token redacted).
- local_only policy hard-blocks external HTTP.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from pipeline.block_cross_table.memory_rag.constants import (
    EGRESS_ALLOW_EXTERNAL,
    EGRESS_LOCAL_ONLY,
    EGRESS_POLICY_CHOICES,
    TABLE_EGRESS_AUDIT,
)

ConnFactory = Callable[[], sqlite3.Connection | None]

_lock = threading.Lock()
_sink: ConnFactory | None = None
_PREVIEW_CHARS = 240
_MAX_PREVIEW = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def register_audit_sink(factory: ConnFactory | None) -> None:
    global _sink
    with _lock:
        _sink = factory


def resolve_egress_policy() -> str:
    import config

    raw = str(config.get_param("memory_rag.egress_policy", EGRESS_ALLOW_EXTERNAL) or "").strip()
    if raw in EGRESS_POLICY_CHOICES:
        return raw
    return EGRESS_ALLOW_EXTERNAL


def assert_external_egress_allowed(*, channel: str) -> None:
    """Hard-fail when local_only but code attempts external call."""
    if resolve_egress_policy() == EGRESS_LOCAL_ONLY:
        from pipeline.block_cross_table.memory_rag.errors import (
            LOCUS_EXTERNAL_CALL_FAILED,
            MemoryRagError,
        )

        raise MemoryRagError(
            "egress blocked: local_only policy",
            fail_locus=LOCUS_EXTERNAL_CALL_FAILED,
            subreason="egress_local_only",
            detail={"channel": channel},
        )


def redact_preview(text: str, *, limit: int = _PREVIEW_CHARS) -> str:
    s = str(text or "").replace("\r", " ").replace("\n", " ")
    # Strip bearer-looking tokens
    import re

    s = re.sub(r"(?i)bearer\s+[a-z0-9._\-]+", "Bearer ***", s)
    s = re.sub(r"(?i)(api[_-]?key|authorization)\s*[:=]\s*\S+", r"\1=***", s)
    if len(s) > int(limit):
        return s[: int(limit)] + "…"
    return s


def payload_preview_from_messages(messages: list[Any]) -> str:
    parts: list[str] = []
    for m in messages or []:
        if isinstance(m, dict):
            role = str(m.get("role") or "")
            content = str(m.get("content") or "")
            parts.append(f"{role}: {content}")
        else:
            parts.append(str(m))
    return redact_preview(" | ".join(parts), limit=_MAX_PREVIEW)


def record_egress(
    *,
    target_url: str,
    model_name: str,
    payload_chars: int,
    payload_preview: str,
    status: str,
    channel: str = "chat",
    detail: dict[str, Any] | None = None,
) -> str | None:
    """
    Persist one egress audit row. Returns audit_id or None if no sink.
    Never stores Authorization headers / raw API keys.
    """
    factory = _sink
    conn = None
    if factory is not None:
        try:
            conn = factory()
        except Exception:
            conn = None
    if conn is None:
        return None
    audit_id = str(uuid4())
    body = {
        "channel": channel,
        "detail": detail or {},
    }
    conn.execute(
        f"""
        INSERT INTO {TABLE_EGRESS_AUDIT}(
            audit_id, created_at, target_url, model_name,
            payload_chars, payload_preview, status, channel, detail_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            audit_id,
            _now(),
            redact_preview(str(target_url or ""), limit=300),
            str(model_name or ""),
            int(payload_chars),
            redact_preview(payload_preview, limit=_MAX_PREVIEW),
            str(status or ""),
            str(channel or ""),
            json.dumps(body, ensure_ascii=False),
        ),
    )
    conn.commit()
    return audit_id


def list_egress_audits(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT audit_id, created_at, target_url, model_name,
               payload_chars, payload_preview, status, channel, detail_json
        FROM {TABLE_EGRESS_AUDIT}
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
                "audit_id": r[0],
                "timestamp": r[1],
                "created_at": r[1],
                "target_url": r[2],
                "model_name": r[3],
                "payload_chars": r[4],
                "payload_preview": r[5],
                "status": r[6],
                "channel": r[7],
                "detail": detail,
            }
        )
    return out
