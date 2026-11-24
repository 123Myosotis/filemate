# -*- coding: utf-8 -*-
"""格式适配器协议：只描述"某个格式怎么扫描/计划/执行/校验"。

公共管线（runner）负责选文件、确认、快照、回滚、证据；
适配器不碰这些，只做本格式的事。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class DocumentAdapter(Protocol):
    tool: str                    # 业务线名，如 "text_office"
    exts: set[str]               # 本模块认领的扩展名
    readonly_ops: set[str]       # 只读 op：不进事务
    dangerous_ops: set[str]      # 危险 op：未确认时必须弹窗
    allowed_ops: set[str]        # 能力白名单
    no_target_message: str       # 没选中可处理文件时的提示

    def scan(self, path) -> dict:
        """读取文件状态（统计/结构/编码），不修改文件。"""
        ...

    def state_detail(self, state: dict) -> str:
        """扫描结果的一行摘要，写进 stages。"""
        ...

    def plan(self, user_input: str, state: dict) -> list[dict]:
        """用户指令 → op 列表；不确定时返回 inspect。"""
        ...

    def is_low_confidence(self, user_input: str, ops: list[dict]) -> bool:
        """是否属于"没听懂"的兜底计划。"""
        ...

    def execute(self, path, ops: list[dict], state: dict) -> dict:
        """真正读写文件；返回 result dict。"""
        ...

    def verify(self, path, ops: list[dict], state: dict) -> dict:
        """写后回读校验；返回 {ok, reason}。"""
        ...
