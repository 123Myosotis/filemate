# -*- coding: utf-8 -*-
"""公共执行管线（供 text_office / pdf_office 等格式模块复用）。"""
from .adapter import DocumentAdapter
from .runner import run

__all__ = ["DocumentAdapter", "run"]
