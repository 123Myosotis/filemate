# -*- coding: utf-8 -*-
"""L5：真实界面 + 真实模型 —— 跨表查询。

不是只验证“链路成功”，而是：
1) 真的在输入框打字、点发送；
2) 等界面出现结果；
3) 打开导出的 Excel，逐行核对数据与排序。

默认跳过，需显式开启：
    set FILEMATE_LIVE=1
    python -m pytest tests/live/test_cross_table_ui.py -q -s
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

pytestmark = pytest.mark.live


def _need_live() -> None:
    if os.environ.get("FILEMATE_LIVE") != "1":
        pytest.skip("未开启真实模型测试：设置 FILEMATE_LIVE=1")
    import config

    if not config.get_api_key():
        pytest.skip("缺少聊天模型 Key（config.json），跳过 L5")


def _make_tables(root: Path) -> None:
    """员工台账 + 项目分配：工号关联，金额用于验证排序。"""
    wb1 = Workbook()
    ws1 = wb1.active
    ws1.append(["工号", "姓名", "部门"])
    ws1.append(["E01", "张三", "研发"])
    ws1.append(["E02", "李四", "销售"])
    ws1.append(["E03", "王五", "研发"])
    ws1.append(["E04", "赵六", "财务"])
    wb1.save(root / "员工台账.xlsx")

    wb2 = Workbook()
    ws2 = wb2.active
    ws2.append(["工号", "项目", "金额"])
    ws2.append(["E01", "A项目", 120])
    ws2.append(["E02", "B项目", 80])
    ws2.append(["E03", "C项目", 50])
    ws2.append(["E04", "D项目", 30])
    wb2.save(root / "项目分配.xlsx")


def _num(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").replace(",", "").strip()
    return float(text or 0)


def _read_export(path: Path):
    """返回 [(姓名, 部门, 项目, 金额)]，按导出文件原始顺序。"""
    wb = load_workbook(path, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = [[c for c in row] for row in ws.iter_rows(values_only=True)]
    header_idx = None
    for i, row in enumerate(rows[:10]):
        text = "".join(str(c or "") for c in row)
        if "姓名" in text and ("金额" in text or "项目" in text):
            header_idx = i
            break
    assert header_idx is not None, "导出表缺少表头（姓名/金额）: %r" % rows[:5]
    header = [str(c or "").strip() for c in rows[header_idx]]

    def col(*names):
        for n in names:
            for j, h in enumerate(header):
                if n in h:
                    return j
        raise AssertionError("导出表缺少列 %s，实际表头=%r" % (names, header))

    i_name, i_dept, i_proj, i_amt = (
        col("姓名"), col("部门"), col("项目"), col("金额"))
    out = []
    for row in rows[header_idx + 1:]:
        if not any(c not in (None, "") for c in row):
            continue
        if str(row[i_name] or "").strip() == "姓名":  # 重复表头行
            continue
        out.append((
            str(row[i_name] or "").strip(),
            str(row[i_dept] or "").strip(),
            str(row[i_proj] or "").strip(),
            _num(row[i_amt]),
        ))
    return out


def _wait_until(app, predicate, timeout=300.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.05)
    app.processEvents()
    return predicate()


def test_cross_table_query_on_real_ui(tmp_path, monkeypatch):
    _need_live()
    ws = tmp_path / "工作区"
    ws.mkdir()
    _make_tables(ws)

    # 隔离数据库：频道、消息、日志都写临时库
    import db.database as db_mod

    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "live.db")
    db_mod.init_db()
    db_mod.add_channel("L5 跨表", workspace_path=str(ws))

    from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox

    from ui.main_window import MainWindow

    # 防止任何模态框把测试卡死：记录并放行
    for name in ("warning", "information", "critical"):
        monkeypatch.setattr(
            QMessageBox, name,
            staticmethod(lambda *a, _n=name, **k: (print("[DIALOG]", _n, a), QMessageBox.StandardButton.Ok)[1]),
        )
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: (print("[DIALOG] question", a), QMessageBox.StandardButton.No)[1]),
    )
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(ws)))

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        tab = window.chat_tabs.widget(0)
        assert tab is not None
        # 明确当前工作区，避免依赖资源管理器初始状态
        tab.setProperty("workspace_path", str(ws))
        window.file_explorer.current_path = ws
        window._apply_workspace_gate(tab)

        input_box = tab.property("input_box")
        send_btn = tab.property("send_btn")
        chat = tab.property("chat_display")
        assert input_box and send_btn and chat

        query = (
            "根据当前目录下的『员工台账.xlsx』和『项目分配.xlsx』，"
            "按工号关联两张表，列出姓名、部门、项目、金额，"
            "并按金额从高到低排序。"
        )
        input_box.setText(query)
        send_btn.click()
        assert window._worker is not None, (
            "点击发送后没有启动任务；send_enabled=%s，聊天=%r"
            % (send_btn.isEnabled(), chat.toPlainText()[:500])
        )

        seen_worker = False

        def _done() -> bool:
            nonlocal seen_worker
            if window._worker is not None:
                seen_worker = True
                return False
            return seen_worker

        assert _wait_until(app, _done, timeout=300.0), "等待真实模型执行超时（300s）"

        text = chat.toPlainText()
        export_path = str(tab.property("export_path") or "")
        assert export_path, "界面没有绑定结果表。回复内容：\n" + text
        assert Path(export_path).is_file(), "结果表不存在：%s" % export_path
        assert "查询完成" in text or "共" in text, "界面未显示查询完成：\n" + text

        rows = _read_export(Path(export_path))
        expected = [
            ("张三", "研发", "A项目", 120.0),
            ("李四", "销售", "B项目", 80.0),
            ("王五", "研发", "C项目", 50.0),
            ("赵六", "财务", "D项目", 30.0),
        ]
        assert rows == expected, "结果数据不符合预期：\n实际=%r\n期望=%r" % (rows, expected)
    finally:
        # 清理本轮导出的结果表（临时目录，仍显式删除）
        try:
            path = str(tab.property("export_path") or "")
            if path:
                Path(path).unlink(missing_ok=True)
        except Exception:
            pass
        window.close()
        app.processEvents()
