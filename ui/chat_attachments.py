# -*- coding: utf-8 -*-
"""对话附件：路径规范化 / 合并。接入层只提供目标文件，不猜意图。"""
from __future__ import annotations

from pathlib import Path


def paths_from_urls(urls) -> list[str]:
    """Qt QUrl 列表 → 本地已存在的文件路径（去重、保序）。目录忽略。"""
    out: list[str] = []
    seen: set[str] = set()
    for u in urls or []:
        try:
            local = u.toLocalFile() if hasattr(u, "toLocalFile") else str(u)
        except Exception:  # noqa: BLE001
            continue
        local = str(local or "").strip()
        if not local:
            continue
        p = Path(local)
        try:
            if not p.is_file():
                continue
            key = str(p.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def paths_from_strings(paths) -> list[str]:
    """字符串路径列表 → 存在的文件（去重）。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in paths or []:
        p = Path(str(raw or "").strip())
        if not str(p):
            continue
        try:
            if not p.is_file():
                continue
            key = str(p.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def merge_file_paths(attachments, selected) -> list[str]:
    """附件优先，再补资源管理器选中；去重保序。"""
    return paths_from_strings(list(attachments or []) + list(selected or []))


def format_attach_note(paths, *, limit: int = 5) -> str:
    """用户气泡里附加的短说明。"""
    items = list(paths or [])
    if not items:
        return ""
    names = [Path(p).name for p in items[:limit]]
    extra = len(items) - len(names)
    note = "、".join(names)
    if extra > 0:
        note += " 等 %d 个" % len(items)
    return "📎 " + note
