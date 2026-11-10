# -*- coding: utf-8 -*-
"""阶段 6：E 分块 + 多文件批处理 + 报告复用。"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from docx import Document

from pipeline.word_office.batch import run_batch
from pipeline.word_office.chunk import split_by_heading

BASE = Path(__file__).resolve().parents[1] / "构建" / "smoke" / "_e6_test"


@pytest.fixture()
def docx():
    shutil.rmtree(BASE, ignore_errors=True)
    BASE.mkdir(parents=True, exist_ok=True)
    p = BASE / "t.docx"
    d = Document()
    d.add_heading("第一章", level=1)
    d.add_paragraph("甲。")
    d.add_heading("第二章", level=1)
    d.add_paragraph("乙。")
    d.save(str(p))
    yield p
    shutil.rmtree(BASE, ignore_errors=True)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    import config
    monkeypatch.setattr(config, "get_api_key", lambda: "k")


def test_chunk_by_heading(docx):
    chunks = split_by_heading(docx)
    assert len(chunks) >= 2
    assert chunks[0]["title"] == "第一章"


def test_batch_empty():
    assert run_batch([], "检查结构")["status"] == "failed"


def test_batch_two(docx):
    r = run_batch([str(docx), str(docx)], "检查结构")
    assert r["status"] == "done"
    assert len(r["results"]) == 2


def test_batch_partial(docx):
    r = run_batch([str(docx), "不存在.docx"], "检查结构")
    assert r["status"] == "partially_completed"
    assert len(r["results"]) == 2
