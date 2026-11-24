# -*- coding: utf-8 -*-
"""批量内容识别 → 按类另存新文档（指纹去重 / 门槛 / 清单）。"""
from __future__ import annotations

from pathlib import Path

import config
from docx import Document

from agent_core.local_file_intent import wants_batch_content_new_docs
from engine.dispatcher import IntentDispatcher
from pipeline.content_split import run_batch_content_new_docs
from pipeline.stage2_classifier.doc_classifier import (
    DEFAULT_TYPE,
    classify,
    classify_decision,
)
from ui.round_undo import apply_file_undos, collect_from_result


def _docx_text(path: Path) -> str:
    d = Document(str(path))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(parts)

CONTRACT = (
    "本合同由甲方与乙方签署。第一条 违约责任。第二条 付款与发票。"
    "第三条 争议解决提交仲裁。双方签署盖章后生效。"
    "附件清单为本合同组成部分，送达地址以本合同载明为准。"
    "未尽事宜由甲乙双方另行协商书面确认。"
)
GUIDE = (
    "本教程指南面向新手。第一步 打开软件。第二步 点击设置。"
    "注意：勾选选项后输入账号。然后保存并退出。最后核验结果。"
    "手册附录列出常见问题与处理步骤，请按顺序完成每一节练习。"
)
NOISE = (
    "今天天气不错，我们去公园散步，买了两杯咖啡，聊了些近况与旅行计划，"
    "傍晚又看了一场电影，回家后简单写了这篇日记当作备忘留存，"
    "明天打算早点出门把杂事处理完再回来休息。"
)


def _write_docx(path: Path, text: str) -> None:
    doc = Document()
    for line in text.strip().splitlines() or [text]:
        doc.add_paragraph(line)
    doc.save(str(path))


def test_wants_batch_phrase():
    assert wants_batch_content_new_docs(
        "帮我把现在桌面上的word文档内容识别后，分类整理进各自新文档里，只动word文档。"
    )


def test_default_type_is_other():
    assert DEFAULT_TYPE == "other"
    t, scores = classify("啊")
    assert t == "other"
    assert max(scores.values()) <= 0


def test_run_classifies_and_copies(tmp_path: Path):
    _write_docx(tmp_path / "协议.docx", CONTRACT)
    _write_docx(tmp_path / "教程.docx", GUIDE)
    (tmp_path / "忽略.lnk").write_bytes(b"x")
    (tmp_path / "~$锁.docx").write_bytes(b"y")

    out = run_batch_content_new_docs(
        "把这些word文档内容识别后分类整理进各自新文档里，只动word文档",
        tmp_path,
    )
    assert out.get("ok") is True, out
    assert out.get("reason") == "ok"
    assert (tmp_path / "协议.docx").is_file()
    assert (tmp_path / "教程.docx").is_file()
    assert (tmp_path / "忽略.lnk").is_file()
    assert (tmp_path / "内容分类" / "合同协议" / "协议.docx").is_file()
    assert (tmp_path / "内容分类" / "指南教程" / "教程.docx").is_file()
    manifest = _docx_text(tmp_path / "内容分类" / "分类清单.docx")
    assert "得分 top2" in manifest
    assert "状态" in manifest
    labels = {
        r["label"] for r in out["classifications"] if r.get("status") == "已分类"
    }
    assert "合同协议" in labels
    assert "指南教程" in labels


def test_hash_dedup_skips_second_run(tmp_path: Path):
    _write_docx(tmp_path / "协议.docx", CONTRACT)
    out1 = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out1.get("ok") is True, out1
    out2 = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    copies = list((tmp_path / "内容分类" / "合同协议").glob("协议*.docx"))
    assert len(copies) == 1, copies
    assert out2.get("ok") is True
    assert out2.get("reason") == "all_skipped"
    assert any(
        r.get("skip_reason", "").startswith("内容相同")
        for r in out2.get("classifications") or []
    )


def test_stale_index_after_undo_rewrites(tmp_path: Path):
    """撤回删产物后，不得因陈旧 JSON 索引永久跳过。"""
    import hashlib
    import json

    _write_docx(tmp_path / "协议.docx", CONTRACT)
    out1 = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out1.get("ok") is True, out1
    copy = tmp_path / "内容分类" / "合同协议" / "协议.docx"
    assert copy.is_file()
    digest = hashlib.sha256((tmp_path / "协议.docx").read_bytes()).hexdigest()
    copy.unlink()
    # 故意写入「文件已不存在」的陈旧索引（旧 bug 会合并回来）
    from pipeline.content_split import state as content_state

    root_out = tmp_path / "内容分类"
    content_state.save_index(root_out, {digest: "合同协议/协议.docx"})
    out2 = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out2.get("ok") is True, out2
    assert out2.get("reason") == "ok"
    assert copy.is_file(), "撤回后应重新写入"


def test_short_text_skipped(tmp_path: Path):
    _write_docx(tmp_path / "短.docx", "甲方乙方第一条。")  # << 80
    out = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out.get("ok") is True
    assert out.get("reason") == "all_skipped"
    assert any(
        "不足" in (r.get("skip_reason") or "")
        for r in out.get("classifications") or []
    )


def test_classify_low_confidence_is_other():
    """低置信时 doc_type 必须是 other，top1 仅展示。"""
    # 构造分差很小的文本较难；用无特征噪声即可
    d = classify_decision(NOISE)
    assert d["low_confidence"] is True
    assert d["doc_type"] == "other"
    t, _ = classify(NOISE)
    assert t == "other"


def test_zero_byte_skipped(tmp_path: Path):
    (tmp_path / "空.docx").write_bytes(b"")
    out = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out.get("reason") == "all_skipped"
    assert any(
        "0 字节" in (r.get("skip_reason") or "")
        for r in out.get("classifications") or []
    )


def test_soft_label_mock_and_source_column(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "get_api_key", lambda: "sk-test")

    def _fake_soft(text, content_hash, *, api_key="", cache=None):
        if cache is not None:
            cache[content_hash] = {"doc_type": "guide", "confidence": 0.9}
        return {"doc_type": "guide", "confidence": 0.9, "source": "model"}

    monkeypatch.setattr(
        "pipeline.content_split.batch_new_docs.soft_label_from_model",
        _fake_soft,
    )
    _write_docx(tmp_path / "流水.docx", NOISE)
    out = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out.get("ok") is True, out
    assert (tmp_path / "内容分类" / "指南教程" / "流水.docx").is_file()
    row = next(r for r in out["classifications"] if r.get("status") == "已分类")
    assert row.get("source", "").startswith("soft:")
    manifest = _docx_text(tmp_path / "内容分类" / "分类清单.docx")
    assert "| 来源 |" in manifest or "来源" in manifest.split("\n")[0:20][-1] or "来源" in manifest


def test_same_name_different_content_gets_suffix(tmp_path: Path):
    """同名不同内容：允许 _1，清单标同名另存。"""
    _write_docx(tmp_path / "协议.docx", CONTRACT)
    run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    # 换内容但同名源 → 先挪走源再放新内容同名，或直接往类目录已有时再分类另一个文件
    other = (
        "本合同甲方乙方。第一条 违约。第二条 付款。第三条 仲裁。"
        "签署盖章生效。附件为组成部分。另有送达与保密条款若干。"
        "未尽事宜协商书面确认，与前一版正文不同以保证哈希不同。"
    )
    _write_docx(tmp_path / "协议B.docx", other)
    # 复制到同名再跑：把协议B 的内容写成「已分类目录外」同名冲突——
    # 更直接：第二次写入同名文件到合同协议（先改源名为冲突）
    dest_dir = tmp_path / "内容分类" / "合同协议"
    # 源仍叫 协议2，类目录里已有 协议.docx；再放一个也叫协议的源
    (tmp_path / "协议.docx").unlink()
    _write_docx(tmp_path / "协议.docx", other)
    out2 = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out2.get("ok") is True, out2
    names = sorted(p.name for p in dest_dir.glob("协议*.docx"))
    assert "协议.docx" in names
    assert any(n.startswith("协议_") for n in names) or "协议_1.docx" in names
    assert any(
        "同名另存" in (r.get("source") or "")
        for r in out2.get("classifications") or []
        if r.get("status") == "已分类"
    )


def test_low_conf_without_key_goes_other(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    _write_docx(tmp_path / "流水.docx", NOISE)
    # 确认规则侧低置信
    d = classify_decision(NOISE)
    assert d["low_confidence"] is True
    out = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out.get("ok") is True, out
    assert (tmp_path / "内容分类" / "其他" / "流水.docx").is_file()


def test_one_level_subdir(tmp_path: Path):
    sub = tmp_path / "材料"
    sub.mkdir()
    _write_docx(sub / "子协议.docx", CONTRACT)
    out = run_batch_content_new_docs(
        "内容识别后分类进各自新文档，含子目录",
        tmp_path,
    )
    assert out.get("ok") is True, out
    assert (tmp_path / "内容分类" / "合同协议" / "子协议.docx").is_file()


def test_dispatch_no_key_runs_split(tmp_path: Path, monkeypatch):
    _write_docx(tmp_path / "合同A.docx", CONTRACT)
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    u = "帮我把word文档内容识别后，分类整理进各自新文档里，只动word文档。"
    out = IntentDispatcher(None).dispatch(u, tmp_path)
    assert "选中一个" not in (out.get("message") or "")
    assert out.get("tool") == "content_split"
    assert out.get("reason") == "ok"
    assert (tmp_path / "内容分类" / "合同协议" / "合同A.docx").is_file()
    rec = collect_from_result(out)
    assert rec.get("extra_files")
    oks, errs = apply_file_undos(rec)
    assert not errs, errs
    assert not (tmp_path / "内容分类" / "合同协议" / "合同A.docx").exists()
    assert (tmp_path / "合同A.docx").is_file()


def test_dispatch_wrong_word_office_route_still_splits(tmp_path: Path, monkeypatch):
    _write_docx(
        tmp_path / "a.docx",
        "参考文献 [1] 摘要 引言 本研究 et al 实验样本对照。"
        "本文讨论了样本设计与对照实验，并列出参考文献与绪论要点。",
    )
    monkeypatch.setattr(config, "get_api_key", lambda: "sk-test")
    monkeypatch.setattr(
        "agent_core.intent_router.route",
        lambda *a, **k: {
            "tool": "word_office", "mode": "execute", "subdir": "",
            "actions": [], "plan": "should-not-run",
        },
    )
    out = IntentDispatcher(None).dispatch(
        "word文档内容识别后分类整理进各自新文档里",
        tmp_path,
    )
    assert out.get("tool") == "content_split"
    assert "选中一个" not in (out.get("message") or "")
    assert (tmp_path / "内容分类").is_dir()


def test_desktop_phrase_uses_resolved_dir(tmp_path: Path, monkeypatch):
    from pipeline.content_split import batch_new_docs as mod

    desk = tmp_path / "FakeDesktop"
    desk.mkdir()
    _write_docx(desk / "协议.docx", CONTRACT)
    monkeypatch.setattr(mod, "resolve_work_dir", lambda _u, _fb: desk)
    # batch imports resolve from common — patch common used by module
    monkeypatch.setattr(
        "pipeline.content_split.batch_new_docs.resolve_work_dir",
        lambda _u, _fb: desk,
    )
    out = run_batch_content_new_docs(
        "把桌面上的word文档内容识别后分类进各自新文档里",
        tmp_path / "channel",
    )
    assert out.get("ok") is True, out
    assert (desk / "内容分类" / "合同协议" / "协议.docx").is_file()
