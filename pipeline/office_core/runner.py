# -*- coding: utf-8 -*-
"""公共执行管线：选文件 → 扫描 → 计划 → 确认 → 预检 → 快照 → 执行 → 校验 → 证据。

Word 模块暂不接入（保持现状），文本/PDF 等新模块直接复用这里。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from . import session, txn
from .evidence import write_evidence
from .result import fail, finish


def _selected_targets(adapter, request: dict) -> list[str]:
    """只保留本模块认领的扩展名，避免把 Excel/图片误交给文本模块。"""
    selected = [str(p) for p in (request.get("selected_files") or []) if str(p).strip()]
    return [p for p in selected if Path(p).suffix.lower() in adapter.exts]


def _patch_set(path: Path, ops: list[dict], backup: str, applied: int) -> dict:
    return {"ops": ops, "backup": backup, "applied": applied, "path": str(path)}


def run(adapter, request: dict) -> dict:
    """通用入口；任何意外都收敛成结构化失败，不把异常抛给 UI。"""
    stages: list = []
    warnings: list = []
    try:
        return _run(adapter, request, stages, warnings)
    except Exception as exc:  # noqa: BLE001
        warnings.append("unexpected_error")
        return finish(
            fail("failed", "这个文件处理时出错了，原文件没有被改动。可以关掉占用它的程序后重试。"),
            stages, warnings)


def _run(adapter, request: dict, stages: list, warnings: list) -> dict:
    text = str(request.get("user_input") or "")
    confirmed = bool(request.get("confirmed"))

    targets = _selected_targets(adapter, request)
    if not targets:
        return finish(fail("no_target", adapter.no_target_message), stages, warnings)

    if len(targets) > 1:
        named = [p for p in targets
                 if Path(p).name in text or Path(p).stem in text]
        if len(named) == 1:
            targets = named
        else:
            planned = adapter.plan(text, {}) or []
            writes = any(o.get("op") not in adapter.readonly_ops for o in planned)
            if writes:
                names = "、".join(Path(p).name for p in targets[:8])
                return finish({
                    "status": "needs_confirmation",
                    "reason": "multiple_candidates",
                    "message": (
                        "你选中了 %d 个文件（%s），我不确定要改哪一个。"
                        "请只保留一个附件，或在指令里写明文件名。"
                        % (len(targets), names)),
                    "user_actions": [],
                    "plan_ref": None,
                    "audit_ref": None,
                }, stages, warnings)

    target = Path(targets[0])
    if not target.is_file():
        return finish(fail("not_found", "选中的文件不存在：%s" % target.name), stages, warnings)

    # 真 dry-run：在临时副本上跑，原文件一个字节都不碰
    dry = bool(request.get("dry_run"))
    work = target
    tmp = None
    if dry:
        tmp = target.with_name(target.stem + ".dryrun" + target.suffix)
        shutil.copy2(target, tmp)
        work = tmp

    try:
        state = adapter.scan(work)
        stages.append({"name": "scan", "status": "ok",
                       "detail": adapter.state_detail(state)})

        ops = adapter.plan(text, state) or []
        if not ops:
            return finish(fail("no_plan", "我没听懂要做什么，请说得具体一点，例如「把 A 替换成 B」。"),
                          stages, warnings)

        writes = any(str(o.get("op") or "") not in adapter.readonly_ops for o in ops)

        if adapter.is_low_confidence(text, ops) and not confirmed:
            return finish({
                "status": "needs_confirmation",
                "reason": "low_confidence",
                "message": "我没太确定你要做什么。请确认后我再执行。",
                "user_actions": [{"label": "确认按「查看信息」执行", "action": "confirm"}],
                "plan_ref": None,
                "audit_ref": None,
            }, stages, warnings)

        if any(str(o.get("op") or "") in adapter.dangerous_ops for o in ops) and not confirmed:
            return finish({
                "status": "needs_confirmation",
                "reason": "dangerous_op",
                "message": "删除内容属于危险操作，请确认后再执行。",
                "user_actions": [{"label": "确认删除", "action": "confirm_delete"}],
                "plan_ref": None,
                "audit_ref": None,
            }, stages, warnings)

        unknown = [str(o.get("op") or "") for o in ops
                   if str(o.get("op") or "") not in adapter.allowed_ops]
        if unknown:
            return finish(fail("pre_check", "暂不支持这些操作：%s" % "、".join(unknown)),
                          stages, warnings)

        snap: txn.Snapshot | None = None
        if writes and not dry:
            try:
                snap = txn.begin(adapter.tool, work)
            except Exception as exc:  # noqa: BLE001
                return finish(fail("pre_check", "无法写前备份，已拒绝执行：%s" % exc),
                              stages, warnings)

        try:
            result = adapter.execute(work, ops, state)
        except Exception:  # noqa: BLE001
            # 执行层的意外异常也要走快照还原，不能把半成品留在磁盘上
            warnings.append("execute_error")
            note = ("原文件会从快照还原。" if snap is not None
                    else "原文件没有改动。")
            result = fail("failed", "这个文件处理时出错了，%s" % note)
        ok = str(result.get("status") or "") == "done"

        if ok:
            try:
                verify = adapter.verify(work, ops, state) or {}
            except Exception:  # noqa: BLE001
                warnings.append("verify_error")
                verify = {"ok": False, "reason": "回读校验失败"}
            if not verify.get("ok", True):
                result = fail("verify_failed",
                              "写入后校验没通过：%s" % verify.get("reason", ""))
                ok = False

        if snap is not None and (not ok or request.get("rollback")):
            restored, err = txn.try_restore(snap)
            if not restored:
                result = fail(
                    "rollback_failed",
                    "写入失败且还原快照失败（%s）。原文件可能不一致，请从 %s 手工恢复。"
                    % (err or "", snap.archive))
            elif request.get("rollback"):
                result["rollback"] = True
                result["applied"] = 0
                result.setdefault("txn", {})["phase"] = "aborted"
            else:
                result.setdefault("txn", {})["phase"] = "aborted"
                msg = str(result.get("message") or "")
                if "已还原" not in msg and "已恢复" not in msg:
                    result["message"] = (msg + "（已从快照整包还原）").strip()
        elif dry:
            result["dry_run"] = True
            result["applied"] = 0
            result.setdefault("txn", {})["phase"] = "dry"
        elif snap is not None:
            result.setdefault("txn", {})["phase"] = "committed"

        applied = int(result.get("applied") or 0)
        backup_path = str(snap.archive) if snap is not None else (
            str(target) + ".bak" if target.with_suffix(target.suffix + ".bak").is_file() else "")
        result["patch_set"] = _patch_set(target, ops, backup_path, applied)

        # 写操作成功时把备份位置补进提示，方便用户回退
        if ok and writes and not dry and snap is not None:
            msg = str(result.get("message") or "")
            note = "写前备份：%s" % snap.archive.name
            if note not in msg:
                result["message"] = (msg + "（%s）" % note).strip()

        try:
            write_evidence(adapter.tool, {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "tool": adapter.tool,
                "user_input": text,
                "file": {"path": str(target)},
                "before": {k: state.get(k) for k in ("encoding", "lines", "chars")},
                "ops": len(ops),
                "applied": applied,
                "status": result.get("status"),
                "reason": result.get("reason"),
            })
        except Exception:  # noqa: BLE001
            warnings.append("evidence_failed")

        try:
            session.remember({"tool": adapter.tool, "file": str(target),
                              "user_input": text, "status": result.get("status")})
        except Exception:  # noqa: BLE001
            pass

        return finish(result, stages, warnings)
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
