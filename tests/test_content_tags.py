# -*- coding: utf-8 -*-
"""内容标签：建表/upsert/查找/纠正/回填；tags.enabled 关闭则 no-op。"""
from __future__ import annotations

from pathlib import Path

import config
from docx import Document

from agent_core.local_file_intent import (
    extract_tag_correction,
    extract_tag_query,
    wants_find_by_tag,
    wants_tag_backfill,
    wants_tag_correct,
)
from engine.dispatcher import IntentDispatcher
from infra import content_tags as ct
from pipeline.content_split.batch_new_docs import run_batch_content_new_docs
from pipeline.content_split.soft_label import soft_label_from_model

CONTRACT = (
    "本合同由甲方与乙方签署。第一条 违约责任。第二条 付款与发票。"
    "第三条 争议解决提交仲裁。双方签署盖章后生效。"
    "附件清单为本合同组成部分，送达地址以本合同载明为准。"
    "未尽事宜由甲乙双方另行协商书面确认。"
)


def _write_docx(path: Path, text: str) -> None:
    doc = Document()
    for line in text.strip().splitlines() or [text]:
        doc.add_paragraph(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))


def _docx_text(path: Path) -> str:
    d = Document(str(path))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(parts)


def test_intent_tag_phrases():
    assert wants_find_by_tag("按标签找合同")
    assert extract_tag_query("按标签找合同") == "合同"
    assert wants_tag_correct("这篇不是合同，是项目")
    assert extract_tag_correction("这篇不是合同，是项目") == ["项目"]
    assert wants_tag_backfill("回填标签")
    assert not wants_find_by_tag("内容识别后分类进各自新文档")


def test_db_path_shares_database_module():
    """生产路径必须引用 db.database.DB_PATH，禁止另写硬编码。"""
    from db import database as dbmod
    import infra.content_tags as mod

    # 模块默认绑定同一对象；conftest 会 monkeypatch，这里只校验导入来源
    assert "DB_PATH" in mod.__dict__
    # 未 patch 前的定义来自 database（conftest 已 patch，用源码约定：from db.database import）
    import inspect
    src = inspect.getsource(mod)
    assert "from db.database import DB_PATH" in src
    assert dbmod.DB_PATH.name == "filemate.db"


def test_upsert_find_override(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "get_param", lambda k, d=None: True if k == "tags.enabled" else d)
    alive = tmp_path / "a.docx"
    alive.write_bytes(b"alive-doc")

    assert ct.upsert_tags("h1", str(alive), ["contract"], source="rule")
    row = ct.get_tags("h1")
    assert row and "合同协议" in row["tags"]

    hits = ct.find_by_tag("合同")
    assert any(h["content_hash"] == "h1" for h in hits)

    # soft 不能覆盖 user
    assert ct.apply_user_override("h1", str(alive), ["项目"])
    assert ct.get_tags("h1")["tags"] == ["项目事务"]
    assert ct.upsert_tags("h1", str(alive), ["合同"], source="rule")
    assert ct.get_tags("h1")["source"] == "user"
    assert ct.get_tags("h1")["tags"] == ["项目事务"]


def test_find_skips_and_prunes_dead_paths(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "get_param", lambda k, d=None: True if k == "tags.enabled" else d)
    dead = tmp_path / "gone.docx"
    alive = tmp_path / "ok.docx"
    alive.write_bytes(b"ok")
    ct.upsert_tags("dead", str(dead), ["合同"], source="rule")
    ct.upsert_tags("live", str(alive), ["合同"], source="rule")
    hits = ct.find_by_tag("合同")
    assert [h["content_hash"] for h in hits] == ["live"]
    assert ct.get_tags("dead") is None  # 检索时顺带删死路径


def test_ensure_auto_backfill_once(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "get_param", lambda k, d=None: True if k == "tags.enabled" else d)
    folder = tmp_path / "内容分类" / "合同协议"
    folder.mkdir(parents=True)
    (folder / "x.docx").write_bytes(b"auto-backfill-bytes-001")
    r1 = ct.ensure_auto_backfill(tmp_path)
    assert r1.get("ok") and r1.get("written", 0) >= 1
    r2 = ct.ensure_auto_backfill(tmp_path)
    assert r2.get("reason") == "already" and r2.get("written", 0) == 0
    # 手动 force 可再跑
    r3 = ct.ensure_auto_backfill(tmp_path, force=True)
    assert r3.get("ok")


def test_tags_disabled_noop(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "get_param", lambda k, d=None: False if k == "tags.enabled" else d)
    assert ct.tags_enabled() is False
    assert ct.upsert_tags("h", "p", ["合同"], source="rule") is False
    assert ct.find_by_tag("合同") == []


def test_backfill_from_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "get_param", lambda k, d=None: True if k == "tags.enabled" else d)
    folder = tmp_path / "内容分类" / "合同协议"
    folder.mkdir(parents=True)
    doc = folder / "x.docx"
    doc.write_bytes(b"hello-tag-backfill-content-bytes")
    r1 = ct.backfill_from_content_dir(tmp_path / "内容分类")
    assert r1.get("ok") and r1.get("written", 0) >= 1
    r2 = ct.backfill_from_content_dir(tmp_path / "内容分类")
    assert r2.get("ok")
    # 幂等：第二次仍可写/更新 path，不爆炸
    hits = ct.find_by_tag("合同协议")
    assert hits


def test_batch_writes_tags_and_manifest_column(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    real_get = config.get_param

    def _gp(k, d=None):
        if k == "tags.enabled":
            return True
        return real_get(k, d)

    monkeypatch.setattr(config, "get_param", _gp)
    _write_docx(tmp_path / "协议.docx", CONTRACT)
    out = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out.get("ok") is True, out
    manifest = _docx_text(tmp_path / "内容分类" / "分类清单.docx")
    assert "| 标签 |" in manifest
    row = next(r for r in out["classifications"] if r.get("status") == "已分类")
    assert row.get("tags")
    digest = row.get("content_hash")
    assert digest and ct.get_tags(digest)


def test_dispatch_find_by_tag(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "get_param", lambda k, d=None: True if k == "tags.enabled" else d)
    alive = tmp_path / "a.docx"
    alive.write_bytes(b"x")
    ct.upsert_tags("abc", str(alive), ["合同"], source="rule")
    out = IntentDispatcher(None).dispatch("按标签找合同", tmp_path)
    assert out.get("handler") == "内容标签"
    assert out.get("ok") is True
    assert any("a.docx" in (p or "") for p in (out.get("paths") or []))


def test_soft_label_multi_tags_normalize():
    r = soft_label_from_model("x" * 100, "h", api_key="")
    assert r["tags"] == ["other"]
    assert r["source"] == "fallback_no_key"


def test_soft_quota_counts_failures(tmp_path: Path, monkeypatch):
    """模型失败也消耗配额，不能靠一直报错绕过 20 上限。"""
    import pipeline.content_split.batch_new_docs as bn
    from pipeline.content_split import soft_label as sl

    monkeypatch.setattr(config, "get_api_key", lambda: "sk-test")
    monkeypatch.setattr(sl, "SOFT_LABEL_MAX_PER_RUN", 2)
    monkeypatch.setattr(bn, "SOFT_LABEL_MAX_PER_RUN", 2)

    calls = {"n": 0}

    def _fail(text, content_hash, *, api_key="", cache=None):
        calls["n"] += 1
        return {
            "doc_type": "other",
            "tags": ["other"],
            "confidence": 0.0,
            "source": "fallback_error",
        }

    monkeypatch.setattr(bn, "soft_label_from_model", _fail)
    # 低置信噪声文 ×3：应只尝试 2 次软标签
    noise = (
        "今天天气不错，我们去公园散步，买了两杯咖啡，聊了些近况与旅行计划，"
        "傍晚又看了一场电影，回家后简单写了这篇日记当作备忘留存，"
        "明天打算早点出门把杂事处理完再回来休息。"
    )
    for i in range(3):
        _write_docx(tmp_path / f"流水{i}.docx", noise + f" 变体{i}。")
    out = run_batch_content_new_docs("内容识别后分类进各自新文档", tmp_path)
    assert out.get("ok") is True, out
    assert calls["n"] == 2
    assert out.get("soft_model_calls") == 2
