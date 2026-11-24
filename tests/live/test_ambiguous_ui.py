# -*- coding: utf-8 -*-
"""L5：信息不全时，系统是「追问」还是「瞎猜」。

每个用例都同时看两件事：
1) 界面上有没有提出确认/补充信息，而不是直接报成功；
2) 磁盘有没有被动过（缺少信息时不该产生任何写入、备份或导出）。

覆盖 5 种典型的「信息不全」：
A. Word 指令含糊（「帮我改一下这个文档」）
B. Word 写指令但没选中文件
C. Word 写指令但选中了多份候选
D. 跨表查询但目录里没有表
E. 跨表查询有表但问题没说清（不该直接出表）

默认跳过：
    set FILEMATE_LIVE=1
    python -m pytest tests/live/test_ambiguous_ui.py -q -s
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path

import pytest
from docx import Document
from openpyxl import Workbook

pytestmark = pytest.mark.live

# 追问/澄清的界面标记（不同链路用词不同）
ASK_MARKERS = (
    "请先", "请确认", "需要你确认", "没太确定", "不确定", "请告诉我", "请说明",
    "哪个", "哪份", "哪一份", "哪一个", "具体", "你想", "怎么改",
    "改什么", "哪些", "选中", "需要 Excel", "未完成", "❓",
)

EXPORT_ROOT = Path(tempfile.gettempdir()) / "filemate_query_exports"

# 追问可能发生在聊天区，也可能弹确认框；两处都算“问了”
DIALOG_LOG: list[str] = []


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


def _make_docx(path: Path, marker: str = "A型机") -> None:
    doc = Document()
    doc.add_heading("设备说明", level=1)
    doc.add_paragraph("本设备为%s，适用于室内。" % marker)
    doc.save(str(path))


def _make_xlsx(path: Path, header: list, rows: list) -> None:
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for row in rows:
        ws.append(row)
    wb.save(path)


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


def _silence_dialogs(monkeypatch, ws: Path, question_yes: bool = False) -> None:
    from PyQt6.QtWidgets import QFileDialog, QMessageBox

    DIALOG_LOG.clear()

    def _ok(name):
        def _fn(*a, **k):
            DIALOG_LOG.append("[%s] %s" % (name, " ".join(str(x) for x in a)))
            return QMessageBox.StandardButton.Ok
        return staticmethod(_fn)

    def _answer(name):
        def _fn(*a, **k):
            DIALOG_LOG.append("[%s] %s" % (name, " ".join(str(x) for x in a)))
            return (QMessageBox.StandardButton.Yes if question_yes
                    else QMessageBox.StandardButton.No)
        return staticmethod(_fn)

    for name in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, name, _ok(name))
    monkeypatch.setattr(QMessageBox, "question", _answer("question"))
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
    """真实输入 + 点发送，返回本轮新增的聊天文本。"""
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

    assert _wait_until(app, _done, timeout), "等待模型执行超时（%ss）" % timeout
    full = chat.toPlainText()
    return full[len(before):] if full.startswith(before) else full


def _asked(text: str) -> bool:
    return any(m in text for m in ASK_MARKERS)


def _asked_any(text: str) -> bool:
    """聊天区或确认框任一处出现追问都算。"""
    return _asked(text) or _asked("\n".join(DIALOG_LOG))


def test_a_vague_word_instruction_asks(tmp_path, monkeypatch):
    """A：只说「改一下」，没有具体改什么 → 应追问，文件不动。"""
    _need_live()
    _isolate(tmp_path, monkeypatch)
    ws = tmp_path / "工作区"
    ws.mkdir()
    docx = ws / "设备说明.docx"
    _make_docx(docx)
    _silence_dialogs(monkeypatch, ws)

    app, window, tab = _boot(ws)
    try:
        window._add_attachments(tab, [str(docx)])
        before = _sha(docx)
        text = _send(window, app, tab, "帮我改一下这个文档")
        print("\n[A] 界面回复：\n%s" % text)
        print("[A] 确认框：\n%s" % "\n".join(DIALOG_LOG))
        assert _asked_any(text), "没有追问，直接给了结果：\n" + text
        assert _sha(docx) == before, "信息不全却改了文件"
        assert not Path(str(docx) + ".bak").exists(), "信息不全却建了备份"
    finally:
        window.close()
        app.processEvents()


def test_b_word_write_without_selected_file_asks(tmp_path, monkeypatch):
    """B：替换指令但没选中任何文件 → 应要求先选文件，不能自己挑一个。"""
    _need_live()
    _isolate(tmp_path, monkeypatch)
    ws = tmp_path / "工作区"
    ws.mkdir()
    a = ws / "甲.docx"
    b = ws / "乙.docx"
    _make_docx(a)
    _make_docx(b)
    _silence_dialogs(monkeypatch, ws)

    app, window, tab = _boot(ws)
    try:
        before = {p.name: _sha(p) for p in (a, b)}
        text = _send(window, app, tab, "把「A型机」替换成「C型机」")
        print("\n[B] 界面回复：\n%s" % text)
        assert _asked_any(text), "没有要求选文件，直接执行了：\n" + text
        for p in (a, b):
            assert _sha(p) == before[p.name], "没选文件却动了：%s" % p.name
            assert not Path(str(p) + ".bak").exists(), "没选文件却建了备份：%s" % p.name
    finally:
        window.close()
        app.processEvents()


def test_c_multiple_word_candidates_asks_which(tmp_path, monkeypatch):
    """C：同时选中两份都可能匹配的文档 → 应追问是哪一份。"""
    _need_live()
    _isolate(tmp_path, monkeypatch)
    ws = tmp_path / "工作区"
    ws.mkdir()
    a = ws / "设备说明-甲.docx"
    b = ws / "设备说明-乙.docx"
    _make_docx(a)
    _make_docx(b)
    _silence_dialogs(monkeypatch, ws)

    app, window, tab = _boot(ws)
    try:
        window._add_attachments(tab, [str(a), str(b)])
        before = {p.name: _sha(p) for p in (a, b)}
        text = _send(window, app, tab, "把「A型机」替换成「C型机」")
        print("\n[C] 界面回复：\n%s" % text)
        print("[C] 确认框：\n%s" % "\n".join(DIALOG_LOG))
        changed = [p.name for p in (a, b) if _sha(p) != before[p.name]]
        assert not changed, (
            "选中两份文档时没有追问，直接改了 %r（界面未指明改的是哪一份）：\n%s"
            % (changed, text))
        assert _asked_any(text), "多份候选时没有追问是哪一份：\n" + text
    finally:
        window.close()
        app.processEvents()


def test_f_confirm_after_ask_actually_runs(tmp_path, monkeypatch):
    """确认框点「是」后必须真的接着执行，而不是静默什么都不做。"""
    _need_live()
    _isolate(tmp_path, monkeypatch)
    ws = tmp_path / "工作区"
    ws.mkdir()
    docx = ws / "设备说明.docx"
    _make_docx(docx)
    _silence_dialogs(monkeypatch, ws, question_yes=True)

    app, window, tab = _boot(ws)
    try:
        window._add_attachments(tab, [str(docx)])
        text = _send(window, app, tab, "帮我改一下这个文档")
        print("\n[F] 界面回复：\n%s" % text)
        print("[F] 确认框：\n%s" % "\n".join(DIALOG_LOG))
        assert _asked_any(text), "没有先追问：\n" + text

        # 点「是」后会再起一个 worker；等它把结构检查结果贴出来
        chat = tab.property("chat_display")
        deadline = time.time() + 60
        while time.time() < deadline and "结构检查" not in chat.toPlainText():
            app.processEvents()
            time.sleep(0.05)
        full = chat.toPlainText()
        assert "结构检查" in full, "点了「是」却没有继续执行：\n" + full
    finally:
        window.close()
        app.processEvents()


def test_d_table_query_without_tables_asks(tmp_path, monkeypatch):
    """D：目录里没有表格却说查表 → 应要求选表，不能凭空答。"""
    _need_live()
    _isolate(tmp_path, monkeypatch)
    ws = tmp_path / "工作区"
    ws.mkdir()
    _make_docx(ws / "说明.docx")
    _silence_dialogs(monkeypatch, ws)

    app, window, tab = _boot(ws)
    try:
        text = _send(window, app, tab, "帮我查一下这些表里的数据")
        print("\n[D] 界面回复：\n%s" % text)
        assert _asked_any(text), "没有表却没有要求补充：\n" + text
        assert "✅ 跨表查询完成" not in text, "没有表却出了查询结果"
    finally:
        window.close()
        app.processEvents()


def test_e_vague_table_query_does_not_invent_export(tmp_path, monkeypatch):
    """E：有表但问题没说清 → 可以追问或明确不做，不能直接猜着出表。"""
    _need_live()
    _isolate(tmp_path, monkeypatch)
    ws = tmp_path / "工作区"
    ws.mkdir()
    _make_xlsx(ws / "员工台账.xlsx", ["工号", "姓名", "部门"],
               [["E01", "张三", "研发"], ["E02", "李四", "销售"]])
    _make_xlsx(ws / "项目分配.xlsx", ["工号", "项目", "金额"],
               [["E01", "A项目", 120], ["E02", "B项目", 80]])
    _silence_dialogs(monkeypatch, ws)

    before_exports = set(EXPORT_ROOT.glob("*/*")) if EXPORT_ROOT.is_dir() else set()

    app, window, tab = _boot(ws)
    try:
        text = _send(window, app, tab, "帮我统计一下这两张表的数据")
        print("\n[E] 界面回复：\n%s" % text)
        after_exports = set(EXPORT_ROOT.glob("*/*")) if EXPORT_ROOT.is_dir() else set()
        new_exports = after_exports - before_exports
        assert "✅ 跨表查询完成" not in text, "问题没说清却直接出了表：\n" + text
        assert not new_exports, "问题没说清却导出了文件：%r" % [str(p) for p in new_exports]
        assert _asked_any(text), "问题没说清也没有追问：\n" + text
    finally:
        window.close()
        app.processEvents()
