# -*- coding: utf-8 -*-
"""内容标签库（新表，不碰旧表）。

按 content_hash 存 1~3 个白名单标签；source=rule/soft/user。
标签只在本地库，不写进 docx。开关 tags.enabled 关掉后调用方应直接 no-op。
库路径与 db.database.DB_PATH 同一文件（可 monkeypatch 隔离测试）。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from db.database import DB_PATH
from pipeline.stage2_classifier.doc_classifier import TYPE_LABELS

_DDL = """
CREATE TABLE IF NOT EXISTS content_tags(
    content_hash TEXT PRIMARY KEY,
    path         TEXT,
    tags_json    TEXT NOT NULL,
    source       TEXT NOT NULL,
    confidence   REAL DEFAULT 0,
    updated_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_content_tags_updated
    ON content_tags(updated_at);
CREATE TABLE IF NOT EXISTS content_tags_meta(
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# 白名单：英文键 + 中文目录名 + 口语短称（纠正/检索用）
_TAG_ALIASES = {}
for _k, _zh in TYPE_LABELS.items():
    _TAG_ALIASES[_k.lower()] = _zh
    _TAG_ALIASES[_zh] = _zh
_TAG_ALIASES.update({
    "合同": "合同协议",
    "协议": "合同协议",
    "项目": "项目事务",
    "事务": "项目事务",
    "技术": "技术文档",
    "小说": "小说叙事",
    "叙事": "小说叙事",
    "指南": "指南教程",
    "教程": "指南教程",
    "论文": "学术论文",
    "学术": "学术论文",
    "报告": "报告",
    "实验报告": "报告",
    "剧本": "剧本",
    "分镜": "剧本",
})

def tags_enabled() -> bool:
    try:
        import config
        return bool(config.get_param("tags.enabled", True))
    except Exception:  # noqa: BLE001
        return True


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_content_tags() -> None:
    """幂等建表；失败不抛到业务（首启失败不影响老功能）。"""
    try:
        conn = _conn()
        try:
            conn.executescript(_DDL)
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def normalize_tags(raw) -> list[str]:
    """收成 1~3 个中文白名单标签；非法丢弃。"""
    items = []
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    out = []
    seen = set()
    for it in items:
        s = str(it or "").strip()
        if not s:
            continue
        zh = _TAG_ALIASES.get(s) or _TAG_ALIASES.get(s.lower())
        if not zh:
            for k, v in TYPE_LABELS.items():
                if s == k or s.lower() == k.lower() or s == v:
                    zh = v
                    break
        if not zh or zh in seen:
            continue
        seen.add(zh)
        out.append(zh)
        if len(out) >= 3:
            break
    return out


def upsert_tags(
    content_hash: str,
    path: str,
    tags,
    *,
    source: str = "rule",
    confidence: float = 0.0,
    force: bool = False,
) -> bool:
    """
    写入/更新标签。同 hash upsert。
    source=user 总是覆盖；否则若已有 user 且非 force → 不覆盖纠正。
    """
    if not tags_enabled():
        return False
    h = str(content_hash or "").strip()
    if not h:
        return False
    cleaned = normalize_tags(tags)
    if not cleaned:
        return False
    src = str(source or "rule").strip() or "rule"
    init_content_tags()
    try:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT source FROM content_tags WHERE content_hash=?", (h,)
            ).fetchone()
            if (
                row is not None
                and str(row["source"] or "") == "user"
                and src != "user"
                and not force
            ):
                conn.execute(
                    "UPDATE content_tags SET path=?, updated_at=? WHERE content_hash=?",
                    (str(path or ""), _now(), h),
                )
                conn.commit()
                return True
            conn.execute(
                "INSERT INTO content_tags"
                "(content_hash, path, tags_json, source, confidence, updated_at) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(content_hash) DO UPDATE SET "
                "path=excluded.path, tags_json=excluded.tags_json, "
                "source=excluded.source, confidence=excluded.confidence, "
                "updated_at=excluded.updated_at",
                (
                    h,
                    str(path or ""),
                    json.dumps(cleaned, ensure_ascii=False),
                    src,
                    float(confidence or 0),
                    _now(),
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return False


def get_tags(content_hash: str) -> dict | None:
    if not tags_enabled():
        return None
    h = str(content_hash or "").strip()
    if not h:
        return None
    init_content_tags()
    try:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT * FROM content_tags WHERE content_hash=?", (h,)
            ).fetchone()
            if row is None:
                return None
            tags = json.loads(row["tags_json"] or "[]")
            if not isinstance(tags, list):
                tags = []
            return {
                "content_hash": row["content_hash"],
                "path": row["path"] or "",
                "tags": tags,
                "source": row["source"] or "",
                "confidence": float(row["confidence"] or 0),
                "updated_at": row["updated_at"] or "",
            }
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def _row_to_hit(row) -> dict:
    try:
        tags = json.loads(row["tags_json"] or "[]")
    except json.JSONDecodeError:
        tags = []
    if not isinstance(tags, list):
        tags = []
    return {
        "content_hash": row["content_hash"],
        "path": row["path"] or "",
        "tags": tags,
        "source": row["source"] or "",
        "confidence": float(row["confidence"] or 0),
    }


def _path_alive(path: str) -> bool:
    p = str(path or "").strip()
    if not p:
        return False
    try:
        return Path(p).is_file()
    except OSError:
        return False


def prune_missing_paths(*, also_temp: bool = True) -> int:
    """删掉路径已不存在（及可选 pytest 临时目录）的标签行。返回删除条数。"""
    init_content_tags()
    removed = 0
    try:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT content_hash, path FROM content_tags"
            ).fetchall()
            for row in rows:
                path = str(row["path"] or "")
                dead = not _path_alive(path)
                if (
                    not dead
                    and also_temp
                    and ("pytest-" in path or "\\Temp\\pytest" in path
                         or "/Temp/pytest" in path)
                ):
                    # 仍指向临时目录的测试残留，即使偶发还在也清掉
                    dead = "pytest-of-" in path or "pytest-" in path
                if dead:
                    conn.execute(
                        "DELETE FROM content_tags WHERE content_hash=?",
                        (row["content_hash"],),
                    )
                    removed += 1
            if removed:
                conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return removed
    return removed


def find_by_tag(
    tag: str,
    *,
    limit: int = 50,
    require_alive: bool = True,
) -> list[dict]:
    """
    按标签检索。SQL 侧先用 LIKE 收窄，再精确匹配 JSON 元素。
    require_alive=True 时跳过（并顺带删除）失效路径。
    """
    if not tags_enabled():
        return []
    needle_raw = str(tag or "").strip()
    if not needle_raw:
        return []
    want = normalize_tags([needle_raw])
    key = want[0] if want else ""
    init_content_tags()
    lim = max(1, min(int(limit), 200))
    # LIKE 收窄：精确标签用 "标签" 片段；模糊用原文
    like = f'%"{key}"%' if key else f"%{needle_raw}%"
    try:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT * FROM content_tags "
                "WHERE tags_json LIKE ? "
                "ORDER BY updated_at DESC",
                (like,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []

    out = []
    dead_hashes = []
    for row in rows:
        hit = _row_to_hit(row)
        tags = hit["tags"]
        if key:
            if key not in tags:
                continue
        else:
            if not any(needle_raw in str(t) for t in tags):
                continue
        path = hit["path"]
        if require_alive and not _path_alive(path):
            dead_hashes.append(hit["content_hash"])
            continue
        out.append(hit)
        if len(out) >= lim:
            break

    if dead_hashes:
        try:
            conn = _conn()
            try:
                conn.executemany(
                    "DELETE FROM content_tags WHERE content_hash=?",
                    [(h,) for h in dead_hashes],
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass
    return out


def _meta_get(key: str) -> str:
    init_content_tags()
    try:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT value FROM content_tags_meta WHERE key=?", (key,)
            ).fetchone()
            return str(row["value"]) if row else ""
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return ""


def _meta_set(key: str, value: str) -> None:
    init_content_tags()
    try:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO content_tags_meta(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def backfill_from_content_dir(root_out: Path) -> dict:
    """
    对 内容分类/<类名>/*.docx 回填：目录名→标签，hash 内容。
    幂等：已有 user 不覆盖；已有同 hash 可更新 path。
    """
    if not tags_enabled():
        return {"ok": False, "reason": "disabled", "written": 0}
    root = Path(root_out)
    if not root.is_dir():
        return {"ok": False, "reason": "no_dir", "written": 0}
    init_content_tags()
    written = 0
    skipped = 0
    zh_dirs = set(TYPE_LABELS.values())
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name not in zh_dirs:
            continue
        tag = folder.name
        for p in sorted(folder.glob("*.docx")):
            if p.name.startswith("~$"):
                continue
            try:
                raw = p.read_bytes()
            except OSError:
                skipped += 1
                continue
            if not raw:
                skipped += 1
                continue
            import hashlib
            digest = hashlib.sha256(raw).hexdigest()
            existing = get_tags(digest)
            if existing and existing.get("source") == "user":
                upsert_tags(
                    digest, str(p.resolve()), existing["tags"],
                    source="user",
                    confidence=float(existing.get("confidence") or 1),
                )
                skipped += 1
                continue
            if upsert_tags(
                digest, str(p.resolve()), [tag],
                source="rule", confidence=1.0,
            ):
                written += 1
            else:
                skipped += 1
    return {"ok": True, "written": written, "skipped": skipped, "root": str(root)}


def ensure_auto_backfill(work_or_root: Path, *, force: bool = False) -> dict:
    """
    首次启用：对「内容分类/」自动回填一次（按目录绝对路径记 meta）。
    force=True 时忽略已回填标记（手动「回填标签」）。
    """
    if not tags_enabled():
        return {"ok": False, "reason": "disabled", "written": 0, "auto": False}
    base = Path(work_or_root)
    root = base if base.name == "内容分类" else base / "内容分类"
    try:
        key = "backfill:" + str(root.resolve()).lower()
    except OSError:
        key = "backfill:" + str(root).lower()
    if not force and _meta_get(key) == "1":
        return {"ok": True, "reason": "already", "written": 0, "auto": True, "skipped": 0}
    result = backfill_from_content_dir(root)
    if result.get("ok"):
        _meta_set(key, "1")
    result["auto"] = not force
    return result


def allowed_tag_names() -> list[str]:
    return list(TYPE_LABELS.values())


def apply_user_override(
    content_hash: str,
    path: str,
    tags,
) -> bool:
    """
    用户纠正：choice_memory 记一条 + content_tags source=user。
    只改标签，不搬文件。下次同 hash 分类不会覆盖（除非 force）。
    """
    if not tags_enabled():
        return False
    h = str(content_hash or "").strip()
    cleaned = normalize_tags(tags)
    if not h or not cleaned:
        return False
    try:
        from db import choice_memory

        choice_memory.record(
            "content_tag",
            h,
            "tags",
            {"tags": cleaned, "file_hash": h, "path": str(path or "")},
        )
    except Exception:  # noqa: BLE001
        pass
    return upsert_tags(
        h, str(path or ""), cleaned, source="user", confidence=1.0,
    )


def resolve_tags_for_hash(content_hash: str) -> dict | None:
    """查询：choice_memory 纠正优先，否则 content_tags。"""
    if not tags_enabled():
        return None
    h = str(content_hash or "").strip()
    if not h:
        return None
    try:
        from db import choice_memory

        rec = choice_memory.lookup("content_tag", h, "tags", file_hash=h)
        if rec and isinstance(rec.get("value"), dict):
            tags = normalize_tags(rec["value"].get("tags"))
            if tags:
                choice_memory.touch(rec.get("choice_id") or "")
                path = str(rec["value"].get("path") or "")
                row = get_tags(h)
                if row and not path:
                    path = row.get("path") or ""
                return {
                    "content_hash": h,
                    "path": path,
                    "tags": tags,
                    "source": "user",
                    "confidence": 1.0,
                    "updated_at": "",
                }
    except Exception:  # noqa: BLE001
        pass
    return get_tags(h)
