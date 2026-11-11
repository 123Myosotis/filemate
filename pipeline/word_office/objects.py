# -*- coding: utf-8 -*-
"""Word 的**九对象**（规格 §九）：最小可用的 dataclass。

- **9/9 全用**：Intent / DocumentState / Plan / PatchSet / ToolRegistry /
  CapabilityManifest / ConfirmationOverlay / IntermediateArtifact / RedactionPolicy。
- 原则：**不重复造**——能复用 `module.state` 的就不重定义。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Intent:
    """用户意图（粗）."""
    user_input: str = ""
    action: str = ""          # file_organize / table_query / word_office / chat
    target: str = ""          # 文件路径
    op: str = ""              # op_type
    confidence: float = 1.0


@dataclass
class DocumentState:
    """文档状态（扫描结果 + 指纹）."""
    path: str = ""
    paragraphs: int = 0
    styles: dict = field(default_factory=dict)
    tables: list = field(default_factory=list)
    inline_shapes: int = 0
    file_sha256: str = ""
    structure_sha256: str = ""


@dataclass
class Plan:
    """执行计划（tier + operations）."""
    tier: str = "A"
    operations: list = field(default_factory=list)


@dataclass
class PatchSet:
    """补丁集（一次执行里的所有改动）."""
    ops: list = field(default_factory=list)
    backup_path: str = ""
    applied: int = 0


@dataclass
class ToolRegistry:
    """工具注册表（op → 执行器名）."""
    tools: dict = field(default_factory=lambda: {
        "inspect": "_execute",
        "find_replace": "_execute",
        "insert": "_exec_insert",
        "delete": "_exec_delete",
        "format": "_exec_format",
        "move": "_exec_move",
        "tracked_change": "_exec_tracked_change",
        "outline": "_exec_outline",
        "table_to_text": "_exec_table_to_text",
        "text_to_table": "_exec_text_to_table",
        "table_to_outline": "_exec_table_to_outline",
        "outline_to_table": "_exec_outline_to_table",
        "l1": "_exec_l1",
    })


@dataclass
class CapabilityManifest:
    """能力清单（本模块能做什么/不能做什么）."""
    supported_formats: list = field(default_factory=lambda: ["docx"])
    op_types: list = field(default_factory=lambda: [
        "inspect", "replace", "insert", "delete", "format", "move",
        "tracked_change", "outline", "table_to_text", "text_to_table",
        "table_to_outline", "outline_to_table",
    ])
    rejected: list = field(default_factory=lambda: [
        "comment", "content_control", "field", "bookmark",
    ])


@dataclass
class IntermediateArtifact:
    """中间产物（分块/画像/召回）."""
    kind: str = ""            # chunk / portrait / recall
    ref: str = ""
    payload: dict = field(default_factory=dict)

    def __str__(self) -> str:
        """**向后兼容**：老代码把 `_portrait(...)` 当 str 用，`str(art)` 返回正文。"""
        return str((self.payload or {}).get("text") or "")


@dataclass
class RedactionPolicy:
    """脱敏策略（外发前要抹掉什么）."""
    mask_fields: list = field(default_factory=lambda: ["api_key", "authorization"])
    allow_external: bool = False


@dataclass
class ConfirmationOverlay:
    """确认层（何时必须弹窗）."""
    level: str = "silent"     # silent / light / modal
    reason: str = ""
    user_actions: list = field(default_factory=list)
