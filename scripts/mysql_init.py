# -*- coding: utf-8 -*-
"""MySQL 初始化 / 迁移脚本。

用法：
  python scripts/mysql_init.py                     # 建库建表（库不存在会先创建）
  python scripts/mysql_init.py --wait 60           # 等 MySQL 就绪后再建（Docker 启动用）
  python scripts/mysql_init.py --migrate-sqlite db/filemate.db
  python scripts/mysql_init.py --migrate-sqlite db/filemate.db --truncate

连接参数按 环境变量 > config.json > 默认值 读取，详见 db/database.py。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import database as db  # noqa: E402


def _raw_connect(with_database: bool):
    import pymysql

    s = db.mysql_settings()
    return pymysql.connect(
        host=s["host"],
        port=s["port"],
        user=s["user"],
        password=s["password"],
        database=s["database"] if with_database else None,
        charset=s["charset"],
        autocommit=True,
        connect_timeout=5,
    )


def wait_ready(seconds: int) -> None:
    if seconds <= 0:
        return
    deadline = time.time() + seconds
    last_error = None
    while time.time() < deadline:
        try:
            _raw_connect(with_database=False).close()
            return
        except Exception as exc:  # noqa: BLE001 - 就绪探测需要吞掉连接异常
            last_error = exc
            time.sleep(2)
    raise SystemExit(f"MySQL 在 {seconds}s 内未就绪：{last_error}")


def ensure_database() -> None:
    s = db.mysql_settings()
    name = s["database"].replace("`", "")
    conn = _raw_connect(with_database=False)
    try:
        cur = conn.cursor()
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{name}` "
            "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    finally:
        conn.close()


def create_tables() -> None:
    script = db.MYSQL_SCHEMA_PATH.read_text(encoding="utf-8")
    conn = _raw_connect(with_database=True)
    try:
        cur = conn.cursor()
        for stmt in db._split_sql(script):
            cur.execute(stmt)
    finally:
        conn.close()


def _counts(conn) -> dict:
    cur = conn.cursor()
    out = {}
    for table in ("channels", "messages", "logs"):
        cur.execute(f"SELECT COUNT(*) FROM `{table}`")
        out[table] = int(cur.fetchone()[0])
    return out


def migrate_sqlite(sqlite_path: Path, truncate: bool) -> None:
    if not sqlite_path.exists():
        raise SystemExit(f"找不到 SQLite 文件：{sqlite_path}")

    src = sqlite3.connect(str(sqlite_path))
    dst = _raw_connect(with_database=True)
    try:
        counts = _counts(dst)
        if any(counts.values()) and not truncate:
            raise SystemExit(
                f"目标库非空 {counts}；确认要覆盖请加 --truncate"
            )
        cur = dst.cursor()
        if truncate:
            for table in ("messages", "logs", "channels"):
                cur.execute(f"DELETE FROM `{table}`")

        # 保留原 id，避免 messages.channel_id 对不上
        for row in src.execute("SELECT id, name, workspace_path, created_at FROM channels"):
            cur.execute(
                "INSERT INTO channels(id, name, workspace_path, created_at) VALUES(%s,%s,%s,%s)",
                row,
            )
        for row in src.execute("SELECT id, channel_id, sender, content, created_at FROM messages"):
            cur.execute(
                "INSERT INTO messages(id, channel_id, sender, content, created_at) "
                "VALUES(%s,%s,%s,%s,%s)",
                row,
            )
        for row in src.execute("SELECT id, message, undo_info, undone, created_at FROM logs"):
            cur.execute(
                "INSERT INTO logs(id, message, undo_info, undone, created_at) "
                "VALUES(%s,%s,%s,%s,%s)",
                row,
            )
        for table in ("channels", "messages", "logs"):
            cur.execute(f"ALTER TABLE `{table}` AUTO_INCREMENT = 1")
    finally:
        src.close()
        dst.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="FileMate MySQL 初始化 / 迁移")
    parser.add_argument("--wait", type=int, default=0, help="等待 MySQL 就绪的秒数")
    parser.add_argument("--migrate-sqlite", type=Path, default=None, help="从 SQLite 文件导数据")
    parser.add_argument("--truncate", action="store_true", help="迁移前清空目标表")
    args = parser.parse_args()

    wait_ready(args.wait)
    ensure_database()
    create_tables()

    dst = _raw_connect(with_database=True)
    try:
        print(f"MySQL 就绪：{db.mysql_settings()['host']}:{db.mysql_settings()['port']}"
              f"/{db.mysql_settings()['database']}　表：{_counts(dst)}")
    finally:
        dst.close()

    if args.migrate_sqlite:
        migrate_sqlite(args.migrate_sqlite, args.truncate)
        dst = _raw_connect(with_database=True)
        try:
            print(f"迁移完成：{_counts(dst)}")
        finally:
            dst.close()


if __name__ == "__main__":
    main()
