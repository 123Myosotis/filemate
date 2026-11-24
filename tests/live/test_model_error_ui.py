# -*- coding: utf-8 -*-
"""L5：模型报错时，界面是「人话」还是「天书」，以及能不能重试。

三个真实报错场景（在 HTTP 客户端层注入，界面与调度链路保持真实）：
1) 网络断开（ConnectionError）
2) 模型不按 JSON 输出（乱码 / 闲聊）
3) 跨表查询中途模型超时（QP 拿不到计划）

每个用例都检查：
- 界面提示是否能看懂、有没有把原始异常/模型乱码直接甩给用户；
- 失败时有没有动文件、有没有产出假结果；
- 失败后发送按钮是否恢复，重新发送同一句能不能成功（可重试）。

默认跳过：
    set FILEMATE_LIVE=1
    python -m pytest tests/live/test_model_error_ui.py -q -s
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path

import pytest
import requests
from docx import Document
from openpyxl import Workbook

pytestmark = pytest.mark.live

EXPORT_ROOT = Path(tempfile.gettempdir()) / "filemate_query_exports"

RAW_LEAK_MARKERS = (
    "Traceback", "ConnectionError", "NewConnectionError", "Max retries exceeded",
    "JSONDecodeError", "ValueError", "KeyError", "请求失败:",
)


def _need_live() -> None:
    if os.environ.get("FILEMATE_LIVE") != "1":
        pytest.skip("未开启真实模型测试：设置 FILEMATE_LIVE=1")
    import config

    if not config.get_api_key():
        pytest.skip("缺少聊天模型 Key（config.json），跳过 L5")


def _wait_until(app, predicate, timeout=300.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.05)
    app.processEvents()
    return predicate()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_docx(path: Path) -> None:
    doc = Document()
    doc.add_heading("设备说明", level=1)
    doc.add_paragraph("本设备为A型机，适用于室内。")
    doc.save(str(path))


def _make_tables(root: Path) -> None:
    wb1 = Workbook()
    ws1 = wb1.active
    ws1.append(["工号", "姓名", "部门"])
    ws1.append(["E01", "张三", "研发"])
    ws1.append(["E02", "李四", "销售"])
    wb1.save(root / "员工台账.xlsx")

    wb2 = Workbook()
    ws2 = wb2.active
    ws2.append(["工号", "项目", "金额"])
    ws2.append(["E01", "A项目", 120])
    ws2.append(["E02", "B项目", 80])
    wb2.save(root / "项目分配.xlsx")


def _isolate(tmp_path, monkeypatch) -> None:
    import db.database as db_mod

    db_path = tmp_path / "live.db"
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    try:
        import infra.content_tags as ct

        monkeypatch.setattr(ct, "DB_PATH", db_path)
    except Exception:  # noqa: BLE001
        pass
    try:
        import pipeline.word_office.memory as word_memory

        monkeypatch.setattr(word_memory, "DB_PATH", db_path)
    except Exception:  # noqa: BLE001
        pass
    import pipeline.word_office.txn as txn

    monkeypatch.setattr(txn, "backup_root", lambda: tmp_path / "word_backups")


def _silence_dialogs(monkeypatch, ws: Path) -> None:
    from PyQt6.QtWidgets import QFileDialog, QMessageBox

    for name in ("warning", "information", "critical"):
        monkeypatch.setattr(
            QMessageBox, name,
            staticmethod(lambda *a, _n=name, **k: (print("[DIALOG]", _n, a),
                                                  QMessageBox.StandardButton.Ok)[1]),
        )
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: (print("[DIALOG] question", a),
                                     QMessageBox.StandardButton.No)[1]),
    )
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(ws)))


def _boot(ws: Path):
    from PyQt6.QtWidgets import QApplication

    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    tab = window.chat_tabs.widget(0)
    assert tab is not None
    tab.setProperty("workspace_path", str(ws))
    window.file_explorer.current_path = ws
    window._apply_workspace_gate(tab)
    return app, window, tab


def _send(window, app, tab, text: str, timeout: float = 300.0) -> str:
    chat = tab.property("chat_display")
    before = chat.toPlainText()
    tab.property("input_box").setText(text)
    tab.property("send_btn").click()
    assert window._worker is not None, (
        "点击发送后没有启动任务；聊天=%r" % chat.toPlainText()[:500])

    seen = False

    def _done() -> bool:
        nonlocal seen
        if window._worker is not None:
            seen = True
            return False
        return seen

    assert _wait_until(app, _done, timeout), "等待执行超时（%ss）" % timeout
    full = chat.toPlainText()
    return full[len(before):] if full.startswith(before) else full


def _assert_ui_recovered(window, tab) -> None:
    """报错后界面必须还能继续用（而不是卡在运行态）。"""
    assert window._worker is None
    assert tab.property("send_btn").isEnabled(), "报错后发送按钮仍被禁用"
    assert tab.property("input_box").isEnabled(), "报错后输入框仍被禁用"


def _assert_no_raw_leak(text: str) -> None:
    for marker in RAW_LEAK_MARKERS:
        assert marker not in text, "把原始异常直接甩给了用户（%s）：\n%s" % (marker, text)


def _patch_chat_completion(monkeypatch):
    """模拟「模型接口层」报错：mode=network 断网；mode=junk 返回非 JSON。"""
    import ai.client as ai_client

    real = ai_client.chat_completion
    state = {"fail": True, "mode": "network"}

    def fake(*a, **k):
        if not state["fail"]:
            return real(*a, **k)
        if state["mode"] == "network":
            raise requests.exceptions.ConnectionError(
                "Max retries exceeded with url: /chat/completions "
                "(Caused by NewConnectionError)")
        return {"choices": [{"message": {"content": "抱歉，我这边暂时无法回答"}}]}

    monkeypatch.setattr(ai_client, "chat_completion", fake)
    return state


def test_network_error_human_message_and_retry_works(tmp_path, monkeypatch):
    """断网：人话提示、文件不动、恢复后重发能成功。"""
    _need_live()
    _isolate(tmp_path, monkeypatch)
    ws = tmp_path / "工作区"
    ws.mkdir()
    docx = ws / "设备说明.docx"
    _make_docx(docx)
    _silence_dialogs(monkeypatch, ws)
    state = _patch_chat_completion(monkeypatch)

    app, window, tab = _boot(ws)
    try:
        window._add_attachments(tab, [str(docx)])
        before = _sha(docx)
        text = _send(window, app, tab, "把「A型机」替换成「C型机」")
        print("\n[网络错误] 界面回复：\n%s" % text)

        assert "暂时不可用" in text or "稍后重试" in text, (
            "断网提示不像人话：\n" + text)
        _assert_no_raw_leak(text)
        assert _sha(docx) == before, "断网时却动了文件"
        assert not Path(str(docx) + ".bak").exists(), "断网时却建了备份"
        _assert_ui_recovered(window, tab)

        # 重试：网络恢复后，原样再发一次
        state["fail"] = False
        text2 = _send(window, app, tab, "把「A型机」替换成「C型机」")
        print("\n[网络恢复后重发] 界面回复：\n%s" % text2)
        assert "已替换" in text2, "恢复后重发没有成功：\n" + text2
        assert "C型机" in Document(str(docx)).paragraphs[1].text
    finally:
        window.close()
        app.processEvents()


def test_non_json_model_output_human_message(tmp_path, monkeypatch):
    """模型不按 JSON 输出：提示要能看懂，不能把模型乱码贴给用户。"""
    _need_live()
    _isolate(tmp_path, monkeypatch)
    ws = tmp_path / "工作区"
    ws.mkdir()
    docx = ws / "设备说明.docx"
    _make_docx(docx)
    _silence_dialogs(monkeypatch, ws)
    state = _patch_chat_completion(monkeypatch)
    state["mode"] = "junk"

    app, window, tab = _boot(ws)
    try:
        window._add_attachments(tab, [str(docx)])
        before = _sha(docx)
        text = _send(window, app, tab, "把「A型机」替换成「C型机」")
        print("\n[模型乱码] 界面回复：\n%s" % text)

        assert "无法解析" in text or "暂时不可用" in text or "换个说法" in text, (
            "模型乱码时提示不明确：\n" + text)
        assert "抱歉，我这边暂时无法回答" not in text, "把模型乱码原文贴进了对话"
        _assert_no_raw_leak(text)
        assert _sha(docx) == before
        _assert_ui_recovered(window, tab)
    finally:
        window.close()
        app.processEvents()


def test_table_query_model_failure_human_and_retry(tmp_path, monkeypatch):
    """跨表查询中途模型超时：人话提示、不出假表、恢复后重发能出结果。"""
    _need_live()
    _isolate(tmp_path, monkeypatch)
    ws = tmp_path / "工作区"
    ws.mkdir()
    _make_tables(ws)
    _silence_dialogs(monkeypatch, ws)

    import pipeline.block_cross_table.query_engine.qp_ai as qp_ai

    real = qp_ai.chat_json
    state = {"fail": True}

    def fake(*a, **k):
        if not state["fail"]:
            return real(*a, **k)
        fake.last_error = "Timeout: 模型响应超时（模拟）"
        return None

    fake.last_error = ""
    monkeypatch.setattr(qp_ai, "chat_json", fake)

    before_exports = set(EXPORT_ROOT.glob("*/*")) if EXPORT_ROOT.is_dir() else set()
    query = "请帮我统计每个员工的姓名、部门和项目金额，按金额从高到低排序"

    app, window, tab = _boot(ws)
    new_exports = set()
    try:
        text = _send(window, app, tab, query, timeout=420)
        print("\n[跨表模型失败] 界面回复：\n%s" % text)

        assert "跨表查询" in text and ("失败" in text or "未完成" in text), (
            "跨表模型失败时提示不明确：\n" + text)
        assert "✅ 跨表查询完成" not in text, "模型失败却给出了查询结果"
        _assert_no_raw_leak(text)
        after_exports = set(EXPORT_ROOT.glob("*/*")) if EXPORT_ROOT.is_dir() else set()
        new_exports = after_exports - before_exports
        assert not new_exports, "模型失败却导出了文件：%r" % [str(p) for p in new_exports]
        _assert_ui_recovered(window, tab)

        # 重试：QP 恢复后，原样再发一次
        state["fail"] = False
        text2 = _send(window, app, tab, query, timeout=420)
        print("\n[跨表恢复后重发] 界面回复：\n%s" % text2)
        assert "跨表查询完成" in text2, "恢复后重发没有成功：\n" + text2
        after2 = set(EXPORT_ROOT.glob("*/*")) if EXPORT_ROOT.is_dir() else set()
        new_exports |= (after2 - before_exports)
        assert any("结果表" in line for line in text2.splitlines()), (
            "重试成功后没有给出结果表路径：\n" + text2)
    finally:
        window.close()
        app.processEvents()
        for p in new_exports:
            try:
                p.unlink()
            except OSError:
                pass
