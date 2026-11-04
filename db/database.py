# db/database.py
"""
SQLite 持久化层：频道、对话消息、操作日志全部落库，重启后恢复。
- 撤回只是标记 undone=1，记录本身永远保留
- 只有用户手动删除/清空日志时才真正删除记录
"""
import json  # undo_info 序列化为 JSON 存储
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "filemate.db"  # 数据库文件放在 db/ 目录下


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")  # 提升并发读写稳定性
    return conn


def init_db():
    """建表（幂等，重复调用无副作用）"""
    conn = _conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS channels(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER NOT NULL,
        sender TEXT NOT NULL,            -- 'user' 或 'ai'
        content TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT NOT NULL,
        undo_info TEXT,                  -- JSON 字符串，NULL 表示不可撤回
        undone INTEGER DEFAULT 0,        -- 0 未撤回 / 1 已撤回（记录保留）
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    """)
    conn.commit()
    conn.close()


# ------------------------------------------------------------------
# 频道
# ------------------------------------------------------------------

def add_channel(name: str) -> int:
    conn = _conn()
    cur = conn.execute("INSERT INTO channels(name) VALUES(?)", (name,))
    conn.commit()
    channel_id = cur.lastrowid
    conn.close()
    return channel_id


def list_channels():
    conn = _conn()
    rows = conn.execute("SELECT id, name FROM channels ORDER BY id").fetchall()
    conn.close()
    return rows


def delete_channel(channel_id: int):
    """删除频道及其所有消息"""
    conn = _conn()
    conn.execute("DELETE FROM messages WHERE channel_id=?", (channel_id,))
    conn.execute("DELETE FROM channels WHERE id=?", (channel_id,))
    conn.commit()
    conn.close()


# ------------------------------------------------------------------
# 消息
# ------------------------------------------------------------------

def add_message(channel_id: int, sender: str, content: str):
    conn = _conn()
    conn.execute("INSERT INTO messages(channel_id, sender, content) VALUES(?,?,?)",
                 (channel_id, sender, content))
    conn.commit()
    conn.close()


def list_messages(channel_id: int):
    conn = _conn()
    rows = conn.execute(
        "SELECT sender, content FROM messages WHERE channel_id=? ORDER BY id",
        (channel_id,)
    ).fetchall()
    conn.close()
    return rows


# ------------------------------------------------------------------
# 操作日志
# ------------------------------------------------------------------

def add_log(message: str, undo_info=None) -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO logs(message, undo_info) VALUES(?,?)",
        (message, json.dumps(undo_info, ensure_ascii=False) if undo_info else None)
    )
    conn.commit()
    log_id = cur.lastrowid
    conn.close()
    return log_id


def list_logs():
    conn = _conn()
    rows = conn.execute(
        "SELECT id, message, undo_info, undone, created_at FROM logs ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


def mark_log_undone(log_id: int):
    conn = _conn()
    conn.execute("UPDATE logs SET undone=1 WHERE id=?", (log_id,))
    conn.commit()
    conn.close()


def delete_log(log_id: int):
    conn = _conn()
    conn.execute("DELETE FROM logs WHERE id=?", (log_id,))
    conn.commit()
    conn.close()


def clear_logs():
    conn = _conn()
    conn.execute("DELETE FROM logs")
    conn.commit()
    conn.close()
