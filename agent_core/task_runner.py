# agent_core/task_runner.py
"""
任务调度层：DAG 执行 + 硬门禁回滚重跑。

- 依赖串行 / 独立并行 / 失败不阻塞聚合任务
- 复核 action=retry：原子回滚交付物，清空 rollback_caps 对应任务，带 validation_feedback 重跑
- 最多 review.rounds 轮；仍失败则 passed=False，不假装成功
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import config

from agent_core.capabilities import CAPABILITIES


def run_tasks(tasks: list, ctx: dict) -> dict:
    tasks = {t["task_id"]: t for t in tasks}
    results, skipped = {}, set()
    review_rounds = int(config.get_param("review.rounds"))
    review_result, rounds_used = None, 0
    control = ctx.get("control")

    for round_no in range(review_rounds + 1):
        if control and not control.checkpoint():
            _report(ctx, "⏹ 任务已结束/取消")
            break
        if round_no > 0:
            retry_ids = _collect_retry_ids(tasks, results, ctx)
            if not retry_ids:
                break
            rounds_used += 1
            _report(ctx, f"🔁 回滚重跑第 {rounds_used} 轮：{len(retry_ids)} 个任务")
            fb = ctx.get("data", {}).get("validation_feedback") or []
            if fb:
                ctx["data"]["regen_hint"] = (
                    "上一轮验证失败，必须修正后重生成。错误：\n- "
                    + "\n- ".join(fb[:8])
                )
            for tid in retry_ids:
                results.pop(tid, None)
            for tid in list(skipped):
                results.pop(tid, None)
            skipped.clear()

        _run_dag(tasks, results, skipped, ctx)
        if control and control.cancelled():
            _report(ctx, "⏹ 任务已结束/取消")
            break

        review_result = (ctx.get("data") or {}).get("review")
        if not isinstance(review_result, dict):
            for tid, t in tasks.items():
                if t["capability"] == "review" and tid in results:
                    out = results[tid].get("output")
                    if isinstance(out, dict) and "action" in out:
                        review_result = out
                        break

        if not isinstance(review_result, dict):
            break
        if review_result.get("action") != "retry":
            break
        if not config.get_param("review.hard_gate"):
            break

    failed = [tid for tid, r in results.items()
              if not r["ok"] and r["status"] == "failed"]
    overall_ok = True
    if control and control.cancelled():
        overall_ok = False
    elif (isinstance(review_result, dict) and config.get_param("review.hard_gate")
            and review_result.get("action") == "retry"):
        overall_ok = False
    return {"ok": overall_ok, "results": results, "failed": failed,
            "skipped": sorted(skipped), "review": review_result,
            "rounds": rounds_used,
            "cancelled": bool(control and control.cancelled())}


def _collect_retry_ids(tasks: dict, results: dict, ctx: dict) -> list:
    caps = set(config.get_param("review.rollback_caps") or [])
    needs = bool((ctx.get("data") or {}).get("needs_regen"))
    ids = []
    for tid, r in results.items():
        cap = tasks[tid]["capability"]
        if cap == "review":
            ids.append(tid)
            continue
        if not r.get("ok") and r.get("status") == "failed" and r.get("retryable", True):
            ids.append(tid)
            continue
        if needs and cap in caps:
            ids.append(tid)
    out, seen = [], set()
    for tid in ids:
        if tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def _run_dag(tasks: dict, results: dict, skipped: set, ctx: dict):
    pending = [tid for tid in tasks if tid not in results]
    guard = 0
    while pending and guard <= len(tasks) + 1:
        guard += 1
        ready = [tid for tid in pending
                 if all(d in results or d in skipped for d in tasks[tid]["dependencies"])]
        if not ready:
            for tid in pending:
                reason = ("前置任务被跳过" if any(d in skipped for d in tasks[tid]["dependencies"])
                          else "依赖关系成环，无法调度")
                _skip(tid, tasks, results, skipped, reason, ctx)
            break

        serial = [tid for tid in ready if not tasks[tid]["parallel_possible"]]
        parallel = [tid for tid in ready if tasks[tid]["parallel_possible"]]

        for tid in serial:
            _run_one(tid, tasks, results, skipped, ctx)
        if parallel:
            workers = min(len(parallel), int(config.get_param("runner.max_workers")))
            with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
                list(pool.map(lambda tid: _run_one(tid, tasks, results, skipped, ctx),
                              parallel))
        pending = [tid for tid in tasks if tid not in results]


def _run_one(tid: str, tasks: dict, results: dict, skipped: set, ctx: dict):
    task = tasks[tid]
    control = ctx.get("control")
    if control and not control.checkpoint():
        results[tid] = {"ok": False, "output": None, "status": "cancelled",
                        "error": "用户结束任务", "retryable": False}
        skipped.add(tid)
        return
    if any(d in skipped for d in task["dependencies"]):
        _skip(tid, tasks, results, skipped, "前置任务被跳过", ctx)
        return
    fn = CAPABILITIES.get(task["capability"])
    if fn is None:
        results[tid] = {"ok": False, "output": None, "status": "failed",
                        "error": f"未注册的能力：{task['capability']}",
                        "retryable": False}
        return
    retries = int(config.get_param("runner.task_retries"))
    last = None
    for attempt in range(retries + 1):
        if control and not control.checkpoint():
            last = {"ok": False, "output": None, "error": "用户结束任务",
                    "retryable": False}
            break
        try:
            last = fn(ctx, task)
            break
        except Exception as e:
            try:
                from ai.client import CallCancelled
            except Exception:
                CallCancelled = ()
            if isinstance(e, CallCancelled):
                last = {"ok": False, "output": None, "error": "用户结束任务",
                        "retryable": False}
                break
            last = {"ok": False, "output": None,
                    "error": f"任务执行异常（第{attempt + 1}次）：{e}",
                    "retryable": True}
            if attempt < retries:
                _report(ctx, f"⚠️ 任务 {tid} 异常，重试 {attempt + 1}/{retries}")
    # 成功步落后盘：供暂停后续跑 / 会话丢失恢复
    try:
        from agent_core import session_state
        data = ctx.get("data") or {}
        if last and last.get("ok"):
            tables_now = data.get("tables") or {}
            if tables_now:
                session_state.save_tables_blob(
                    Path(ctx.get("work_dir") or "."), tables_now,
                    channel_id=ctx.get("channel_id"))
            session_state.save_checkpoint(
                Path(ctx.get("work_dir") or "."),
                {
                    "user_input": ctx.get("user_input") or "",
                    "last_task": tid,
                    "capability": task.get("capability"),
                    "tables": session_state.snapshot_tables(data.get("tables") or {}),
                    "table_result": {
                        "columns": list((data.get("table_result") or {}).get("columns") or []),
                        "rows": len((data.get("table_result") or {}).get("rows") or []),
                        "name": (data.get("table_result") or {}).get("name") or "",
                    },
                    "files": list(data.get("files") or [])[:200],
                    "filter_notes": list(data.get("filter_notes") or [])[-40:],
                },
                channel_id=ctx.get("channel_id"),
            )
    except Exception:
        pass
    results[tid] = {
        "ok": bool(last.get("ok")),
        "output": last.get("output"),
        "error": last.get("error") or "",
        "status": ("cancelled" if (last or {}).get("error") == "用户结束任务"
                   else ("done" if last.get("ok") else "failed")),
        "retryable": last.get("retryable", True),
    }
    if not last.get("ok"):
        _report(ctx, f"⚠️ 任务「{task['task_name']}」失败：{(last.get('error') or '')[:80]}")


def _skip(tid, tasks, results, skipped, reason, ctx=None):
    skipped.add(tid)
    results[tid] = {"ok": False, "output": None, "status": "skipped",
                    "error": reason, "retryable": False}
    if ctx is not None:
        _report(ctx, f"⏭ 跳过 {tasks[tid].get('task_name') or tid}：{reason}")


def _report(ctx, msg):
    fn = ctx.get("report")
    if fn:
        fn(msg)
