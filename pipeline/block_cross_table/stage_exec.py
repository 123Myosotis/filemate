# -*- coding: utf-8 -*-
"""S 段：按 StagePlanSchema 串行执行阶段；E 经最小桩交接。

不调 A'/A/B；不写画像；不内部模拟问用户。
回滚：② S 直接执行；①/③ 只向上报告。
阶段 AI 结构无效：先重试（代位 plan_regenerate_max），用尽后再走门闸。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.conflict_e import validate_e_package
from pipeline.block_cross_table.stage_exec_trace import KIND_STAGE_EXEC, StageExecTracer
from pipeline.block_cross_table.stage_plan import (
    ROLLBACK_OWNERSHIP,
    ROLLBACK_STAGE,
    STAGE_ROLLBACK_POINTS,
    read_plan_regenerate_max,
)
from pipeline.block_cross_table.stage_plan_trace import require_run_id


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _norm_block_ids(raw: Sequence[Any]) -> list[str]:
    return [str(x) for x in raw]


def resolve_input_block_ids(
    stage: Mapping[str, Any],
    *,
    actual_touched: Optional[Sequence[Any]] = None,
) -> list[str]:
    """Q2：默认=计划 block_ids；实际触达须非空且 ⊆ 计划集。"""
    planned = stage.get("block_ids")
    if not isinstance(planned, list) or not planned:
        raise CrossTableError(
            "cross_table_input_invalid",
            "stage block_ids missing",
        )
    planned_norm = _norm_block_ids(planned)
    planned_set = set(planned_norm)
    if actual_touched is None:
        return list(planned_norm)
    actual_norm = _norm_block_ids(actual_touched)
    if not actual_norm:
        raise CrossTableError(
            "cross_table_input_invalid",
            "actual_touched empty",
        )
    for bid in actual_norm:
        if bid not in planned_set:
            raise CrossTableError(
                "cross_table_input_invalid",
                "actual_touched not subset of plan block_ids",
                {"block_id": bid},
            )
    return actual_norm


def gate_allows_e(allow_user_ask: Any, needs_user_ask: Any) -> bool:
    """S 触 E 充要：allow_user_ask AND needs_user_ask。"""
    return allow_user_ask is True and needs_user_ask is True


def _stage_locators(stage: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("block_ids", "subtable_ids", "layer_ids", "dimension_ids"):
        vals = stage.get(key) or []
        if isinstance(vals, list):
            out.extend("%s:%s" % (key, x) for x in vals)
    return out


def _is_ai_structure_ok(raw: Any, allowed_kinds: Sequence[str]) -> tuple[bool, str]:
    if not isinstance(raw, Mapping):
        return False, "ai_response_not_object"
    if raw.get("ok") is not True:
        return False, str(raw.get("reason") or "ai_ok_false")
    kind = raw.get("decision_kind")
    if kind is not None and str(kind) not in set(allowed_kinds):
        return False, "decision_kind_not_allowed:%s" % kind
    if "artifact" not in raw:
        return False, "artifact_missing"
    return True, ""


def _apply_rollback(
    *,
    stage: Mapping[str, Any],
    stage_products: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """按 rollback_point 分支：② S 直接执行；①/③ 向上报告。"""
    rb = stage.get("rollback_point")
    sid = str(stage.get("stage_id") or "")
    if rb not in STAGE_ROLLBACK_POINTS:
        raise CrossTableError(
            "cross_table_input_invalid",
            "rollback_point invalid for stage exec",
            {"rollback_point": rb},
        )
    if rb == ROLLBACK_STAGE:
        # ②：S 直接丢本阶段产物
        discarded = stage_products.pop(sid, None)
        return {
            "action": "s_execute",
            "rollback_point": rb,
            "stage_id": sid,
            "discarded": discarded is not None,
            "reason": reason,
            "executor": "S",
        }
    # ① / ③：S 不执行重做，只向上报告
    return {
        "action": "escalate_report",
        "rollback_point": rb,
        "stage_id": sid,
        "reason": reason,
        "executor": "upstream",
        "note": (
            "S does not redo a_prime_recognition or upstream_layout; "
            "I/A-prime or layout owner must execute."
        ),
    }


def _call_e(
    e_handler: Any,
    package: Mapping[str, Any],
) -> dict[str, Any]:
    validate_e_package(package)
    if hasattr(e_handler, "handle"):
        return dict(e_handler.handle(package))
    if callable(e_handler):
        out = e_handler(package)
        if not isinstance(out, Mapping):
            raise CrossTableError(
                "cross_table_input_invalid",
                "e_handler must return mapping",
            )
        return dict(out)
    raise CrossTableError(
        "cross_table_contract_invalid",
        "e_handler missing",
    )


def run_stage_exec(
    *,
    run_id: Any,
    plan: Mapping[str, Any],
    chat: Callable[[str], Any],
    e_handler: Any,
    plan_regenerate_max: Any = None,
    clock: Optional[Callable[[], str]] = None,
    contract_data: Optional[Mapping[str, Any]] = None,
    project_fn: Optional[Callable[[Mapping[str, Any]], Any]] = None,
    actual_touched_by_stage: Optional[Mapping[str, Sequence[Any]]] = None,
) -> dict[str, Any]:
    """S 主入口。成功返回投影；失败返回回滚报告或 E 交接结果。"""
    rid = require_run_id(run_id)
    _clock = clock or _default_clock
    if plan_regenerate_max is None:
        regen_max = read_plan_regenerate_max(contract_data)
    else:
        from pipeline.block_cross_table.stage_plan import require_plan_regenerate_max

        regen_max = require_plan_regenerate_max(plan_regenerate_max)

    stages = plan.get("stages")
    if not isinstance(stages, list) or not stages:
        raise CrossTableError(
            "cross_table_input_invalid",
            "plan stages missing",
        )

    review_result = plan.get("review_result")
    plan_needs_user_ask: Any = None
    if isinstance(review_result, Mapping):
        plan_needs_user_ask = review_result.get("needs_user_ask")

    stage_products: dict[str, Any] = {}
    trace_paths: list[str] = []
    e_results: list[dict[str, Any]] = []
    rollback_reports: list[dict[str, Any]] = []

    for stage in stages:
        if not isinstance(stage, Mapping):
            raise CrossTableError(
                "cross_table_input_invalid",
                "stage not object",
            )
        sid = stage.get("stage_id")
        if not isinstance(sid, str) or sid == "":
            raise CrossTableError(
                "cross_table_input_invalid",
                "stage_id missing",
            )
        if "allow_user_ask" not in stage:
            pkg = {
                "kind": "missing_field",
                "missing_field": "allow_user_ask",
                "review_raw": None,
                "stage_id": sid,
                "locator_ids": _stage_locators(stage),
            }
            e_out = _call_e(e_handler, pkg)
            e_results.append({"package": pkg, "e_response": e_out})
            rb = _apply_rollback(
                stage=stage, stage_products=stage_products, reason="allow_user_ask_missing"
            )
            rollback_reports.append(rb)
            return {
                "ok": False,
                "status": "structure_invalid_missing_field",
                "stage_id": sid,
                "e_results": e_results,
                "rollback": rb,
                "trace_paths": trace_paths,
            }

        allow_user_ask = stage.get("allow_user_ask")
        allowed_kinds = stage.get("allowed_decision_kinds") or []
        if not isinstance(allowed_kinds, list) or not allowed_kinds:
            raise CrossTableError(
                "cross_table_input_invalid",
                "allowed_decision_kinds missing",
                {"stage_id": sid},
            )

        tracer = StageExecTracer(rid, sid, _clock)
        touched = None
        if actual_touched_by_stage and sid in actual_touched_by_stage:
            touched = actual_touched_by_stage[sid]
        input_blocks = resolve_input_block_ids(stage, actual_touched=touched)

        # --- 阶段 AI：重试阶段（用尽前不触 E）---
        last_reason = "ai_not_attempted"
        attempts = 0
        ai_raw: Any = None
        while attempts < regen_max:
            attempts += 1
            prompt = (
                "S stage AI. Reply JSON with ok, artifact, optional decision_kind. "
                + json.dumps(
                    {
                        "stage_id": sid,
                        "allowed_decision_kinds": list(allowed_kinds),
                        "attempt": attempts,
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
            ai_raw = chat(prompt)
            tracer.write(
                KIND_STAGE_EXEC,
                {"attempt": attempts, "phase": "stage_ai"},
                input_block_ids=input_blocks,
            )
            trace_paths.extend(tracer.paths[-1:])
            ok, reason = _is_ai_structure_ok(ai_raw, allowed_kinds)
            if ok:
                last_reason = ""
                break
            last_reason = reason
            # 单次无效：继续重试，不触 E

        if last_reason:
            # 用尽后：门闸
            needs = plan_needs_user_ask
            if needs is None:
                pkg = {
                    "kind": "missing_field",
                    "missing_field": "needs_user_ask",
                    "review_raw": review_result,
                    "stage_id": sid,
                    "locator_ids": _stage_locators(stage),
                }
                e_out = _call_e(e_handler, pkg)
                e_results.append({"package": pkg, "e_response": e_out})
                rb = _apply_rollback(
                    stage=stage,
                    stage_products=stage_products,
                    reason="needs_user_ask_missing_after_ai_exhausted",
                )
                rollback_reports.append(rb)
                return {
                    "ok": False,
                    "status": "structure_invalid_missing_field",
                    "stage_id": sid,
                    "attempts": attempts,
                    "e_results": e_results,
                    "rollback": rb,
                    "trace_paths": trace_paths,
                }
            if gate_allows_e(allow_user_ask, needs):
                pkg = {
                    "kind": "stage_ai_structure_exhausted",
                    "invalid_reason": last_reason,
                    "stage_id": sid,
                    "locator_ids": _stage_locators(stage),
                    "attempts": attempts,
                }
                e_out = _call_e(e_handler, pkg)
                e_results.append({"package": pkg, "e_response": e_out})
                return {
                    "ok": False,
                    "status": "hand_off_e",
                    "stage_id": sid,
                    "attempts": attempts,
                    "e_results": e_results,
                    "trace_paths": trace_paths,
                }
            rb = _apply_rollback(
                stage=stage,
                stage_products=stage_products,
                reason="ai_exhausted_gate_denied",
            )
            rollback_reports.append(rb)
            return {
                "ok": False,
                "status": "structure_failed_rollback",
                "stage_id": sid,
                "attempts": attempts,
                "rollback": rb,
                "trace_paths": trace_paths,
            }

        assert isinstance(ai_raw, Mapping)
        artifact = ai_raw.get("artifact")

        # --- 阶段复核 ---
        review_prompt = (
            "S stage review. Reply JSON with passed, reason, needs_user_ask. "
            + json.dumps(
                {"stage_id": sid, "artifact": artifact},
                ensure_ascii=False,
                default=str,
            )
        )
        review_raw = chat(review_prompt)
        tracer.write(
            KIND_STAGE_EXEC,
            {"phase": "stage_review"},
            input_block_ids=input_blocks,
        )
        trace_paths.append(tracer.paths[-1])

        if not isinstance(review_raw, Mapping):
            pkg = {
                "kind": "missing_field",
                "missing_field": "needs_user_ask",
                "review_raw": review_raw,
                "stage_id": sid,
                "locator_ids": _stage_locators(stage),
            }
            e_out = _call_e(e_handler, pkg)
            e_results.append({"package": pkg, "e_response": e_out})
            rb = _apply_rollback(
                stage=stage, stage_products=stage_products, reason="review_not_object"
            )
            return {
                "ok": False,
                "status": "structure_invalid_missing_field",
                "e_results": e_results,
                "rollback": rb,
                "trace_paths": trace_paths,
            }

        if "needs_user_ask" not in review_raw:
            pkg = {
                "kind": "missing_field",
                "missing_field": "needs_user_ask",
                "review_raw": dict(review_raw),
                "stage_id": sid,
                "locator_ids": _stage_locators(stage),
            }
            e_out = _call_e(e_handler, pkg)
            e_results.append({"package": pkg, "e_response": e_out})
            rb = _apply_rollback(
                stage=stage,
                stage_products=stage_products,
                reason="needs_user_ask_missing",
            )
            return {
                "ok": False,
                "status": "structure_invalid_missing_field",
                "e_results": e_results,
                "rollback": rb,
                "trace_paths": trace_paths,
            }

        needs_user_ask = review_raw.get("needs_user_ask")
        if not isinstance(needs_user_ask, bool):
            pkg = {
                "kind": "missing_field",
                "missing_field": "needs_user_ask",
                "review_raw": dict(review_raw),
                "stage_id": sid,
                "locator_ids": _stage_locators(stage),
            }
            e_out = _call_e(e_handler, pkg)
            e_results.append({"package": pkg, "e_response": e_out})
            rb = _apply_rollback(
                stage=stage,
                stage_products=stage_products,
                reason="needs_user_ask_not_bool",
            )
            return {
                "ok": False,
                "status": "structure_invalid_missing_field",
                "e_results": e_results,
                "rollback": rb,
                "trace_paths": trace_paths,
            }

        if review_raw.get("passed") is True:
            stage_products[sid] = artifact
            continue

        # 复核未通过 → §2.3 门闸
        if gate_allows_e(allow_user_ask, needs_user_ask):
            pkg = {
                "kind": "review_failed_ask_user",
                "review_raw": dict(review_raw),
                "stage_id": sid,
                "locator_ids": _stage_locators(stage),
                "trigger": "review_failed_ask_user",
            }
            e_out = _call_e(e_handler, pkg)
            e_results.append({"package": pkg, "e_response": e_out})
            return {
                "ok": False,
                "status": "hand_off_e",
                "stage_id": sid,
                "e_results": e_results,
                "trace_paths": trace_paths,
            }

        rb = _apply_rollback(
            stage=stage,
            stage_products=stage_products,
            reason="review_failed_gate_denied",
        )
        rollback_reports.append(rb)
        if rb.get("action") == "s_execute":
            # ②：丢产物后重跑本阶段一次（仍受 regen_max 约束的新一轮 AI）
            # 为避免无限环，重跑失败则向上报告
            return {
                "ok": False,
                "status": "stage_restarted",
                "stage_id": sid,
                "rollback": rb,
                "trace_paths": trace_paths,
                "note": "caller may re-invoke run_stage_exec for this stage",
            }
        return {
            "ok": False,
            "status": "escalate_rollback",
            "stage_id": sid,
            "rollback": rb,
            "trace_paths": trace_paths,
        }

    # 全部阶段完成 → 投影
    snapshot = {"stage_products": dict(stage_products), "plan": plan}
    if project_fn is not None:
        projection = project_fn(snapshot)
    else:
        projection = {"ok": True, "stage_products": dict(stage_products)}
    if isinstance(projection, Mapping) and projection.get("ok") is False:
        # 断言失败 → 回滚到第一阶段起点同等恢复（报告）
        first = stages[0]
        rb = _apply_rollback(
            stage=first if isinstance(first, Mapping) else {"stage_id": "s0", "rollback_point": ROLLBACK_STAGE},
            stage_products=stage_products,
            reason="projection_assert_failed",
        )
        return {
            "ok": False,
            "status": "projection_failed",
            "rollback": rb,
            "projection": projection,
            "trace_paths": trace_paths,
        }
    return {
        "ok": True,
        "status": "ok",
        "projection": projection,
        "stage_products": dict(stage_products),
        "e_results": e_results,
        "trace_paths": trace_paths,
    }


def report_cross_block_ownership(
    *,
    stage: Mapping[str, Any],
    conflicts: Sequence[Mapping[str, Any]],
    e_handler: Any,
    actual_touched: Optional[Sequence[Any]] = None,
) -> dict[str, Any]:
    """S 发现④：组装包交 E；必须带 input_block_ids。"""
    input_blocks = resolve_input_block_ids(stage, actual_touched=actual_touched)
    pkg = {
        "kind": "cross_block_ownership",
        "rollback_point": ROLLBACK_OWNERSHIP,
        "input_block_ids": input_blocks,
        "conflicts": list(conflicts),
        "adjudication": None,
    }
    e_out = _call_e(e_handler, pkg)
    return {"package": pkg, "e_response": e_out}
