# -*- coding: utf-8 -*-
"""阶段 5：任务链（线性 + 失败即停）。"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from docx import Document

from pipeline.word_office.chain import run_chain

BASE = Path(__file__).resolve().parents[1] / "构建" / "smoke" / "_chain_test"


@pytest.fixture()
def docx():
    shutil.rmtree(BASE, ignore_errors=True)
    BASE.mkdir(parents=True, exist_ok=True)
    p = BASE / "t.docx"
    d = Document()
    d.add_heading("标题", level=1)
    d.add_paragraph("甲方是A型机。")
    d.save(str(p))
    yield p
    shutil.rmtree(BASE, ignore_errors=True)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    import config
    monkeypatch.setattr(config, "get_api_key", lambda: "k")


def test_chain_empty():
    assert run_chain([])["status"] == "failed"


def test_chain_single(docx):
    r = run_chain([{"user_input": "检查结构", "selected_files": [str(docx)]}])
    assert r["status"] == "done"


def test_chain_two(docx):
    r = run_chain([
        {"user_input": "检查结构", "selected_files": [str(docx)]},
        {"user_input": "把 A型机 替换成 C型机", "selected_files": [str(docx)]},
    ])
    assert r["status"] == "done"
    assert len(r["results"]) == 2
    assert all(x["status"] == "done" for x in r["results"])


def test_chain_stops_on_failure(docx):
    r = run_chain([
        {"user_input": "把 不存在 替换成 X", "selected_files": [str(docx)]},
        {"user_input": "检查结构", "selected_files": [str(docx)]},
    ])
    assert r["status"] == "failed"
    assert r["reason"] == "chain_stopped"
    assert len(r["results"]) == 1
