# -*- coding: utf-8 -*-
"""用户纠正记忆（接入层「记住选择」）。

只在用户明确改过一次之后写入。记忆绝不碰业务文件。
不合并 Excel `memory_rag` 与 Word `memory.py`（架构 §9：1≠2≠3）。
生命周期与 Word 术语相同：active / invalidated / revoked。
内容指纹变了 → lookup 当 miss（不把旧表头套到新文件上）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from db.database import DB_PATH

_STATUS = ("active", "invalidated", "revoked")
_KINDS = ("table_wash", "content_tag")

_DDL = """CREATE TABLE IF NOT EXISTS memory_choice (
    choice_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    cue TEXT NOT NULL,
    value TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    use_count INTEGER DEFAULT 0,
    last_used TEXT,
    created_at TEXT
)"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_choice_memory() -> None:
    conn = _connect()
    try:
        conn.execute(_DDL)
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _id(kind: str, scope: str, cue: str) -> str:
    raw = "|".join((str(kind), str(scope), str(cue)))
    return "c-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def record(kind: str, scope: str, cue: str, value: dict) -> str:
    """写入/覆盖一条纠正。kind 不在白名单、scope/cue 空 → 跳过。"""
    kind = str(kind or "")
    scope = str(scope or "").strip()
    cue = str(cue or "").strip()
    if kind not in _KINDS or not scope or not cue:
        return ""
    if not isinstance(value, dict):
        return ""
    init_choice_memory()
    cid = _id(kind, scope, cue)
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO memory_choice"
            "(choice_id, kind, scope, cue, value, status, use_count, last_used, created_at) "
            "VALUES(?,?,?,?,?,'active',0,?,?) "
            "ON CONFLICT(choice_id) DO UPDATE SET value=excluded.value, "
            "status='active', last_used=excluded.last_used",
            (cid, kind, scope, cue, json.dumps(value, ensure_ascii=False),
             _now(), _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return cid


def lookup(kind: str, scope: str, cue: str, file_hash: str = "") -> dict | None:
    """只返回 active。file_hash 非空且与记录不一致 → None（当 miss，不自动 revoke）。"""
    kind = str(kind or "")
    scope = str(scope or "").strip()
    cue = str(cue or "").strip()
    if kind not in _KINDS or not scope or not cue:
        return None
    init_choice_memory()
    cid = _id(kind, scope, cue)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM memory_choice WHERE choice_id=? AND status='active'",
            (cid,),
        ).fetchone()
        if row is None:
            return None
        rec = dict(row)
        try:
            rec["value"] = json.loads(rec.get("value") or "{}")
        except json.JSONDecodeError:
            return None
        stored = str((rec["value"] or {}).get("file_hash") or "")
        want = str(file_hash or "")
        if want and stored != want:
            return None
        return rec
    finally:
        conn.close()


def touch(choice_id: str) -> None:
    if not choice_id:
        return
    init_choice_memory()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE memory_choice SET use_count=use_count+1, last_used=? WHERE choice_id=?",
            (_now(), choice_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_status(choice_id: str, status: str) -> bool:
    if status not in _STATUS or not choice_id:
        return False
    init_choice_memory()
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE memory_choice SET status=?, last_used=? WHERE choice_id=?",
            (status, _now(), choice_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def forget_scope(kind: str, scope: str) -> int:
    """测试/用户作废：该 scope 下 kind 全部 revoked。"""
    init_choice_memory()
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE memory_choice SET status='revoked', last_used=? "
            "WHERE kind=? AND scope=?",
            (_now(), str(kind), str(scope)),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()
