# -*- coding: utf-8 -*-
"""L5 真实模型 + 真实界面测试的公共设置。"""
from __future__ import annotations

import os

# 必须在创建 QApplication 之前设置，避免测试机没有显示器时失败
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def pytest_configure(config):
    config.addinivalue_line("markers", "live: 真实模型 + 真实界面的 L5 测试")

