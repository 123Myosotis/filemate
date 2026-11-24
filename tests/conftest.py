# -*- coding: utf-8 -*-
"""全局测试夹具：隔离内容标签库与内容分类 storage，不写真实库。"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_content_tags_db(tmp_path_factory, monkeypatch):
    """每个测试用例使用独立 SQLite，不写真实标签库。"""
    db = tmp_path_factory.mktemp("content_tags") / "tags_test.db"
    monkeypatch.setattr("infra.content_tags.DB_PATH", db)
    yield


@pytest.fixture(autouse=True)
def _isolate_content_state_dir(tmp_path_factory, monkeypatch):
    """内容分类索引/缓存进临时目录，不写项目 storage/content_index。"""
    root = tmp_path_factory.mktemp("content_index")
    monkeypatch.setenv("FILEMATE_CONTENT_STATE_DIR", str(root))
    yield
