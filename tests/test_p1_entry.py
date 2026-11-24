# -*- coding: utf-8 -*-
"""P1 入口：文案不绑厂商；按钮预览 dry-run 不动文件；无 Key 提示走聊天模型设置。"""
from pathlib import Path

import config
from engine.dispatcher import IntentDispatcher
from engine.file_organize_apply import apply_moves, plan_moves, preview_text
from ui.entry_copy import HELP_HTML, INPUT_PLACEHOLDER, WELCOME_HTML


def test_entry_copy_not_vendor_locked():
    blob = WELCOME_HTML + INPUT_PLACEHOLDER + HELP_HTML
    assert "智谱" not in blob
    assert "设置智谱" not in blob
    assert "不需要 API Key" in WELCOME_HTML
    assert "本地 Web" in WELCOME_HTML
    assert "聊天模型设置" in WELCOME_HTML
    assert "DeepSeek" in WELCOME_HTML or "OpenAI 兼容" in WELCOME_HTML


def test_plan_moves_dry_run_does_not_move(tmp_path):
    src = tmp_path / "a.pdf"
    src.write_bytes(b"%PDF")
    plan = plan_moves([src])
    assert src.is_file()
    assert plan["moved"]
    dst = Path(plan["moved"][0][1])
    assert "文档" in dst.parts
    assert not dst.exists()
    text = preview_text(plan)
    assert "a.pdf" in text
    assert "确定后才移动" in text


def test_apply_moves_uses_contract(tmp_path):
    src = tmp_path / "合同A.pdf"
    src.write_bytes(b"x")
    other = tmp_path / "其它.pdf"
    other.write_bytes(b"y")
    out = apply_moves([src, other])
    assert (tmp_path / "合同类" / "合同A.pdf").is_file() or (
        tmp_path / "文档" / "合同A.pdf"
    ).is_file()
    assert out["moved"]


def test_dispatch_no_key_miss_mentions_settings_not_zhipu(tmp_path, monkeypatch):
    (tmp_path / "a.pdf").write_bytes(b"x")
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    out = IntentDispatcher(None).dispatch("帮我分析这段文字的风格", tmp_path)
    msg = out.get("message") or ""
    assert "智谱" not in msg
    assert "聊天模型设置" in msg
    assert "API Key" in msg
    assert (tmp_path / "a.pdf").is_file()
    assert not out.get("operations")
