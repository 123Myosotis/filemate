# -*- coding: utf-8 -*-
"""清理分类缓存：id 已换才删；root 缺失有宽限；聊天须确认。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from agent_core.local_file_intent import (
    wants_cleanup_content_cache,
    wants_confirm_cleanup_content_cache,
)
from engine.dispatcher import IntentDispatcher
from pipeline.content_split import cleanup_orphan_indexes
from pipeline.content_split import state as content_state


def test_wants_cleanup_phrases():
    assert wants_cleanup_content_cache("清理分类缓存")
    assert wants_cleanup_content_cache("清理一下内容分类索引")
    assert wants_confirm_cleanup_content_cache("确认清理分类缓存")
    assert not wants_confirm_cleanup_content_cache("清理分类缓存")
    assert not wants_cleanup_content_cache("内容识别后分类进各自新文档")


def test_missing_root_fresh_is_deferred(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FILEMATE_CONTENT_STATE_DIR", str(tmp_path / "idx"))
    live_root = tmp_path / "live" / "内容分类"
    live_root.mkdir(parents=True)
    uid_live = content_state.dir_id(live_root)
    content_state.save_index(live_root, {"classifier_version": 1, "files": {}})

    orphan = content_state.state_root() / ("a" * 32)
    orphan.mkdir(parents=True)
    (orphan / "index.json").write_text(
        json.dumps(
            {"root": str(tmp_path / "gone" / "内容分类"), "files": {}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    res = cleanup_orphan_indexes(dry_run=False)
    assert uid_live in (res.get("kept") or [])
    assert "a" * 32 in (res.get("deferred") or [])
    assert "a" * 32 not in (res.get("removed") or [])
    assert orphan.exists()


def test_missing_root_old_is_removed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FILEMATE_CONTENT_STATE_DIR", str(tmp_path / "idx"))
    orphan = content_state.state_root() / ("c" * 32)
    orphan.mkdir(parents=True)
    idx = orphan / "index.json"
    idx.write_text(
        json.dumps(
            {"root": str(tmp_path / "gone2" / "内容分类"), "files": {}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    old = time.time() - (content_state.ORPHAN_MISSING_ROOT_DAYS + 1) * 86400
    os.utime(idx, (old, old))
    res = cleanup_orphan_indexes(dry_run=False)
    assert "c" * 32 in (res.get("removed") or [])
    assert not orphan.exists()


def test_id_mismatch_removed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FILEMATE_CONTENT_STATE_DIR", str(tmp_path / "idx"))
    live_root = tmp_path / "live2" / "内容分类"
    live_root.mkdir(parents=True)
    uid_new = content_state.dir_id(live_root)
    content_state.save_index(live_root, {"classifier_version": 1, "files": {}})

    stale = content_state.state_root() / ("d" * 32)
    stale.mkdir(parents=True)
    (stale / "index.json").write_text(
        json.dumps({"root": str(live_root.resolve()), "files": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    res = cleanup_orphan_indexes(dry_run=False)
    assert uid_new in (res.get("kept") or [])
    assert "d" * 32 in (res.get("removed") or [])
    assert not stale.exists()


def test_dispatch_preview_then_confirm(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FILEMATE_CONTENT_STATE_DIR", str(tmp_path / "idx"))
    live_root = tmp_path / "x" / "内容分类"
    live_root.mkdir(parents=True)
    content_state.dir_id(live_root)
    # id 不匹配 → 可删
    orphan = content_state.state_root() / ("e" * 32)
    orphan.mkdir(parents=True)
    (orphan / "index.json").write_text(
        json.dumps({"root": str(live_root.resolve()), "files": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    preview = IntentDispatcher(None).dispatch("清理分类缓存", tmp_path)
    assert preview.get("reason") == "cache_clean_preview"
    assert orphan.exists()
    done = IntentDispatcher(None).dispatch("确认清理分类缓存", tmp_path)
    assert done.get("reason") == "cache_cleaned"
    assert not orphan.exists()
