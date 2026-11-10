# -*- coding: utf-8 -*-
"""记忆（阶段 4.1）：文件主题记忆表 + 保存 / 读取 / 检索 / 触摸。

最小版：
- 表 `memory_file`（hash 主键 + summary + keywords + use_count + last_used）。
- 检索优先 FTS5，FTS5 不可用则回退 `LIKE`。
- 不做热度/floor/降级/做梦（后续阶段）。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from db.database import DB_PATH

_TABLE = "memory_file"
_FTS = "memory_file_fts"

_DDL = [
    """CREATE TABLE IF NOT EXISTS memory_file (
        hash TEXT PRIMARY KEY,
        path TEXT,
        summary TEXT,
        keywords TEXT,
        use_count INTEGER DEFAULT 0,
        last_used TEXT,
        created_at TEXT
    )""",
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_memory() -> None:
    """建表（幂等）。"""
    conn = _connect()
    try:
        for sql in _DDL:
            conn.execute(sql)
        # FTS5 可选（不支持就跳过）
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS %s USING fts5(hash UNINDEXED, summary, keywords)"
                % _FTS
            )
        except sqlite3.OperationalError:
            pass
        conn.commit()
    finally:
        conn.close()


def _has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_FTS,)
    ).fetchone()
    return row is not None


def save_file_memory(hash: str, path: str = "", summary: str = "",
                     keywords: str = "") -> None:
    """写入/更新一条文件记忆（按 hash 覆盖）。"""
    if not hash:
        return
    init_memory()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO memory_file(hash, path, summary, keywords, use_count, last_used, created_at) "
            "VALUES(?,?,?,?,0,?,?) "
            "ON CONFLICT(hash) DO UPDATE SET path=excluded.path, "
            "summary=excluded.summary, keywords=excluded.keywords, last_used=excluded.last_used",
            (hash, path, summary, keywords, now, now),
        )
        if _has_fts(conn):
            conn.execute("DELETE FROM %s WHERE hash=?" % _FTS, (hash,))
            conn.execute(
                "INSERT INTO %s(hash, summary, keywords) VALUES(?,?,?)" % _FTS,
                (hash, summary, keywords),
            )
        conn.commit()
    finally:
        conn.close()


def get_file_memory(hash: str) -> dict | None:
    init_memory()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM memory_file WHERE hash=?", (hash,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def touch(hash: str) -> None:
    """被用到：use_count+1、last_used 更新。"""
    if not hash:
        return
    init_memory()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE memory_file SET use_count=use_count+1, last_used=? WHERE hash=?",
            (time.strftime("%Y-%m-%dT%H:%M:%S"), hash),
        )
        conn.commit()
    finally:
        conn.close()


def search_memory(query: str, limit: int = 10) -> list[dict]:
    """检索：FTS5 优先，失败回退 LIKE。"""
    q = str(query or "").strip()
    if not q:
        return []
    init_memory()
    conn = _connect()
    try:
        if _has_fts(conn):
            try:
                rows = conn.execute(
                    "SELECT hash, summary, keywords FROM %s WHERE %s MATCH ? LIMIT ?"
                    % (_FTS, _FTS), (q, limit)
                ).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        like = "%" + q + "%"
        rows = conn.execute(
            "SELECT hash, summary, keywords FROM memory_file "
            "WHERE summary LIKE ? OR keywords LIKE ? LIMIT ?",
            (like, like, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
