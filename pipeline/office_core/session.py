# -*- coding: utf-8 -*-
"""即时记忆：只存"最近一次任务"，供多轮/追问使用（进程内，不落盘）。"""
from __future__ import annotations

import time

_LAST: dict = {}


def remember(event: dict) -> None:
    global _LAST
    _LAST = dict(event or {})
    _LAST.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))


def last() -> dict:
    return dict(_LAST) if _LAST else {}


def clear() -> None:
    global _LAST
    _LAST = {}
