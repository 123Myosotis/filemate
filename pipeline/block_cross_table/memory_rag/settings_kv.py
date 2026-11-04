# -*- coding: utf-8 -*-
"""SQLite-backed settings mirror (web UI) + config.json sync for maibot_data_dir."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from pipeline.block_cross_table.memory_rag.constants import (
    SETTING_MAIBOT_DATA_DIR,
    TABLE_SETTINGS_KV,
)
from pipeline.block_cross_table.memory_rag.merge_settings import (
    resolve_maibot_data_dir,
    save_maibot_data_dir,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        f"""
        INSERT INTO {TABLE_SETTINGS_KV}(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (str(key), str(value), _now()),
    )
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute(
        f"SELECT value FROM {TABLE_SETTINGS_KV} WHERE key=?",
        (str(key),),
    ).fetchone()
    if row is None:
        return default
    return str(row[0] or default)


def get_maibot_data_dir(conn: sqlite3.Connection | None = None) -> str:
    if conn is not None:
        local = get_setting(conn, SETTING_MAIBOT_DATA_DIR, "")
        if local.strip():
            return local.strip()
    return resolve_maibot_data_dir()


def set_maibot_data_dir(conn: sqlite3.Connection | None, path: str) -> str:
    cleaned = save_maibot_data_dir(path)
    if conn is not None:
        set_setting(conn, SETTING_MAIBOT_DATA_DIR, cleaned)
    return cleaned
