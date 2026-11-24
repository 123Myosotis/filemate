# -*- coding: utf-8 -*-
"""文本文件模块对外入口（与 WordModule.handle 同签名）。"""
from __future__ import annotations

from pipeline.office_core import run

from .adapter import TextAdapter


class TextModule:
    @staticmethod
    def handle(request: dict) -> dict:
        return run(TextAdapter(), request)
