# -*- coding: utf-8 -*-
"""任务链（阶段 5）：**线性**执行多个子任务（单动作不走链）。

原则（`项目架构.md` §2）：
- 单子任务 → 直接 `WordModule.handle`（不走链）。
- 多子任务 → **线性** t1 → t2 → t3；**任一失败即停**（失败整批拒绝）。
- **不做** DAG（并行/分支）——等实测需要再加。
"""
from __future__ import annotations

from .module import WordModule


def run_chain(tasks: list[dict]) -> dict:
    """线性执行子任务列表。每个 task 就是 WordModule.handle 的 request。"""
    tasks = [t for t in (tasks or []) if isinstance(t, dict)]
    if not tasks:
        return {"status": "failed", "reason": "empty_chain",
                "message": "没有子任务。", "results": []}
    if len(tasks) == 1:
        return WordModule.handle(tasks[0])

    results = []
    for i, task in enumerate(tasks):
        r = WordModule.handle(task)
        results.append({"i": i + 1, "status": r.get("status"),
                        "message": r.get("message")})
        if r.get("status") != "done":
            return {"status": "failed", "reason": "chain_stopped",
                    "message": "第 %d 个子任务失败：%s" % (i + 1, r.get("message")),
                    "results": results}
    return {"status": "done", "reason": "chain",
            "message": "已完成 %d 个子任务。" % len(tasks),
            "results": results}
