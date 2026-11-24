# -*- coding: utf-8 -*-
"""工作区内文件名：唯一命中才锁；多命中不猜。"""
from __future__ import annotations

from pathlib import Path

from ui.workspace_resolve import (
    extract_filename_tokens,
    format_ambiguous_message,
    resolve_names_in_workspace,
)


def test_extract_tokens_quoted_and_bare():
    text = "把「合同.docx」和 表.xlsx 联查，忽略合同两个字"
    toks = extract_filename_tokens(text)
    assert "合同.docx" in toks
    assert "表.xlsx" in toks
    assert "合同" not in toks  # 无后缀不抽


def test_unique_hit(tmp_path):
    f = tmp_path / "合同.docx"
    f.write_bytes(b"PK")
    r = resolve_names_in_workspace(tmp_path, "看看 合同.docx 的批注")
    assert r.hits == [str(f.resolve())]
    assert not r.ambiguous
    assert not r.missing


def test_ambiguous_same_name(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    fa = a / "合同.docx"
    fb = b / "合同.docx"
    fa.write_bytes(b"PK")
    fb.write_bytes(b"PK")
    r = resolve_names_in_workspace(tmp_path, "打开 合同.docx")
    assert not r.hits
    assert "合同.docx" in r.ambiguous
    assert len(r.ambiguous["合同.docx"]) == 2
    msg = format_ambiguous_message(r)
    assert "不唯一" in msg
    assert "不会猜" in msg


def test_relative_path_unique(tmp_path):
    sub = tmp_path / "标书"
    sub.mkdir()
    f = sub / "技术偏离表.xlsx"
    f.write_bytes(b"PK")
    r = resolve_names_in_workspace(tmp_path, "查 标书/技术偏离表.xlsx")
    assert r.hits == [str(f.resolve())]


def test_outside_workspace_not_used(tmp_path):
    ws = tmp_path / "ws"
    other = tmp_path / "other"
    ws.mkdir()
    other.mkdir()
    (other / "秘.pdf").write_bytes(b"%PDF")
    r = resolve_names_in_workspace(ws, "打开 秘.pdf")
    assert not r.hits
    assert "秘.pdf" in r.missing
