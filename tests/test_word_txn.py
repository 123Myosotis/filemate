# -*- coding: utf-8 -*-
"""P3：Word 写事务——真 dry-run、失败整包还原、链逆序补偿。"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from docx import Document

from pipeline.word_office import WordModule
from pipeline.word_office.chain import run_chain
from pipeline.word_office import txn

BASE = Path(__file__).resolve().parents[1] / "构建" / "smoke" / "_txn_test"


@pytest.fixture()
def docx():
    shutil.rmtree(BASE, ignore_errors=True)
    BASE.mkdir(parents=True, exist_ok=True)
    p = BASE / "t.docx"
    d = Document()
    d.add_heading("标题", level=1)
    d.add_paragraph("甲方是A型机。乙方是B型机。")
    d.save(str(p))
    yield p
    shutil.rmtree(BASE, ignore_errors=True)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    import config
    monkeypatch.setattr(config, "get_api_key", lambda: "k")


def test_txn_begin_writes_archive_and_bak(docx):
    snap = txn.begin(docx)
    assert snap.archive.is_file()
    assert docx.with_suffix(docx.suffix + ".bak").is_file()
    assert snap.archive.read_bytes() == docx.read_bytes()


def test_dry_run_does_not_touch_original(docx):
    before = docx.read_bytes()
    mtime = docx.stat().st_mtime
    r = WordModule.handle({
        "user_input": "把 A型机 替换成 C型机",
        "selected_files": [str(docx)],
        "dry_run": True,
    })
    assert r["status"] == "done"
    assert r.get("dry_run") is True
    assert (r.get("txn") or {}).get("phase") == "dry"
    assert docx.read_bytes() == before
    assert abs(docx.stat().st_mtime - mtime) < 1.0


def test_failed_write_restores_snapshot(docx):
    before = docx.read_bytes()
    r = WordModule.handle({
        "user_input": "把 不存在的字串XYZ 替换成 别的",
        "selected_files": [str(docx)],
    })
    assert r["status"] != "done"
    assert docx.read_bytes() == before


def test_commit_phase_on_success(docx):
    r = WordModule.handle({
        "user_input": "把 A型机 替换成 C型机",
        "selected_files": [str(docx)],
    })
    assert r["status"] == "done"
    assert (r.get("txn") or {}).get("phase") == "committed"
    assert (r.get("patch_set") or {}).get("backup")
    assert "C型机" in Document(str(docx)).paragraphs[1].text


def test_rollback_flag_restores(docx):
    before = docx.read_bytes()
    r = WordModule.handle({
        "user_input": "把 A型机 替换成 C型机",
        "selected_files": [str(docx)],
        "rollback": True,
    })
    assert r.get("rollback") is True
    assert docx.read_bytes() == before


def test_chain_compensates_previous_write(docx):
    before = docx.read_bytes()
    r = run_chain([
        {"user_input": "把 A型机 替换成 C型机", "selected_files": [str(docx)]},
        {"user_input": "把 根本没有的词 替换成 X", "selected_files": [str(docx)]},
    ])
    assert r["status"] == "failed"
    assert r["reason"] == "chain_stopped"
    assert (r.get("txn") or {}).get("phase") == "compensated"
    assert docx.read_bytes() == before
    assert "A型机" in Document(str(docx)).paragraphs[1].text
