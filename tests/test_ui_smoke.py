# -*- coding: utf-8 -*-
"""UI 冒烟：**MainWindow 能构造**（不显示、不交互）。

只验证"起得来"，不做 UI 自动化（那要 pytest-qt + 稳定选择器）。
"""
from __future__ import annotations

import os

import pytest


def test_main_window_construct():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from ui.main_window import MainWindow

    w = MainWindow()
    try:
        assert w is not None
        assert w.windowTitle() or True
    finally:
        w.close()


def test_dispatch_worker_run():
    """交互层冒烟：`_DispatchWorker.run()` 同步跑，走通 dispatch → 结果信号。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    from ui.main_window import _DispatchWorker

    class FakeDispatcher:
        def dispatch(self, text, target, **kwargs):
            return {"handler": "WordOffice", "status": "done",
                    "message": "ok", "confirmed": kwargs.get("confirmed")}

    w = _DispatchWorker(FakeDispatcher(), "hi", ".")
    got = {}
    w.finished_result.connect(lambda r: got.update(r))
    w.run()  # 同步
    assert got.get("status") == "done"
    assert got.get("message") == "ok"
