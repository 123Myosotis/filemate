# -*- coding: utf-8 -*-
"""B 段：只读画像 → 模型出阶段计划 → 本地一致性自检 → 复核 → 交付。

不调 A'；不写画像；不重推层。语义判断交模型。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.portrait_write import (
    PORTRAIT_STRUCTURE_STATUS_KEY,
    STRUCTURE_STATUS_LAYERS_RECOGNIZED,
    STRUCTURE_STATUS_UPSTREAM_ABSENT,
    b_should_read_layer_objects,
)
from pipeline.block_cross_table.stage_plan_trace import (
    KIND_GENERATE,
    KIND_REVIEW,
    StagePlanTracer,
    require_run_id,
)

# 图 4：阶段声明仅允许 ①–③；④ 只走跨块发现上报（§6），不得进计划
# 字面量与契约 enums.stage_rollback_point 对齐（v1.6.0+）
ROLLBACK_A_PRIME = "a_prime_recognition"
ROLLBACK_STAGE = "stage_start"
ROLLBACK_LAYOUT = "upstream_layout"
ROLLBACK_OWNERSHIP = "cross_block_ownership"
STAGE_ROLLBACK_POINTS = frozenset(
    {ROLLBACK_A_PRIME, ROLLBACK_STAGE, ROLLBACK_LAYOUT}
)
# 兼容旧名：全量四种含 ④（仅用于发现上报校验，不用于阶段声明）
ROLLBACK_POINTS = STAGE_ROLLBACK_POINTS | {ROLLBACK_OWNERSHIP}

PLAN_REGENERATE_MAX_KEY = "plan_regenerate_max"
STRUCTURE_STATUS_ENUM_KEY = "structure_status"
STAGE_ROLLBACK_ENUM_KEY = "stage_rollback_point"

# 与 StagePlanSchema.stage_item.required 对齐（v1.6.0）
_STAGE_REQUIRED = (
    "stage_id",
    "parent_table_ids",
    "block_ids",
    "row_range",
    "col_range",
    "subtable_ids",
    "dimension_ids",
    "layer_ids",
    "allowed_decision_kinds",
    "allow_user_ask",
    "rollback_point",
    "dependency_allows_prior",
)


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _cross_table(contract_data: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
    if contract_data is not None:
        block = contract_data.get("cross_table")
    else:
        from pipeline.synth_contract.registry import load_contract

        block = (load_contract().data or {}).get("cross_table")
    if not isinstance(block, Mapping):
        raise CrossTableError(
            "cross_table_contract_invalid",
            "cross_table block missing",
        )
    return block


def read_allowed_decision_kinds(
    contract_data: Optional[Mapping[str, Any]] = None,
) -> list[str]:
    """决策种类全集：契约 enums.orchestrate_decision_kind。"""
    enums = (_cross_table(contract_data).get("enums") or {})
    kinds = enums.get("orchestrate_decision_kind")
    if not isinstance(kinds, list) or not kinds:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "orchestrate_decision_kind missing",
        )
    return [str(x) for x in kinds]


def read_structure_status_enum(
    contract_data: Optional[Mapping[str, Any]] = None,
) -> frozenset[str]:
    """契约 enums.structure_status；缺键/空 → 契约无效类。"""
    enums = (_cross_table(contract_data).get("enums") or {})
    members = enums.get(STRUCTURE_STATUS_ENUM_KEY)
    if not isinstance(members, list) or not members:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "structure_status enum missing",
        )
    return frozenset(str(x) for x in members)


def read_stage_rollback_point_enum(
    contract_data: Optional[Mapping[str, Any]] = None,
) -> frozenset[str]:
    """契约 enums.stage_rollback_point（仅①–③）；缺键/空 → 契约无效类。"""
    enums = (_cross_table(contract_data).get("enums") or {})
    members = enums.get(STAGE_ROLLBACK_ENUM_KEY)
    if not isinstance(members, list) or not members:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "stage_rollback_point enum missing",
        )
    out = frozenset(str(x) for x in members)
    if ROLLBACK_OWNERSHIP in out:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "stage_rollback_point must not include ownership point",
        )
    return out


def require_plan_regenerate_max(plan_regenerate_max: Any) -> int:
    """校验已解析的上限值；缺/非法 → 契约无效类。"""
    if plan_regenerate_max is None:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "plan_regenerate_max missing",
        )
    if (
        isinstance(plan_regenerate_max, bool)
        or not isinstance(plan_regenerate_max, int)
        or plan_regenerate_max < 1
    ):
        raise CrossTableError(
            "cross_table_contract_invalid",
            "plan_regenerate_max invalid",
        )
    return int(plan_regenerate_max)


def read_plan_regenerate_max(
    contract_data: Optional[Mapping[str, Any]] = None,
) -> int:
    """从契约根能力键读取计划再生成上限；缺键/非法 → 契约无效类。"""
    block = _cross_table(contract_data)
    if PLAN_REGENERATE_MAX_KEY not in block:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "plan_regenerate_max missing",
        )
    return require_plan_regenerate_max(block[PLAN_REGENERATE_MAX_KEY])


def load_portraits_from_store(store: Any) -> dict[str, dict[str, Any]]:
    """从持久化 store 读全部已写切片。"""
    ids = list(store.written_block_ids())
    out: dict[str, dict[str, Any]] = {}
    for bid in ids:
        slice_data = store.get(bid)
        if isinstance(slice_data, dict):
            out[str(bid)] = dict(slice_data)
    return out


def build_portrait_index(
    portraits: Mapping[str, Mapping[str, Any]],
    *,
    contract_data: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """机械索引：块 / 层 / 子表 / 维度；供一致性校验。"""
    status_enum = read_structure_status_enum(contract_data)
    # 与契约两值语义对齐；实现常量须是契约子集
    if STRUCTURE_STATUS_UPSTREAM_ABSENT not in status_enum:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "structure_status missing upstream_absent",
        )
    if STRUCTURE_STATUS_LAYERS_RECOGNIZED not in status_enum:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "structure_status missing layers_recognized",
        )

    block_ids = set(portraits.keys())
    layers_by_block: dict[str, set[str]] = {}
    subtables_by_block: dict[str, set[str]] = {}
    dimensions_by_block: dict[str, set[str]] = {}
    recognized: set[str] = set()
    absent: set[str] = set()

    for bid, slice_data in portraits.items():
        status = slice_data.get(PORTRAIT_STRUCTURE_STATUS_KEY)
        if status not in status_enum:
            raise CrossTableError(
                "cross_table_input_invalid",
                "portrait structure_status unknown",
                {"block_id": bid, "structure_status": status},
            )
        if status == STRUCTURE_STATUS_UPSTREAM_ABSENT:
            absent.add(bid)
            layers_by_block[bid] = set()
            subtables_by_block[bid] = set()
            dimensions_by_block[bid] = set()
            continue
        if status != STRUCTURE_STATUS_LAYERS_RECOGNIZED:
            raise CrossTableError(
                "cross_table_input_invalid",
                "portrait structure_status unknown",
                {"block_id": bid, "structure_status": status},
            )
        recognized.add(bid)
        order = slice_data.get("layer_order") or []
        layers_by_block[bid] = {str(x) for x in order if x is not None and str(x) != ""}
        subs: set[str] = set()
        for item in slice_data.get("subtable_boundaries") or []:
            if isinstance(item, dict) and "subtable_id" in item:
                subs.add(str(item["subtable_id"]))
        subtables_by_block[bid] = subs
        dims: set[str] = set()
        for item in slice_data.get("dimension_membership") or []:
            if isinstance(item, dict) and item.get("dimension_field_id") is not None:
                dims.add(str(item["dimension_field_id"]))
        dimensions_by_block[bid] = dims

    return {
        "block_ids": block_ids,
        "recognized": recognized,
        "absent": absent,
        "layers_by_block": layers_by_block,
        "subtables_by_block": subtables_by_block,
        "dimensions_by_block": dimensions_by_block,
    }


def detect_cross_block_ownership_inconsistency(
    portraits: Mapping[str, Mapping[str, Any]],
) -> Optional[dict[str, Any]]:
    """机械发现：同维在多已识别块上的层集合不一致。只上报，不裁定对错。"""
    # dim -> list of (block_id, frozenset layer_ids)
    by_dim: dict[str, list[tuple[str, frozenset[str]]]] = {}
    for bid, slice_data in portraits.items():
        if not b_should_read_layer_objects(slice_data):
            continue
        for item in slice_data.get("dimension_membership") or []:
            if not isinstance(item, dict):
                continue
            dim = item.get("dimension_field_id")
            if dim is None or str(dim) == "":
                continue
            layers = item.get("layer_ids")
            if not isinstance(layers, list):
                continue
            # 空列表合法：frozenset()
            key = str(dim)
            by_dim.setdefault(key, []).append(
                (str(bid), frozenset(str(x) for x in layers))
            )

    conflicts: list[dict[str, Any]] = []
    for dim, entries in by_dim.items():
        if len(entries) < 2:
            continue
        sets = {fs for _bid, fs in entries}
        if len(sets) > 1:
            conflicts.append(
                {
                    "dimension_field_id": dim,
                    "blocks": [
                        {"block_id": bid, "layer_ids": sorted(fs)}
                        for bid, fs in entries
                    ],
                }
            )
    if not conflicts:
        return None
    return {
        "kind": "cross_block_ownership_inconsistency",
        "rollback_point": ROLLBACK_OWNERSHIP,
        "conflicts": conflicts,
        # B 不裁定对错
        "adjudication": None,
    }


def validate_stage_plan_against_portrait(
    plan: Mapping[str, Any],
    index: Mapping[str, Any],
    *,
    allowed_decision_kinds: Sequence[str],
    stage_rollback_points: Optional[Sequence[str]] = None,
) -> list[str]:
    """返回 reasons；空列表=通过。提案无效类由调用方包装。

    回滚点填 ④（cross_block_ownership）→ 计划校验拒绝
    （rollback_point_ownership_forbidden_in_stage），非 Schema 加载拒绝。
    """
    reasons: list[str] = []
    stages = plan.get("stages")
    if not isinstance(stages, list) or not stages:
        return ["stages_missing_or_empty"]

    allowed = set(allowed_decision_kinds)
    allowed_rb = (
        frozenset(str(x) for x in stage_rollback_points)
        if stage_rollback_points is not None
        else STAGE_ROLLBACK_POINTS
    )
    seen_stage_ids: set[str] = set()

    for stage in stages:
        if not isinstance(stage, Mapping):
            reasons.append("stage_not_object")
            continue
        for key in _STAGE_REQUIRED:
            if key not in stage:
                reasons.append("stage_field_missing:%s" % key)
        sid = stage.get("stage_id")
        if not isinstance(sid, str) or sid == "":
            reasons.append("stage_id_invalid")
        else:
            if sid in seen_stage_ids:
                reasons.append("stage_id_duplicate:%s" % sid)
            seen_stage_ids.add(sid)

        block_ids = stage.get("block_ids")
        if not isinstance(block_ids, list) or not block_ids:
            reasons.append("block_ids_missing")
            block_ids = []
        for raw in block_ids:
            bid = str(raw)
            if bid not in index["block_ids"]:
                reasons.append("unknown_block:%s" % bid)

        parents = stage.get("parent_table_ids")
        if not isinstance(parents, list) or not parents:
            reasons.append("parent_table_ids_missing")

        if "row_range" in stage and not isinstance(stage.get("row_range"), Mapping):
            reasons.append("row_range_invalid")
        if "col_range" in stage and not isinstance(stage.get("col_range"), Mapping):
            reasons.append("col_range_invalid")

        subs = stage.get("subtable_ids")
        if not isinstance(subs, list):
            reasons.append("subtable_ids_missing")
            subs = []

        dims = stage.get("dimension_ids")
        if dims is None:
            dims = []
        if not isinstance(dims, list):
            reasons.append("dimension_ids_invalid")
            dims = []

        layers = stage.get("layer_ids")
        if layers is None:
            layers = []
        if not isinstance(layers, list):
            reasons.append("layer_ids_invalid")
            layers = []

        for raw in block_ids:
            bid = str(raw)
            if bid in index["absent"]:
                if layers:
                    reasons.append("layers_on_absent_block:%s" % bid)
                continue
            if bid not in index["recognized"]:
                continue
            for s in subs:
                if str(s) not in index["subtables_by_block"].get(bid, set()):
                    # 子表可能只在部分块；若任一块包含即可
                    pass
            for layer in layers:
                if str(layer) not in index["layers_by_block"].get(bid, set()):
                    # 层须在该阶段点名的某个已识别块中
                    pass

        # 子表：每个 subtable 须在至少一个引用块中存在
        for s in subs:
            sid = str(s)
            found = False
            for raw in block_ids:
                bid = str(raw)
                if sid in index["subtables_by_block"].get(bid, set()):
                    found = True
                    break
            if not found and block_ids:
                reasons.append("unknown_subtable:%s" % sid)

        for d in dims:
            did = str(d)
            found = False
            for raw in block_ids:
                bid = str(raw)
                if did in index["dimensions_by_block"].get(bid, set()):
                    found = True
                    break
            if not found and block_ids:
                reasons.append("unknown_dimension:%s" % did)

        for layer in layers:
            lid = str(layer)
            found = False
            for raw in block_ids:
                bid = str(raw)
                if lid in index["layers_by_block"].get(bid, set()):
                    found = True
                    break
            if not found:
                reasons.append("unknown_layer:%s" % lid)

        kinds = stage.get("allowed_decision_kinds")
        if not isinstance(kinds, list) or not kinds:
            reasons.append("allowed_decision_kinds_missing")
        else:
            for k in kinds:
                if str(k) not in allowed:
                    reasons.append("decision_kind_not_in_enum:%s" % k)

        if "allow_user_ask" in stage and not isinstance(
            stage.get("allow_user_ask"), bool
        ):
            reasons.append("allow_user_ask_not_bool")

        rb = stage.get("rollback_point")
        if rb == ROLLBACK_OWNERSHIP:
            reasons.append("rollback_point_ownership_forbidden_in_stage")
        elif rb not in allowed_rb:
            reasons.append("rollback_point_invalid")

        if "dependency_allows_prior" in stage and not isinstance(
            stage.get("dependency_allows_prior"), bool
        ):
            reasons.append("dependency_allows_prior_not_bool")

    return reasons


def _one_chat(chat: Callable[[str], Any], prompt: str) -> Any:
    return chat(prompt)


def _build_generate_prompt(
    portraits: Mapping[str, Mapping[str, Any]],
    user_utterance: str,
    capability_text: str,
    allowed_kinds: Sequence[str],
) -> str:
    payload = {
        "portraits": portraits,
        "user_utterance": user_utterance,
        "capability_text": capability_text,
        "allowed_decision_kinds": list(allowed_kinds),
        "rollback_points": sorted(STAGE_ROLLBACK_POINTS),
        "note": (
            "Return JSON object with key stages (array). "
            "Do not invent layer/subtable/dimension ids absent from portraits. "
            "Empty dimension layer_ids means no header layers; not absent structure. "
            "Stage rollback_point must be one of stage rollback_points only; "
            "cross_block_ownership is not allowed inside stages. "
            "Each stage needs allow_user_ask (bool permission) and "
            "row_range/col_range objects."
        ),
    }
    return "B stage plan generate. Reply JSON only. " + json.dumps(
        payload, ensure_ascii=False, default=str
    )


def _build_review_prompt(
    portraits: Mapping[str, Mapping[str, Any]],
    plan: Mapping[str, Any],
) -> str:
    payload = {"portraits": portraits, "plan": plan}
    return (
        "B stage plan review. Reply JSON with passed (bool) and reason (string). "
        + json.dumps(payload, ensure_ascii=False, default=str)
    )


def run_stage_plan(
    *,
    run_id: Any,
    store: Any,
    chat: Callable[[str], Any],
    user_utterance: str,
    capability_text: str,
    plan_regenerate_max: Any = None,
    clock: Optional[Callable[[], str]] = None,
    contract_data: Optional[Mapping[str, Any]] = None,
    portraits: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """B 主入口。成功返回 plan；结构/复核失败在上限内重出；跨块发现单独返回。

    plan_regenerate_max：省略时从契约读；显式传入时仍校验（测用）。
    """
    rid = require_run_id(run_id)
    if plan_regenerate_max is None:
        regen_max = read_plan_regenerate_max(contract_data)
    else:
        regen_max = require_plan_regenerate_max(plan_regenerate_max)
    allowed_kinds = read_allowed_decision_kinds(contract_data)
    allowed_rb = read_stage_rollback_point_enum(contract_data)
    _clock = clock or _default_clock

    if portraits is None:
        portraits = load_portraits_from_store(store)
    if not portraits:
        raise CrossTableError(
            "cross_table_input_invalid",
            "no portrait slices for stage plan",
        )

    ownership = detect_cross_block_ownership_inconsistency(portraits)
    if ownership is not None:
        return {
            "ok": False,
            "status": "cross_block_ownership",
            "report": ownership,
            "plan": None,
            "trace_paths": [],
        }

    index = build_portrait_index(portraits, contract_data=contract_data)
    tracer = StagePlanTracer(rid, _clock, plan_generation=1)
    last_reasons: list[str] = []
    attempts = 0

    while attempts < regen_max:
        attempts += 1
        raw = _one_chat(
            chat,
            _build_generate_prompt(
                portraits, user_utterance, capability_text, allowed_kinds
            ),
        )
        tracer.write(KIND_GENERATE, {"attempt": attempts})
        if not isinstance(raw, dict):
            last_reasons = ["plan_not_object"]
            continue
        plan = {
            "stages": raw.get("stages"),
            "plan_generation": tracer.plan_generation,
        }
        reasons = validate_stage_plan_against_portrait(
            plan,
            index,
            allowed_decision_kinds=allowed_kinds,
            stage_rollback_points=sorted(allowed_rb),
        )
        if reasons:
            last_reasons = reasons
            continue

        review_raw = _one_chat(chat, _build_review_prompt(portraits, plan))
        tracer.write(KIND_REVIEW, {"attempt": attempts})
        if not isinstance(review_raw, dict) or "passed" not in review_raw:
            last_reasons = ["review_shape_invalid"]
            continue
        if review_raw.get("passed") is not True:
            last_reasons = ["review_failed:%s" % (review_raw.get("reason") or "")]
            continue

        # 交付前再确认声明齐全（§4）
        for stage in plan["stages"]:
            for key in _STAGE_REQUIRED:
                if key not in stage:
                    last_reasons = ["delivery_missing:%s" % key]
                    break
            else:
                continue
            break
        else:
            needs_user_ask = bool(review_raw.get("needs_user_ask", False))
            plan_with_review = dict(plan)
            plan_with_review["review_result"] = {
                "passed": True,
                "reason": review_raw.get("reason"),
                "needs_user_ask": needs_user_ask,
            }
            return {
                "ok": True,
                "status": "ok",
                "plan": plan_with_review,
                "review": {
                    "passed": True,
                    "reason": review_raw.get("reason"),
                    "needs_user_ask": needs_user_ask,
                },
                "trace_paths": list(tracer.paths),
                "attempts": attempts,
            }
        # delivery missing → retry
        continue

    # 选 B：达上限仍失败 → 组装冲突包交 I → E（不抛提案无效冒充终态）
    conflict = {
        "kind": "stage_plan_regenerate_exhausted",
        "reason_codes": ["plan_regenerate_exhausted"],
        "reasons": list(last_reasons),
        "attempts": attempts,
        "plan_regenerate_max": regen_max,
        "hand_off": "I_to_E",
    }
    return {
        "ok": False,
        "status": "conflict",
        "plan": None,
        "conflict": conflict,
        "reasons": last_reasons,
        "trace_paths": list(tracer.paths),
        "attempts": attempts,
    }
