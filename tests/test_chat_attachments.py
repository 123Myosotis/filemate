# -*- coding: utf-8 -*-
"""对话附件合并：附件优先，不猜意图。"""
from __future__ import annotations

from pathlib import Path

from ui.chat_attachments import (
    format_attach_note, merge_file_paths, paths_from_strings,
)


def test_paths_from_strings_files_only(tmp_path):
    f = tmp_path / "a.docx"
    f.write_bytes(b"PK")
    d = tmp_path / "sub"
    d.mkdir()
    out = paths_from_strings([str(f), str(d), str(tmp_path / "missing.txt")])
    assert out == [str(f.resolve())]


def test_merge_attachments_first(tmp_path):
    a = tmp_path / "a.xlsx"
    b = tmp_path / "b.xlsx"
    a.write_bytes(b"PK")
    b.write_bytes(b"PK")
    merged = merge_file_paths([str(a)], [str(b), str(a)])
    assert merged[0] == str(a.resolve())
    assert str(b.resolve()) in merged
    assert len(merged) == 2


def test_format_attach_note(tmp_path):
    f = tmp_path / "合同.docx"
    f.write_bytes(b"PK")
    note = format_attach_note([str(f)])
    assert "合同.docx" in note
    assert note.startswith("📎")
