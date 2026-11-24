# -*- coding: utf-8 -*-
"""频道工作区：DB 绑定（一频道一路径）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from db import database as db


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    return path


def test_add_channel_with_workspace(tmp_db, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    cid = db.add_channel("1", workspace_path=str(ws))
    assert db.get_channel_workspace(cid) == str(ws)
    rows = db.list_channels()
    assert rows[0][0] == cid
    assert rows[0][1] == "1"
    assert rows[0][2] == str(ws)


def test_set_channel_workspace(tmp_db, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cid = db.add_channel("2", workspace_path=str(a))
    db.set_channel_workspace(cid, str(b))
    assert db.get_channel_workspace(cid) == str(b)


def test_migrate_adds_workspace_column(tmp_path, monkeypatch):
    """旧库无 workspace_path 时 init_db 会 ALTER。"""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE channels(id INTEGER PRIMARY KEY, name TEXT NOT NULL,"
        " created_at TEXT)"
    )
    conn.execute("INSERT INTO channels(name) VALUES('old')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    cols = [r[1] for r in sqlite3.connect(path).execute(
        "PRAGMA table_info(channels)").fetchall()]
    assert "workspace_path" in cols
    rows = db.list_channels()
    assert rows[0][1] == "old"
    assert rows[0][2] is None
