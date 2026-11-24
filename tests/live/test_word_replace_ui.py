# -*- coding: utf-8 -*-
"""L5：真实界面 + 真实模型 —— Word 替换（文字 + 格式）。

验证三件事，而不是只看“执行成功”：
1) 界面上真的发送并完成；
2) 正文里的文字确实被替换；
3) 段落样式与加粗格式没有被破坏，且写前备份存在。

默认跳过，需显式开启：
    set FILEMATE_LIVE=1
    python -m pytest tests/live/test_word_replace_ui.py -q -s
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from docx import Document

pytestmark = pytest.mark.live


def _need_live() -> None:
    if os.environ.get("FILEMATE_LIVE") != "1":
        pytest.skip("未开启真实模型测试：设置 FILEMATE_LIVE=1")
    import config

    if not config.get_api_key():
        pytest.skip("缺少聊天模型 Key（config.json），跳过 L5")


def _make_docx(path: Path) -> None:
    doc = Document()
    doc.add_heading("设备说明", level=1)
    p1 = doc.add_paragraph()
    p1.add_run("本设备为")
    p1.add_run("A型机")
    p1.add_run("，适用于室内。")
    p2 = doc.add_paragraph()
    bold = p2.add_run("A型机")
    bold.bold = True
    p2.add_run("必须先断电再维护。")
    doc.save(str(path))


def _wait_until(app, predicate, timeout=300.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.05)
    app.processEvents()
    return predicate()


def test_word_replace_on_real_ui(tmp_path, monkeypatch):
    _need_live()
    ws = tmp_path / "工作区"
    ws.mkdir()
    docx = ws / "测试文档.docx"
    _make_docx(docx)

    # 隔离数据库与备份目录，避免污染真实运行数据
    import db.database as db_mod

    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "live.db")
    db_mod.init_db()
    db_mod.add_channel("L5 Word", workspace_path=str(ws))
    try:
        import pipeline.word_office.memory as word_memory

        monkeypatch.setattr(word_memory, "DB_PATH", tmp_path / "live.db")
    except Exception:
        pass
    import pipeline.word_office.txn as txn

    backup_dir = tmp_path / "word_backups"
    monkeypatch.setattr(txn, "backup_root", lambda: backup_dir)

    from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox

    from ui.main_window import MainWindow

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

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        tab = window.chat_tabs.widget(0)
        assert tab is not None
        tab.setProperty("workspace_path", str(ws))
        window.file_explorer.current_path = ws
        window._apply_workspace_gate(tab)

        # 走界面真正的“附件”通道，而不是绕过 UI 改内部参数
        window._add_attachments(tab, [str(docx)])
        assert getattr(tab, "attach_paths", []), "附件没有加进当前频道"

        input_box = tab.property("input_box")
        send_btn = tab.property("send_btn")
        chat = tab.property("chat_display")
        input_box.setText("把「A型机」替换成「C型机」")
        send_btn.click()
        assert window._worker is not None, (
            "点击发送后没有启动任务；聊天=%r" % chat.toPlainText()[:500])

        seen = False

        def _done() -> bool:
            nonlocal seen
            if window._worker is not None:
                seen = True
                return False
            return seen

        assert _wait_until(app, _done), "等待真实模型执行超时（300s）"
        text = chat.toPlainText()
        assert "已替换" in text, "界面没有替换成功提示：\n" + text

        result = Document(str(docx))
        body = "\n".join(p.text for p in result.paragraphs)
        assert "A型机" not in body, "正文仍有未替换内容：\n" + body
        assert body.count("C型机") == 2, "替换数量不对：\n" + body

        heading = result.paragraphs[0]
        assert heading.text.strip() == "设备说明"
        assert heading.style.name.lower().startswith("heading"), "标题样式被破坏：%s" % heading.style.name

        bold_runs = [r for r in result.paragraphs if r.text.strip().startswith("C型机")]
        assert bold_runs, "没找到替换后的加粗段落"
        first_run = bold_runs[-1].runs[0]
        assert first_run.text.strip() == "C型机"
        assert first_run.bold is True, "替换后加粗格式丢失"

        assert (ws / "测试文档.docx.bak").is_file(), "缺少同目录 .bak 备份"
        assert any(backup_dir.glob("*.docx")), "缺少独立快照"
    finally:
        window.close()
        app.processEvents()
