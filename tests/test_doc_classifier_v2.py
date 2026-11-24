# -*- coding: utf-8 -*-
"""分类器 v2：新类目、文件名加权、阻尼、版本失效。"""
from __future__ import annotations

import json
from pathlib import Path

from docx import Document

from pipeline.content_split.batch_new_docs import run_batch_content_new_docs
from pipeline.content_split.common import ALL_WORD_EXTS, LEGACY_WORD_EXTS, WORD_EXTS, list_docx
from pipeline.content_split.legacy_word import (
    _classify_com_error,
    extract_doc_text,
    word_com_available,
)


def _docx_text(path: Path) -> str:
    d = Document(str(path))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(parts)
from pipeline.stage2_classifier.doc_classifier import (
    CLASSIFIER_VERSION,
    RULES_DIGEST,
    TYPE_LABELS,
    classify_decision,
    compute_rules_digest,
)


def test_type_labels_include_report_script():
    assert TYPE_LABELS["report"] == "报告"
    assert TYPE_LABELS["script"] == "剧本"
    assert CLASSIFIER_VERSION >= 3


def test_rules_digest_locked_to_version():
    """改 FEATURES/置信/强规则后必须同步更新 RULES_DIGEST，并建议 VERSION+=1。"""
    got = compute_rules_digest()
    assert got == RULES_DIGEST, (
        f"规则面已变（新 digest={got}，锁定={RULES_DIGEST}）。"
        "请：1) CLASSIFIER_VERSION += 1；2) RULES_DIGEST = 新 digest。"
    )


def test_quoted_technical_not_novel():
    """引号多的技术说明不应因引号判成小说。"""
    text = (
        "本规格说明模块调用与接口字段。配置依赖环境变量。"
        "「请求」与「响应」日志写入服务器缓存。"
        "“异常”处理与版本机制见技术方案设计说明。"
        "测试用例覆盖表结构与数据库部署。"
    ) * 2
    d = classify_decision(text, filename="规格说明.docx")
    assert d["doc_type"] == "technical", d
    assert d["low_confidence"] is False


def test_lab_report_to_report():
    text = (
        "实验报告 课程名称：软件工程。学号：2025。姓名：张三。学院：计科。专业：软工。\n"
        "一、实验目的\n验证微信小程序登录流程。\n"
        "二、实验环境\nWindows + 微信开发者工具。\n"
        "三、实验步骤\n1. 打开项目。2. 编译运行。\n"
        "四、结果分析\n功能正常。\n"
        "五、实验总结\n掌握小程序基础。指导教师：李四。\n"
    )
    d = classify_decision(text, filename="实验报告-微信小程序.docx")
    assert d["doc_type"] == "report", d


def test_lab_report_beats_technical_density():
    """真实报告里实现/模块/接口很多，结构强规则仍应强制 report。"""
    text = (
        "实验报告\n学号：1\n指导教师：王\n"
        "实验目的：实现模块接口与调用机制。\n"
        "实验步骤：配置依赖、编译运行、写测试用例。\n"
        "实验环境：服务器与数据库。\n"
        "结果分析：请求响应日志正常。\n"
        "实验总结：掌握架构与版本管理。\n"
    ) + ("实现模块接口调用参数请求响应日志版本机制架构配置部署。" * 20)
    d = classify_decision(text, filename="实验报告-软件项目管理.docx")
    assert d["doc_type"] == "report", d
    assert d.get("forced_report") is True
    # 得分保持真实：技术分可以高于报告分，不伪造险胜
    assert d["scores"]["technical"] >= d["scores"]["report"]
    assert d["top1"] == "technical"


def test_script_type():
    text = (
        "第一幕 咖啡馆。场景：白天。旁白：故事开始。\n"
        "角色：主角、反派。台词：你终于来了。\n"
        "分镜 1：特写。剧情推进到第二幕。\n"
    ) * 3
    d = classify_decision(text, filename="剧本.docx")
    assert d["doc_type"] == "script", d


def test_contract_keywords():
    text = (
        "本合同由甲方与乙方签署。第一条 违约责任。第二条 付款与发票。"
        "第三条 争议解决提交仲裁。双方签署盖章后生效。"
        "附件清单为本合同组成部分，送达地址以本合同载明为准。"
        "未尽事宜由甲乙双方另行协商书面确认。"
    )
    d = classify_decision(text, filename="服务合同.docx")
    assert d["doc_type"] == "contract", d


def test_filename_boost_report():
    # 正文弱特征，靠文件名拉高 report
    weak = "本周完成情况如下。姓名已填。学院已填。" + ("说明一点。" * 40)
    d = classify_decision(weak, filename="课程设计大作业.docx")
    assert d["scores"]["report"] >= d["scores"]["technical"]


def test_short_doc_needs_larger_margin():
    short = "甲方乙方第一条违约。"  # 很短
    d = classify_decision(short, filename="x.docx")
    # 短文即使有合同词，margin 不够则 other
    assert d["char_count"] < 500
    if d["margin"] < 1.0:
        assert d["doc_type"] == "other" or d["low_confidence"] is True


def _write_docx(path: Path, text: str) -> None:
    doc = Document()
    doc.add_paragraph(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))


CONTRACT = (
    "本合同由甲方与乙方签署。第一条 违约责任。第二条 付款与发票。"
    "第三条 争议解决提交仲裁。双方签署盖章后生效。"
    "附件清单为本合同组成部分，送达地址以本合同载明为准。"
    "未尽事宜由甲乙双方另行协商书面确认。"
)


def test_version_mismatch_overwrite_no_suffix(tmp_path: Path):
    """已有产物时版本升级：同类覆盖，绝不产生 _1。"""
    _write_docx(tmp_path / "协议.docx", CONTRACT)
    out1 = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out1.get("ok") is True
    dest = tmp_path / "内容分类" / "合同协议" / "协议.docx"
    assert dest.is_file()
    before = list((tmp_path / "内容分类" / "合同协议").glob("协议*.docx"))
    assert len(before) == 1

    from pipeline.content_split import state as content_state

    idx = content_state.index_path(tmp_path / "内容分类")
    data = json.loads(idx.read_text(encoding="utf-8"))
    assert data.get("classifier_version") == CLASSIFIER_VERSION
    data["classifier_version"] = 1  # 假装旧版 → stale
    idx.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    out2 = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out2.get("ok") is True
    after = list((tmp_path / "内容分类" / "合同协议").glob("协议*.docx"))
    assert len(after) == 1, after
    assert not (tmp_path / "内容分类" / "合同协议" / "协议_1.docx").exists()
    row = next(
        r for r in out2.get("classifications") or []
        if r.get("status") in ("已分类", "已重算")
    )
    assert "stale" in (row.get("source") or "")


def test_version_mismatch_migrate_deletes_old(tmp_path: Path):
    """换类目：删索引登记的旧副本，新路径不走 _1。"""
    # 先放进「其他」假装旧错分
    wrong = tmp_path / "内容分类" / "其他"
    wrong.mkdir(parents=True)
    _write_docx(tmp_path / "协议.docx", CONTRACT)
    import hashlib
    import shutil
    digest = hashlib.sha256((tmp_path / "协议.docx").read_bytes()).hexdigest()
    shutil.copy2(tmp_path / "协议.docx", wrong / "协议.docx")
    from pipeline.content_split import state as content_state

    root_out = tmp_path / "内容分类"
    idx = content_state.index_path(root_out)
    idx.write_text(
        json.dumps({
            "classifier_version": 1,
            "root": str(root_out.resolve()),
            "files": {
                digest: {
                    "rel": "其他/协议.docx",
                    "label": "其他",
                    "doc_type": "other",
                    "top2": "-",
                    "stale": True,
                }
            },
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    out = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out.get("ok") is True
    assert (tmp_path / "内容分类" / "合同协议" / "协议.docx").is_file()
    assert not (wrong / "协议.docx").exists()
    assert not (tmp_path / "内容分类" / "合同协议" / "协议_1.docx").exists()


def test_hash_skip_fills_previous_label(tmp_path: Path):
    _write_docx(tmp_path / "协议.docx", CONTRACT)
    run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    out2 = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    row = next(
        r for r in out2.get("classifications") or []
        if r.get("source") == "hash_skip"
    )
    assert row.get("label") == "合同协议"
    assert row.get("doc_type") == "contract"
    assert row.get("top2") and row.get("top2") != "-"


def test_stale_migrate_unique_dest_when_name_collision(tmp_path: Path):
    """换类时目标夹已有异内容同名 → unique_dest，不静默覆盖。"""
    import hashlib
    import shutil

    _write_docx(tmp_path / "协议.docx", CONTRACT)
    other_text = CONTRACT + "本版另有补充条款若干，内容不同以保证哈希不同。"
    dest_dir = tmp_path / "内容分类" / "合同协议"
    dest_dir.mkdir(parents=True)
    _write_docx(dest_dir / "协议.docx", other_text)
    wrong = tmp_path / "内容分类" / "其他"
    wrong.mkdir(parents=True)
    shutil.copy2(tmp_path / "协议.docx", wrong / "协议.docx")
    digest = hashlib.sha256((tmp_path / "协议.docx").read_bytes()).hexdigest()
    from pipeline.content_split import state as content_state

    root_out = tmp_path / "内容分类"
    idx = content_state.index_path(root_out)
    idx.write_text(
        json.dumps({
            "classifier_version": 1,
            "root": str(root_out.resolve()),
            "files": {
                digest: {
                    "rel": "其他/协议.docx",
                    "label": "其他",
                    "doc_type": "other",
                    "top2": "-",
                    "stale": True,
                }
            },
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    out = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out.get("ok") is True
    # 原异内容文件仍在
    assert (dest_dir / "协议.docx").is_file()
    assert (dest_dir / "协议_1.docx").is_file()
    assert not (wrong / "协议.docx").exists()


def test_empty_type_dir_cleaned_after_migrate(tmp_path: Path):
    import hashlib
    import shutil

    _write_docx(tmp_path / "协议.docx", CONTRACT)
    wrong = tmp_path / "内容分类" / "小说叙事"
    wrong.mkdir(parents=True)
    shutil.copy2(tmp_path / "协议.docx", wrong / "协议.docx")
    digest = hashlib.sha256((tmp_path / "协议.docx").read_bytes()).hexdigest()
    from pipeline.content_split import state as content_state

    root_out = tmp_path / "内容分类"
    content_state.index_path(root_out).write_text(
        json.dumps({
            "classifier_version": 1,
            "root": str(root_out.resolve()),
            "files": {
                digest: {
                    "rel": "小说叙事/协议.docx",
                    "label": "小说叙事",
                    "doc_type": "novel",
                    "top2": "-",
                    "stale": True,
                }
            },
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    out = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out.get("ok") is True
    assert not wrong.exists()
    assert "小说叙事" in (out.get("empty_dirs_removed") or [])


def test_force_report_manifest_annotation(tmp_path: Path):
    text = (
        "实验报告\n学号：1\n指导教师：王\n"
        "实验目的：验证。\n实验步骤：操作。\n实验环境：Win。\n"
        "结果分析：通过。\n实验总结：完成。\n"
    ) + ("实现模块接口调用参数请求响应日志版本机制架构。" * 15)
    _write_docx(tmp_path / "实验报告-样例.docx", text)
    out = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    row = next(r for r in out["classifications"] if r.get("status") in ("已分类", "已重算"))
    assert "报告" in (row.get("label") or "")
    assert "结构强规则" in (row.get("label") or "")
    assert (row.get("top2") or "").startswith("〔结构强规则→报告〕")
    assert "force_report" in (row.get("source") or "")
    # 目录名仍是纯「报告」
    assert (tmp_path / "内容分类" / "报告").is_dir()


def test_com_error_reasons_granular():
    assert _classify_com_error(RuntimeError("password required"), stage="open") == "encrypted"
    assert _classify_com_error(RuntimeError("Server execution failed"), stage="dispatch") == "com_unavailable"
    assert _classify_com_error(RuntimeError("blocked by policy"), stage="open") == "open_failed"
    assert _classify_com_error(RuntimeError("disk full"), stage="save") == "corrupt"


def test_automation_security_set_on_open(tmp_path: Path, monkeypatch):
    """打开 .doc 前必须 ForceDisable 自动化宏。"""
    import sys
    import types

    flags = {"sec": None}

    class _FakeDoc:
        def SaveAs2(self, *a, **k):
            raise RuntimeError("stop-after-security")

        def Close(self, *a, **k):
            pass

    class _FakeDocs:
        @staticmethod
        def Open(*a, **k):
            return _FakeDoc()

    class _FakeApp:
        def __init__(self):
            self.Documents = _FakeDocs()

        def __setattr__(self, name, value):
            if name == "AutomationSecurity":
                flags["sec"] = value
            object.__setattr__(self, name, value)

        def Quit(self):
            pass

    fake_mod = types.ModuleType("win32com")
    fake_client = types.ModuleType("win32com.client")
    fake_client.DispatchEx = lambda name: _FakeApp()
    monkeypatch.setitem(sys.modules, "win32com", fake_mod)
    monkeypatch.setitem(sys.modules, "win32com.client", fake_client)
    monkeypatch.setattr(
        "pipeline.content_split.legacy_word.word_com_available",
        lambda: True,
    )

    p = tmp_path / "x.doc"
    p.write_bytes(b"fake")
    r = extract_doc_text(p)
    assert flags["sec"] == 3
    assert r.get("ok") is False
    assert r.get("reason") == "corrupt"


def test_list_word_exts(tmp_path: Path):
    (tmp_path / "a.docx").write_bytes(b"x")
    (tmp_path / "b.docm").write_bytes(b"x")
    (tmp_path / "c.doc").write_bytes(b"x")
    (tmp_path / "d.pdf").write_bytes(b"x")
    (tmp_path / "e.txt").write_bytes(b"x")
    found = {p.suffix.lower() for p in list_docx(tmp_path)}
    assert found == {".docx", ".docm", ".doc"}
    assert ".pdf" not in found
    assert WORD_EXTS | LEGACY_WORD_EXTS == ALL_WORD_EXTS


def test_legacy_doc_skip_without_converter(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "pipeline.content_split.batch_new_docs.word_com_available",
        lambda: False,
    )
    monkeypatch.setattr(
        "pipeline.content_split.legacy_word.word_com_available",
        lambda: False,
    )
    (tmp_path / "老合同.doc").write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 100)
    out = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out.get("ok") is True
    reasons = [r.get("skip_reason") for r in out.get("classifications") or []]
    assert "legacy_doc_no_converter" in reasons
    assert "legacy_doc_no_converter" in (out.get("message") or "") or out.get("legacy_skipped", 0) >= 1
    manifest = _docx_text(tmp_path / "内容分类" / "分类清单.docx")
    assert "| 格式 |" in manifest
    assert "doc" in manifest


def test_legacy_doc_mock_converter(tmp_path: Path, monkeypatch):
    contract = (
        "本合同由甲方与乙方签署。第一条 违约责任。第二条 付款与发票。"
        "第三条 争议解决提交仲裁。双方签署盖章后生效。"
        "附件清单为本合同组成部分，送达地址以本合同载明为准。"
        "未尽事宜由甲乙双方另行协商书面确认。"
    )

    def _fake(src: Path):
        return {
            "ok": True,
            "text": contract,
            "reason": "",
            "converter": "word_com",
        }

    monkeypatch.setattr(
        "pipeline.content_split.batch_new_docs.extract_doc_text",
        _fake,
    )
    (tmp_path / "劳务合同.doc").write_bytes(b"fake-doc-bytes")
    out = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out.get("ok") is True, out
    assert out.get("reason") == "ok"
    row = next(r for r in out["classifications"] if r.get("status") == "已分类")
    assert row.get("format") == "doc"
    assert row.get("label") == "合同协议"
    assert (tmp_path / "内容分类" / "合同协议" / "劳务合同.doc").is_file()


def test_extract_doc_no_pywin32_no_raise(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "pipeline.content_split.legacy_word.word_com_available",
        lambda: False,
    )
    p = tmp_path / "nope.doc"
    p.write_bytes(b"x")
    r = extract_doc_text(p)
    assert r["ok"] is False
    assert r["reason"] == "legacy_doc_no_converter"
