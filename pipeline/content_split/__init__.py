# -*- coding: utf-8 -*-
"""内容成稿族：P1 六类另存 / P2A 结构拆段 / P2B 模板填槽（仿写不做）。"""
from .batch_new_docs import run_batch_content_new_docs
from .state import cleanup_orphan_indexes
from .structure_split import run_structure_split
from .template_fill import run_template_fill

__all__ = [
    "run_batch_content_new_docs",
    "run_structure_split",
    "run_template_fill",
    "cleanup_orphan_indexes",
]
