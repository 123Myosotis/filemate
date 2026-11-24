# -*- coding: utf-8 -*-
"""任务链（阶段 5）：**线性**执行多个子任务（单动作不走链）。

原则（`docs/architecture.md` §2）：
- 单子任务 → 直接 `WordModule.handle`（不走链）。
- 多子任务 → **线性** t1 → t2 → t3；**任一失败即停**（失败整批拒绝）。
- **P3**：已成功的写任务按逆序还原快照；还原失败 → `compensation_failed`。
- **不做** DAG（并行/分支）——等实测需要再加。
"""
from __future__ import annotations

from .module import WordModule
from .txn import restore_file


def run_chain(tasks: list[dict]) -> dict:
    """线性执行子任务列表。每个 task 就是 WordModule.handle 的 request。"""
    tasks = [t for t in (tasks or []) if isinstance(t, dict)]
    if not tasks:
        return {"status": "failed", "reason": "empty_chain",
                "message": "没有子任务。", "results": []}
    if len(tasks) == 1:
        return WordModule.handle(tasks[0])

    results = []
    committed = []
    for i, task in enumerate(tasks):
        r = WordModule.handle(task)
        results.append({"i": i + 1, "status": r.get("status"),
                        "message": r.get("message"),
                        "reason": r.get("reason")})
        if r.get("status") != "done":
            comp = _compensate(committed)
            if not comp["ok"]:
                return {"status": "compensation_failed",
                        "reason": "compensation_failed",
                        "message": "第 %d 个子任务失败后，逆序还原也失败：%s"
                        % (i + 1, comp["error"]),
                        "results": results}
            return {"status": "failed", "reason": "chain_stopped",
                    "message": "第 %d 个子任务失败：%s（已还原此前写入）"
                    % (i + 1, r.get("message")),
                    "results": results, "txn": {"phase": "compensated"}}
        ps = r.get("patch_set") or {}
        if ps.get("backup") and ps.get("path") and int(ps.get("applied") or 0) > 0:
            committed.append({"path": ps["path"], "backup": ps["backup"]})
    return {"status": "done", "reason": "chain",
            "message": "已完成 %d 个子任务。" % len(tasks),
            "results": results}


def _compensate(committed: list) -> dict:
    errors = []
    for item in reversed(committed or []):
        try:
            restore_file(item["backup"], item["path"])
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s" % (item.get("path"), exc))
    if errors:
        return {"ok": False, "error": "；".join(errors)}
    return {"ok": True, "error": ""}
