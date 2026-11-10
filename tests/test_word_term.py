# -*- coding: utf-8 -*-
"""阶段 ②：术语 + 术语↔锚点 + 生命周期。"""
from __future__ import annotations

import pytest

from pipeline.word_office import memory


@pytest.fixture(autouse=True)
def _clean():
    memory.init_memory()
    import sqlite3
    from db.database import DB_PATH
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("DELETE FROM memory_term WHERE term LIKE 'test-%'")
        conn.execute("DELETE FROM memory_term_anchor WHERE file_hash LIKE 'test-%'")
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    finally:
        conn.close()


def test_upsert_and_get_term():
    tid = memory.upsert_term("test-设备", "设备、机器", 1.0)
    t = memory.get_term("test-设备")
    assert t is not None
    assert t["term_id"] == tid
    assert t["status"] == "active"


def test_link_and_get_anchors():
    tid = memory.upsert_term("test-台账", "", 1.0)
    memory.link_anchor(tid, "test-hash", "para:3", "设备台账段落")
    anchors = memory.get_anchors(tid)
    assert len(anchors) == 1
    assert anchors[0]["locator"] == "para:3"


def test_lifecycle():
    tid = memory.upsert_term("test-x", "", 1.0)
    assert memory.set_status(tid, "invalidated") is True
    assert memory.get_term("test-x")["status"] == "invalidated"
    assert memory.set_status(tid, "bad") is False
