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
