# infra/cache_db.py
"""
缓存层：把"可以安全复用的中间结果"落 SQLite，重复处理同一文件时跳过重复计算。
- doc_type_cache: 内容 hash → 文档类型（内容不变判别结果就不变，避免重复计算）
- file_summary:   文件 path → mtime/hash/摘要（同一版本文件不重复摘要）
- table_profile:  CSV 列类型画像
- field_mapping:  用户确认过的字段映射（跨表联查用）
与业务库共用 db/filemate.db，表名不冲突、互不干扰。
"""
import json  # scores/profile 等结构化数据以 JSON 文本落库
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "filemate.db"  # 与业务库同一文件


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")  # 提升并发读写稳定性
    return conn


def init_cache_tables():
    """建缓存表（幂等，重复调用无副作用）"""
    conn = _conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS doc_type_cache(
        content_hash TEXT PRIMARY KEY,          -- 文件内容 sha256
        doc_type     TEXT NOT NULL,             -- 判别出的文档类型
        scores       TEXT,                      -- 各类型得分 JSON（透明可查）
        created_at   TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS file_summary(
        path       TEXT PRIMARY KEY,            -- 文件绝对路径
        mtime      REAL,                        -- 修改时间，变了就失效
        hash       TEXT,                        -- 内容 hash
        summary    TEXT,                        -- 摘要文本
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS table_profile(
        path       TEXT PRIMARY KEY,            -- 表格文件路径
        mtime      REAL,
        profile    TEXT,                        -- 列类型画像 JSON
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS field_mapping(
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_type  TEXT NOT NULL,
        field     TEXT NOT NULL,
        mapped_to TEXT NOT NULL,
        UNIQUE(doc_type, field)                -- 同类型同字段只留一条
    );
    """)
    conn.commit()
    conn.close()


# ------------------------------------------------------------------
# doc_type_cache：判别结果缓存
# ------------------------------------------------------------------

def get_doc_type(content_hash: str):
    """按内容 hash 查缓存的判别结果；未命中返回 None"""
    conn = _conn()
    row = conn.execute(
        "SELECT doc_type, scores FROM doc_type_cache WHERE content_hash=?", (content_hash,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    scores = json.loads(row[1]) if row[1] else {}
    return {"doc_type": row[0], "scores": scores}


def set_doc_type(content_hash: str, doc_type: str, scores: dict):
    """写入判别结果缓存（同 hash 覆盖旧值）"""
    conn = _conn()
    conn.execute(
        "INSERT OR REPLACE INTO doc_type_cache(content_hash, doc_type, scores) VALUES(?,?,?)",
        (content_hash, doc_type, json.dumps(scores, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


# ------------------------------------------------------------------
# file_summary：文件摘要缓存
# ------------------------------------------------------------------

def get_file_summary(path: str, mtime: float):
    """取文件摘要；mtime 变了视为失效返回 None"""
    conn = _conn()
    row = conn.execute("SELECT mtime, summary FROM file_summary WHERE path=?", (path,)).fetchone()
    conn.close()
    if row is None or row[0] != mtime:
        return None
    return row[1]


def set_file_summary(path: str, mtime: float, summary: str):
    conn = _conn()
    conn.execute(
        "INSERT OR REPLACE INTO file_summary(path, mtime, summary) VALUES(?,?,?)",
        (path, mtime, summary),
    )
    conn.commit()
    conn.close()


# ------------------------------------------------------------------
# table_profile：表格画像缓存
# ------------------------------------------------------------------

def get_table_profile(path: str, mtime: float):
    conn = _conn()
    row = conn.execute("SELECT mtime, profile FROM table_profile WHERE path=?", (path,)).fetchone()
    conn.close()
    if row is None or row[0] != mtime:
        return None
    return json.loads(row[1])


def set_table_profile(path: str, mtime: float, profile: dict):
    conn = _conn()
    conn.execute(
        "INSERT OR REPLACE INTO table_profile(path, mtime, profile) VALUES(?,?,?)",
        (path, mtime, json.dumps(profile, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


# ------------------------------------------------------------------
# field_mapping：字段映射（导出模板 / 跨表联查用）
# ------------------------------------------------------------------

def set_field_mapping(doc_type: str, field: str, mapped_to: str):
    conn = _conn()
    conn.execute(
        "INSERT OR REPLACE INTO field_mapping(doc_type, field, mapped_to) VALUES(?,?,?)",
        (doc_type, field, mapped_to),
    )
    conn.commit()
    conn.close()


def get_field_mappings(doc_type: str) -> dict:
    """取某文档类型的全部字段映射 {field: mapped_to}"""
    conn = _conn()
    rows = conn.execute(
        "SELECT field, mapped_to FROM field_mapping WHERE doc_type=?", (doc_type,)
    ).fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}
