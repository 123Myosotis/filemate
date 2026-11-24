# db/database.py
"""
业务库持久化层：频道、对话消息、操作日志。

- 默认 SQLite（零配置、单文件）；设置 `FILEMATE_DB_BACKEND=mysql`
  或 config.json 的 `db.backend` 可切到 MySQL。
- 两种后端共用同一套函数签名，SQL 占位符统一写 `?`，MySQL 侧自动翻译为 `%s`。
- 撤回只是标记 undone=1，记录本身永远保留；
  只有用户手动删除/清空日志时才真正删除记录。
"""
import json  # undo_info 序列化为 JSON 存储
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "filemate.db"  # SQLite 数据库文件放在 db/ 目录下
MYSQL_SCHEMA_PATH = Path(__file__).parent / "mysql_schema.sql"


# ------------------------------------------------------------------
# 后端选择
# ------------------------------------------------------------------

def _cfg(key: str, default=None):
    """读 config.json；配置文件缺失/损坏时退回 default，不影响启动。"""
    try:
        import config

        return config.get_param(key, default)
    except Exception:
        return default


def backend() -> str:
    """当前后端：环境变量 > config.json > 默认 sqlite。"""
    raw = os.environ.get("FILEMATE_DB_BACKEND") or _cfg("db.backend") or "sqlite"
    raw = str(raw).strip().lower()
    return "mysql" if raw in ("mysql", "mariadb") else "sqlite"


def mysql_settings() -> dict:
    """MySQL 连接参数：环境变量 > config.json > 默认值。"""
    def pick(env_key: str, cfg_key: str, default):
        val = os.environ.get(env_key)
        if val is None or str(val).strip() == "":
            val = _cfg(cfg_key, default)
        if val is None or str(val).strip() == "":
            val = default
        return val

    return {
        "host": str(pick("FILEMATE_DB_HOST", "db.mysql.host", "127.0.0.1")),
        "port": int(pick("FILEMATE_DB_PORT", "db.mysql.port", 3306)),
        "user": str(pick("FILEMATE_DB_USER", "db.mysql.user", "root")),
        "password": str(pick("FILEMATE_DB_PASSWORD", "db.mysql.password", "")),
        "database": str(pick("FILEMATE_DB_NAME", "db.mysql.database", "filemate")),
        "charset": "utf8mb4",
    }


def _mysql_connect():
    """延迟导入 PyMySQL：只装 SQLite 的用户不需要它。"""
    try:
        import pymysql
    except ImportError as exc:  # pragma: no cover - 取决于本机是否装 PyMySQL
        raise RuntimeError(
            "MySQL 后端需要 PyMySQL，请先执行：pip install PyMySQL"
        ) from exc
    settings = mysql_settings()
    return pymysql.connect(
        host=settings["host"],
        port=settings["port"],
        user=settings["user"],
        password=settings["password"],
        database=settings["database"],
        charset=settings["charset"],
        autocommit=False,
        connect_timeout=5,
    )


class _MysqlConn:
    """把 pymysql 连接适配成 sqlite3.Connection 的最小接口。"""

    def __init__(self, raw):
        self._raw = raw
        self._cur = raw.cursor()

    def execute(self, sql: str, params=()):
        self._cur.execute(sql.replace("?", "%s"), tuple(params or ()))
        return self._cur

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        try:
            self._cur.close()
        finally:
            self._raw.close()


def _conn():
    if backend() == "mysql":
        return _MysqlConn(_mysql_connect())
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")  # 提升并发读写稳定性
    return conn


def _split_sql(script: str) -> list:
    """按分号切 SQL；忽略空行与 `--` 注释行。"""
    lines = []
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        lines.append(line)
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


def _init_mysql() -> None:
    script = MYSQL_SCHEMA_PATH.read_text(encoding="utf-8")
    conn = _conn()
    try:
        for stmt in _split_sql(script):
            conn.execute(stmt)
        conn.commit()
        cols = {row[0] for row in conn.execute("SHOW COLUMNS FROM channels").fetchall()}
        if "workspace_path" not in cols:
            conn.execute("ALTER TABLE channels ADD COLUMN workspace_path VARCHAR(1024) NULL")
            conn.commit()
    finally:
        conn.close()


def init_db():
    """建表（幂等，重复调用无副作用）"""
    if backend() == "mysql":
        _init_mysql()
        return

    conn = _conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS channels(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        workspace_path TEXT,
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
    # 旧库升级：补 workspace_path（一频道一工作区）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(channels)").fetchall()]
    if "workspace_path" not in cols:
        conn.execute("ALTER TABLE channels ADD COLUMN workspace_path TEXT")
    conn.commit()
    conn.close()


# ------------------------------------------------------------------
# 频道
# ------------------------------------------------------------------

def add_channel(name: str, workspace_path: str | None = None) -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO channels(name, workspace_path) VALUES(?,?)",
        (name, workspace_path),
    )
    conn.commit()
    channel_id = cur.lastrowid
    conn.close()
    return channel_id


def list_channels():
    """返回 [(id, name, workspace_path), ...]"""
    conn = _conn()
    rows = conn.execute(
        "SELECT id, name, workspace_path FROM channels ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


def set_channel_workspace(channel_id: int, workspace_path: str | None) -> None:
    conn = _conn()
    conn.execute(
        "UPDATE channels SET workspace_path=? WHERE id=?",
        (workspace_path, int(channel_id)),
    )
    conn.commit()
    conn.close()


def get_channel_workspace(channel_id: int) -> str | None:
    conn = _conn()
    row = conn.execute(
        "SELECT workspace_path FROM channels WHERE id=?",
        (int(channel_id),),
    ).fetchone()
    conn.close()
    if not row:
        return None
    val = row[0]
    return str(val) if val else None


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

def add_message(channel_id: int, sender: str, content: str) -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO messages(channel_id, sender, content) VALUES(?,?,?)",
        (channel_id, sender, content))
    conn.commit()
    msg_id = int(cur.lastrowid or 0)
    conn.close()
    return msg_id


def list_messages(channel_id: int):
    conn = _conn()
    rows = conn.execute(
        "SELECT sender, content FROM messages WHERE channel_id=? ORDER BY id",
        (channel_id,)
    ).fetchall()
    conn.close()
    return rows


def delete_messages_by_ids(channel_id: int, message_ids) -> int:
    """按 id 删除本频道消息（撤回本轮 AI 回应）。返回删除条数。"""
    ids = [int(x) for x in (message_ids or []) if x]
    if not ids:
        return 0
    conn = _conn()
    n = 0
    for mid in ids:
        cur = conn.execute(
            "DELETE FROM messages WHERE channel_id=? AND id=?",
            (channel_id, mid))
        n += cur.rowcount or 0
    conn.commit()
    conn.close()
    return n


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
