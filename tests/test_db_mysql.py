# -*- coding: utf-8 -*-
"""业务库双后端：后端选择、占位符翻译、SQLite 回归；MySQL 集成需显式开启。"""
import os

import pytest

from db import database as db


def test_backend_env_switch(monkeypatch):
    monkeypatch.setattr(db, "_cfg", lambda key, default=None: default)
    monkeypatch.setenv("FILEMATE_DB_BACKEND", "mysql")
    assert db.backend() == "mysql"
    monkeypatch.setenv("FILEMATE_DB_BACKEND", "mariadb")
    assert db.backend() == "mysql"
    monkeypatch.setenv("FILEMATE_DB_BACKEND", "sqlite")
    assert db.backend() == "sqlite"
    monkeypatch.setenv("FILEMATE_DB_BACKEND", "MySQL ")
    assert db.backend() == "mysql"


def test_mysql_placeholder_translation():
    class FakeCursor:
        def __init__(self):
            self.sql = None
            self.params = None

        def execute(self, sql, params):
            self.sql, self.params = sql, params

        def close(self):
            pass

    class FakeRaw:
        def __init__(self):
            self.cursor_obj = FakeCursor()

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    raw = FakeRaw()
    conn = db._MysqlConn(raw)
    conn.execute("SELECT * FROM t WHERE id=? AND name=?", (1, "a"))
    assert raw.cursor_obj.sql == "SELECT * FROM t WHERE id=%s AND name=%s"
    assert raw.cursor_obj.params == (1, "a")


def test_sqlite_roundtrip(monkeypatch, tmp_path):
    monkeypatch.delenv("FILEMATE_DB_BACKEND", raising=False)
    monkeypatch.setattr(db, "_cfg", lambda key, default=None: default)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "biz.db")

    db.init_db()
    cid = db.add_channel("测试频道")
    assert cid > 0
    db.set_channel_workspace(cid, str(tmp_path))
    assert db.get_channel_workspace(cid) == str(tmp_path)

    mid = db.add_message(cid, "user", "你好")
    assert mid > 0
    assert db.list_messages(cid) == [("user", "你好")]
    assert db.delete_messages_by_ids(cid, [mid]) == 1

    lid = db.add_log("整理桌面", {"moves": []})
    assert lid > 0
    db.mark_log_undone(lid)
    assert db.list_logs()[0][3] == 1

    # 幂等：重复建表不报错、旧数据仍在
    db.init_db()
    assert len(db.list_channels()) == 1


@pytest.mark.skipif(
    os.environ.get("FILEMATE_MYSQL_TEST") != "1",
    reason="需要真实 MySQL：设置 FILEMATE_MYSQL_TEST=1 后运行",
)
def test_mysql_roundtrip(monkeypatch):
    pytest.importorskip("pymysql")
    monkeypatch.setenv("FILEMATE_DB_BACKEND", "mysql")
    monkeypatch.setattr(db, "_cfg", lambda key, default=None: default)

    db.init_db()
    cid = db.add_channel("mysql 测试频道")
    try:
        assert cid > 0
        db.add_message(cid, "ai", "ok")
        assert db.list_messages(cid)[0] == ("ai", "ok")
    finally:
        db.delete_channel(cid)
