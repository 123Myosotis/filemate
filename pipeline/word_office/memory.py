# -*- coding: utf-8 -*-
"""Word 记忆（阶段 ②）：文件主题 + 术语 + 术语↔锚点 + 生命周期。

设计依据（`docs/architecture.md` §9）：
- **底座**：`memory_file`（文件主题）+ `session`（即时）——已有。
- **抄 Excel 格式**：`memory_term`（术语结构）+ 生命周期。
- **Word 独有**：`memory_term_anchor`（术语 ↔ 段落锚点）。

**不碰** Excel 的 `memory_rag`（只照格式）。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

from db.database import DB_PATH

_STATUS = ("active", "invalidated", "revoked")

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
    """CREATE TABLE IF NOT EXISTS memory_term (
        term_id TEXT PRIMARY KEY,
        term TEXT NOT NULL,
        aliases TEXT,
        weight REAL DEFAULT 1.0,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT,
        last_used TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS memory_term_anchor (
        anchor_id TEXT PRIMARY KEY,
        term_id TEXT NOT NULL,
        file_hash TEXT NOT NULL,
        locator TEXT NOT NULL,
        snippet TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS memory_vec (
        hash TEXT PRIMARY KEY,
        vec TEXT NOT NULL,
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
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_file_fts "
                "USING fts5(hash UNINDEXED, summary, keywords)"
            )
        except sqlite3.OperationalError:
            pass
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------- 文件主题（已有） ----------------

def save_file_memory(hash: str, path: str = "", summary: str = "",
                     keywords: str = "") -> None:
    if not hash:
        return
    init_memory()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO memory_file(hash, path, summary, keywords, use_count, last_used, created_at) "
            "VALUES(?,?,?,?,0,?,?) "
            "ON CONFLICT(hash) DO UPDATE SET path=excluded.path, "
            "summary=excluded.summary, keywords=excluded.keywords, last_used=excluded.last_used",
            (hash, path, summary, keywords, _now(), _now()),
        )
        try:
            conn.execute("DELETE FROM memory_file_fts WHERE hash=?", (hash,))
            conn.execute(
                "INSERT INTO memory_file_fts(hash, summary, keywords) VALUES(?,?,?)",
                (hash, summary, keywords),
            )
        except sqlite3.OperationalError:
            pass
        conn.commit()
    finally:
        conn.close()


def get_file_memory(hash: str) -> dict | None:
    init_memory()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM memory_file WHERE hash=?", (hash,)).fetchone()
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
            (_now(), hash),
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
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_file_fts'"
        ).fetchone()
        if row is not None:
            try:
                rows = conn.execute(
                    "SELECT hash, summary, keywords FROM memory_file_fts WHERE memory_file_fts MATCH ? LIMIT ?",
                    (q, limit),
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


# ---------------- 术语 + 术语↔锚点（新增） ----------------

def _stable_id(prefix: str, text: str) -> str:
    return prefix + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def upsert_term(term: str, aliases: str = "", weight: float = 1.0) -> str:
    """写/更新一个术语；返回 term_id（**稳定哈希**，可重复调用）。"""
    t = str(term).strip()
    if not t:
        return ""
    init_memory()
    term_id = _stable_id("t-", t)
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO memory_term(term_id, term, aliases, weight, status, created_at, last_used) "
            "VALUES(?,?,?,?, 'active', ?, ?) "
            "ON CONFLICT(term_id) DO UPDATE SET aliases=excluded.aliases, "
            "weight=excluded.weight, last_used=excluded.last_used",
            (term_id, t, aliases, float(weight), _now(), _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return term_id


def get_term(term: str) -> dict | None:
    init_memory()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM memory_term WHERE term=?", (str(term).strip(),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def link_anchor(term_id: str, file_hash: str, locator: str, snippet: str = "") -> str:
    """把术语挂到**锚点**（段落/标题/表格位置）。"""
    if not term_id or not file_hash or not locator:
        return ""
    init_memory()
    anchor_id = _stable_id("a-", "|".join((term_id, file_hash, locator)))
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO memory_term_anchor"
            "(anchor_id, term_id, file_hash, locator, snippet, status, created_at) "
            "VALUES(?,?,?,?,?, 'active', ?)",
            (anchor_id, term_id, file_hash, locator, snippet, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return anchor_id


def get_anchors(term_id: str, limit: int = 20) -> list[dict]:
    init_memory()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM memory_term_anchor WHERE term_id=? AND status='active' LIMIT ?",
            (term_id, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_status(term_id: str, status: str) -> bool:
    """生命周期：active / invalidated / revoked。"""
    if status not in _STATUS:
        return False
    init_memory()
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE memory_term SET status=?, last_used=? WHERE term_id=?",
            (status, _now(), term_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------- 混合召回（proto：FTS5 + LIKE → RRF） ----------------

def save_vec(hash: str, vec: list) -> None:
    """存向量（JSON）；hash 空 / vec 空 → 跳过。"""
    if not hash or not vec:
        return
    init_memory()
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO memory_vec(hash, vec, created_at) VALUES(?,?,?)",
            (hash, json.dumps([float(x) for x in vec]), _now()),
        )
        conn.commit()
    finally:
        conn.close()


def _get_vec(hash: str) -> list:
    init_memory()
    conn = _connect()
    try:
        row = conn.execute("SELECT vec FROM memory_vec WHERE hash=?", (hash,)).fetchone()
        if not row:
            return []
        try:
            return [float(x) for x in json.loads(row["vec"])]
        except Exception:  # noqa: BLE001
            return []
    finally:
        conn.close()


def _vec_hits(q: str, k: int) -> list[str]:
    """向量通道：query → embed → 与库内向量算余弦。**没 Key 返回空**。"""
    try:
        from ai import embed
        if not embed.is_available():
            return []
        qv = embed.embed(q)
        if not qv:
            return []
    except Exception:  # noqa: BLE001
        return []
    init_memory()
    conn = _connect()
    try:
        rows = conn.execute("SELECT hash FROM memory_vec").fetchall()
    finally:
        conn.close()
    scored = []
    for r in rows:
        v = _get_vec(r["hash"])
        if v:
            scored.append((r["hash"], embed.cosine(qv, v)))
    scored.sort(key=lambda kv: -kv[1])
    return [h for h, _ in scored[:k]]


def _fts_hits(q: str, k: int) -> list[str]:
    init_memory()
    conn = _connect()
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_file_fts'").fetchone() is None:
            return []
        try:
            rows = conn.execute(
                "SELECT hash FROM memory_file_fts WHERE memory_file_fts MATCH ? LIMIT ?",
                (q, k),
            ).fetchall()
            return [r["hash"] for r in rows]
        except sqlite3.OperationalError:
            return []
    finally:
        conn.close()


def _like_hits(q: str, k: int) -> list[str]:
    init_memory()
    conn = _connect()
    like = "%" + q + "%"
    try:
        rows = conn.execute(
            "SELECT hash FROM memory_file WHERE summary LIKE ? OR keywords LIKE ? LIMIT ?",
            (like, like, k),
        ).fetchall()
        return [r["hash"] for r in rows]
    finally:
        conn.close()


def recall(query: str, limit: int = 10, rrf_k: int = 60) -> list[dict]:
    """混合召回（proto）：**FTS5 + LIKE 两路 → RRF 融合**。

    **不做向量**（Word 暂无 embedding）；等有 embedding 再加第三路。
    """
    q = str(query or "").strip()
    if not q:
        return []
    channels = [_fts_hits(q, limit * 3), _like_hits(q, limit * 3)]
    # 第三路（向量）——**有 Key 才加**，没有就是两路
    try:
        vec = _vec_hits(q, limit * 3)
        if vec:
            channels.append(vec)
    except Exception:  # noqa: BLE001
        pass
    scores: dict[str, float] = {}
    for ch in channels:
        for rank, h in enumerate(ch):
            scores[h] = scores.get(h, 0.0) + 1.0 / (rrf_k + rank + 1)
    top = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
    out = []
    for h, sc in top:
        rec = get_file_memory(h) or {"hash": h}
        rec["score"] = round(sc, 6)
        out.append(rec)
    return out

_ACRONYM = re.compile(r"\b[A-Z][A-Z0-9]{1,}\b")
_QUOTED = re.compile(r"[「『《\"']([^「」『』《》\"']{2,20})[」』》\"']")
_ZH_SEG = re.compile(r"[\u4e00-\u9fff]{2,}")


def mine_terms(text: str, min_freq: int = 3) -> list[str]:
    """挖候选术语：英文缩写 + 引号内 + 重复 n-gram（2–4 字，频率 ≥ min_freq）。"""
    if not text:
        return []
    cands: list[str] = []
    cands += _ACRONYM.findall(text)
    cands += _QUOTED.findall(text)
    cnt: dict[str, int] = {}
    for seg in _ZH_SEG.findall(text):
        for n in (2, 3, 4):
            for i in range(len(seg) - n + 1):
                g = seg[i:i + n]
                cnt[g] = cnt.get(g, 0) + 1
    freq = [g for g, c in cnt.items() if c >= int(min_freq)]
    # 只留最长：若 g 是另一个候选的子串，丢弃（避免"设备台/备台账"这类碎片）
    freq = [g for g in freq if not any(g != h and g in h for h in freq)]
    cands += freq
    seen: set = set()
    out: list[str] = []
    for c in cands:
        c = str(c).strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def mine_and_save(text: str, min_freq: int = 3) -> list[str]:
    """挖术语并写入 `memory_term`；返回写入的术语。"""
    terms = mine_terms(text, min_freq=min_freq)
    for t in terms:
        upsert_term(t, aliases="", weight=1.0)
    return terms
