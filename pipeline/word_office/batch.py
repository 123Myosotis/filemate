# -*- coding: utf-8 -*-
"""多文件批处理（阶段 6）：**串行** + **单文件失败不中断**（记录继续）。"""
from __future__ import annotations

from .module import WordModule


def run_batch(files: list[str], user_input: str) -> dict:
    files = [str(f) for f in (files or []) if str(f).strip()]
    if not files:
        return {"status": "failed", "reason": "no_files",
                "message": "没有文件。", "results": []}
    results = []
    for f in files:
        r = WordModule.handle({"user_input": user_input, "selected_files": [f]})
        results.append({"file": f, "status": r.get("status"),
                        "message": r.get("message")})
    ok = sum(1 for x in results if x["status"] == "done")
    return {"status": "done" if ok == len(results) else "partially_completed",
            "reason": "batch", "message": "%d/%d 成功" % (ok, len(results)),
            "results": results}
