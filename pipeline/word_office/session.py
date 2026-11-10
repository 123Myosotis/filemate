# -*- coding: utf-8 -*-
"""即时记忆（阶段 4.4）：**当前任务**的内存上下文（进程内，不落盘）。

只存"最近一次任务"，供多轮/追问（"刚才那个"）使用。
任务结束/进程退出即丢——这是"即时"的定义（与后台记忆区分）。
"""
from __future__ import annotations

import time

_LAST: dict = {}


def remember(event: dict) -> None:
    """记住最近一次任务（覆盖）。"""
    global _LAST
    _LAST = dict(event or {})
    _LAST.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))


def last() -> dict:
    """取最近一次任务；没有则空 dict。"""
    return dict(_LAST) if _LAST else {}


def clear() -> None:
    global _LAST
    _LAST = {}
