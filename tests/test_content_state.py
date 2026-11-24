# -*- coding: utf-8 -*-
"""内容分类状态：UUID / storage 索引 / 迁移 / 改名·复制。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from docx import Document

from pipeline.content_split import run_batch_content_new_docs
from pipeline.content_split import state as content_state


CONTRACT = (
    "本合同由甲方与乙方签署。第一条 违约责任。第二条 付款与发票。"
    "第三条 争议解决提交仲裁。双方签署盖章后生效。"
    "附件清单为本合同组成部分，送达地址以本合同载明为准。"
    "未尽事宜由甲乙双方另行协商书面确认。"
)


def _write_docx(path: Path, text: str) -> None:
    doc = Document()
    doc.add_paragraph(text)
    doc.save(str(path))


def test_first_run_only_id_and_manifest_in_out(tmp_path: Path):
    _write_docx(tmp_path / "协议.docx", CONTRACT)
    out = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out.get("ok") is True, out
    root = tmp_path / "内容分类"
    assert (root / ".filemate" / "id").is_file()
    assert (root / "分类清单.docx").is_file()
    assert not (root / "_content_hash_index.json").exists()
    assert not (root / ".filemate" / "hash_index.json").exists()
    assert not (root / "_label_cache.json").exists()
    uid = content_state.dir_id(root)
    assert content_state.index_path(root).is_file()
    assert uid in str(content_state.index_path(root))
    assert content_state.index_path(root).parent.parent == content_state.state_root()


def test_rename_keeps_uuid_and_hits_index(tmp_path: Path):
    work = tmp_path / "A"
    work.mkdir()
    _write_docx(work / "协议.docx", CONTRACT)
    out1 = run_batch_content_new_docs("内容识别后分类进各自新文档", work)
    assert out1.get("ok") is True, out1
    root_a = work / "内容分类"
    uid = content_state.dir_id(root_a)
    work2 = tmp_path / "B"
    work.rename(work2)
    root_b = work2 / "内容分类"
    assert content_state.dir_id(root_b) == uid
    out2 = run_batch_content_new_docs("内容识别后分类进各自新文档", work2)
    assert out2.get("reason") == "all_skipped"
    copies = list((root_b / "合同协议").glob("协议*.docx"))
    assert len(copies) == 1


def test_copy_gets_new_uuid(tmp_path: Path):
    work = tmp_path / "src"
    work.mkdir()
    _write_docx(work / "协议.docx", CONTRACT)
    run_batch_content_new_docs("内容识别后分类进各自新文档", work)
    root = work / "内容分类"
    uid1 = content_state.dir_id(root)
    dup = tmp_path / "dup"
    shutil.copytree(work, dup)
    root2 = dup / "内容分类"
    # 同 UUID 文件仍在；load 时发现旧 root 仍存活 → rotate
    data = content_state.load_index(root2)
    assert data is None
    uid2 = content_state.dir_id(root2)
    assert uid2 != uid1
    out = run_batch_content_new_docs("内容识别后分类进各自新文档", dup)
    assert out.get("ok") is True
    # 复制目录已有产物 → 指纹命中跳过，不产生 _1
    copies = list((root2 / "合同协议").glob("协议*.docx"))
    assert len(copies) == 1


def test_delete_id_rebuilds_no_dup(tmp_path: Path):
    _write_docx(tmp_path / "协议.docx", CONTRACT)
    run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    root = tmp_path / "内容分类"
    (root / ".filemate" / "id").unlink()
    out2 = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out2.get("reason") == "all_skipped"
    assert len(list((root / "合同协议").glob("协议*.docx"))) == 1


def test_delete_storage_rebuilds_no_dup(tmp_path: Path):
    _write_docx(tmp_path / "协议.docx", CONTRACT)
    run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    root = tmp_path / "内容分类"
    idx = content_state.index_path(root)
    shutil.rmtree(idx.parent)
    out2 = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out2.get("reason") == "all_skipped"
    assert len(list((root / "合同协议").glob("协议*.docx"))) == 1
    assert content_state.index_path(root).is_file()


def test_migrate_legacy_files(tmp_path: Path):
    root = tmp_path / "内容分类"
    root.mkdir()
    (root / "_content_hash_index.json").write_text(
        json.dumps({"classifier_version": 1, "files": {"abc": "其他/a.docx"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "_label_cache.json").write_text(
        json.dumps({"classifier_version": 1, "entries": {"abc": {}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "_分类清单.md").write_text(
        "# 内容分类清单\n本轮：已分类 1\n得分 top2\n",
        encoding="utf-8",
    )
    (root / "_分类清单_用户笔记.md").write_text("# 我的笔记", encoding="utf-8")
    fm = root / ".filemate"
    fm.mkdir()
    (fm / "label_cache.json").write_text(
        json.dumps({"classifier_version": 1, "entries": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    p = content_state.index_path(root)
    assert p.is_file()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "files" in data or "abc" in data
    assert not (root / "_content_hash_index.json").exists()
    c = content_state.cache_path(root, "label_cache.json")
    assert c.is_file()
    # .filemate/label_cache 优先迁入；根目录旧缓存被删
    assert not (root / "_label_cache.json").exists()
    assert not (fm / "label_cache.json").exists()
    assert not (root / "_分类清单.md").exists()
    assert (root / "_分类清单_用户笔记.md").is_file()
