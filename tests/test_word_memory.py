# -*- coding: utf-8 -*-
"""阶段 4.1：记忆表（save / get / search / touch）。"""
from __future__ import annotations

import pytest

from pipeline.word_office import memory


@pytest.fixture(autouse=True)
def _clean():
    memory.init_memory()
    yield
    # 清掉测试用 hash，避免污染
    import sqlite3
    from db.database import DB_PATH
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("DELETE FROM memory_file WHERE hash LIKE 'test-%'")
        conn.execute("DELETE FROM memory_file_fts WHERE hash LIKE 'test-%'")
        conn.execute("DELETE FROM memory_vec WHERE hash LIKE 'test-%'")
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    finally:
        conn.close()


def test_save_get():
    memory.save_file_memory("test-a", "/tmp/a.docx", "设备台账", "设备、负责人")
    r = memory.get_file_memory("test-a")
    assert r is not None
    assert r["summary"] == "设备台账"
    assert r["keywords"] == "设备、负责人"


def _fake_embed(text, model="", timeout=20):
    # "设备/机器/台账/清单" → 同一向量；否则正交
    if any(k in (text or "") for k in ("设备", "机器", "台账", "清单")):
        return [1.0, 0.0, 0.0]
    return [0.0, 1.0, 0.0]


def test_recall_semantic_third_channel(monkeypatch):
    """向量第三路：FTS5/LIKE 字面 miss 时，语义向量也能召回。"""
    import ai.embed as emb
    monkeypatch.setattr(emb, "is_available", lambda: True)
    monkeypatch.setattr(emb, "embed", _fake_embed)
    memory.save_file_memory("test-vec-target", "/tmp/t.docx", "设备台账", "设备、负责人")
    memory.save_file_memory("test-vec-noise", "/tmp/n.docx", "无关内容", "其他")
    hits = memory.recall("机器清单", limit=5)
    hashes = [h["hash"] for h in hits]
    assert "test-vec-target" in hashes


def test_overwrite_same_hash():
    memory.save_file_memory("test-b", "/tmp/b.docx", "旧摘要", "旧")
    memory.save_file_memory("test-b", "/tmp/b.docx", "新摘要", "新")
    r = memory.get_file_memory("test-b")
    assert r["summary"] == "新摘要"


def test_search():
    memory.save_file_memory("test-c", "/tmp/c.docx", "值班表", "值班、负责人")
    hits = memory.search_memory("值班")
    assert any(h["hash"] == "test-c" for h in hits)


def test_touch():
    memory.save_file_memory("test-d", "/tmp/d.docx", "x", "y")
    memory.touch("test-d")
    r = memory.get_file_memory("test-d")
    assert r["use_count"] == 1


def test_not_found():
    assert memory.get_file_memory("test-none") is None
    assert memory.search_memory("肯定没有这个词zzz") == []


def test_session_last():
    from pipeline.word_office import session

    session.clear()
    session.remember({"file": "x.docx", "tier": "A"})
    assert session.last()["file"] == "x.docx"
    session.clear()
    assert session.last() == {}


def test_recall_rrf():
    memory.save_file_memory("test-rrf", "/tmp/r.docx", "设备台账", "设备")
    hits = memory.recall("设备")
    assert any(h["hash"] == "test-rrf" for h in hits)
    assert all("score" in h for h in hits)
