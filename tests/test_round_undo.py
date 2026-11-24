# -*- coding: utf-8 -*-
"""本轮撤回：只认明确字段，机械还原。"""
from pathlib import Path

from ui.round_undo import (
    apply_file_undos, collect_from_result, empty_round, has_undoable, merge_rounds,
)


def test_collect_moves_and_word(tmp_path):
    src = tmp_path / "a.txt"
    dst = tmp_path / "b" / "a.txt"
    bak = tmp_path / "snap.docx"
    doc = tmp_path / "doc.docx"
    bak.write_text("old", encoding="utf-8")
    doc.write_text("new", encoding="utf-8")
    result = {
        "operations": [{"type": "move", "src": str(src), "dst": str(dst)}],
        "patch_set": {"path": str(doc), "backup": str(bak), "applied": 1},
        "export": {"path": str(tmp_path / "out.xlsx")},
        "report_paths": [str(tmp_path / "r.docx")],
    }
    rec = collect_from_result(result, log_ids=[9])
    assert rec["moves"][0]["src"] == str(src)
    assert rec["moves"][0]["log_id"] == 9
    assert rec["word_restores"][0]["backup"] == str(bak)
    assert str(tmp_path / "out.xlsx") in rec["extra_files"]
    assert has_undoable(rec)


def test_apply_word_and_move(tmp_path):
    doc = tmp_path / "x.docx"
    bak = tmp_path / "x.bak.docx"
    bak.write_bytes(b"BEFORE")
    doc.write_bytes(b"AFTER")
    src = tmp_path / "old" / "f.txt"
    dst = tmp_path / "new" / "f.txt"
    dst.parent.mkdir(parents=True)
    dst.write_text("moved", encoding="utf-8")
    rec = empty_round()
    rec["word_restores"] = [{"path": str(doc), "backup": str(bak)}]
    rec["moves"] = [{"src": str(src), "dst": str(dst)}]
    oks, errs = apply_file_undos(rec)
    assert not errs
    assert doc.read_bytes() == b"BEFORE"
    assert src.is_file()
    assert not dst.exists()
    assert oks


def test_merge_and_empty():
    a = empty_round()
    a["ai_msg_ids"] = [1]
    b = empty_round()
    b["ai_msg_ids"] = [2]
    b["moves"] = [{"src": "a", "dst": "b"}]
    m = merge_rounds(a, b)
    assert m["ai_msg_ids"] == [1, 2]
    assert len(m["moves"]) == 1
    assert not has_undoable(empty_round())
