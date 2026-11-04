"""A' 多维结构识别层：本地只做几何投影、包含事实与结构校验；层归属与表头语义由模型决定。

不重跑 matrix_geom。不改块划分。不读单元格原文做本地判断（原文只附给模型）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.layer_recognize_trace import (
    KIND_ASK_SUBSET,
    KIND_FORMAL_REVIEW,
    KIND_MERGE_LAYERS,
    KIND_PROPOSE,
    KIND_PROPOSE_SLICE,
    LayerRecognizeTraceSession,
    build_layer_recognize_trace_body,
    prompt_shape_from_propose_payload,
    prompt_shape_from_review_payload,
)

# 分组标签声明种类（模型枚举；本地只做集合核对）
_GROUP_EXACT = "exact_one_layer"
_GROUP_SPANS = "spans_multiple_layers"
_GROUP_NONE = "not_a_layer"
_GROUP_KINDS = frozenset({_GROUP_EXACT, _GROUP_SPANS, _GROUP_NONE})


def _merges_from_block(block: dict[str, Any] | None) -> list[tuple[int, int, int, int]]:
    out: list[tuple[int, int, int, int]] = []
    for raw in ((block or {}).get("context") or {}).get("merges") or []:
        if not isinstance(raw, dict):
            continue
        out.append(
            (
                int(raw.get("r0", -1)),
                int(raw.get("c0", -1)),
                int(raw.get("r1", -1)),
                int(raw.get("c1", -1)),
            )
        )
    return out


def _cell(grid: list, row: int, col: int) -> str:
    if row < 0 or row >= len(grid):
        return ""
    line = grid[row] or []
    if col < 0 or col >= len(line) or line[col] is None:
        return ""
    return str(line[col]).strip()


def _locator_key(loc: dict[str, int]) -> tuple[int, int, int, int]:
    return (loc["r0"], loc["c0"], loc["r1"], loc["c1"])


def _contains(outer: dict[str, int], inner: dict[str, int]) -> bool:
    """真包含：outer 的定位集合严格包含 inner。"""
    return (
        outer["r0"] <= inner["r0"]
        and outer["c0"] <= inner["c0"]
        and outer["r1"] >= inner["r1"]
        and outer["c1"] >= inner["c1"]
        and _locator_key(outer) != _locator_key(inner)
    )


def _disjoint(a: dict[str, int], b: dict[str, int]) -> bool:
    return a["r1"] <= b["r0"] or b["r1"] <= a["r0"] or a["c1"] <= b["c0"] or b["c1"] <= a["c0"]


def project_upstream_rectangles(
    block: dict[str, Any] | None,
    draft: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """把块上下文合并矩形与草案字段列范围投影为矩形列表。不扫网格、不读原文做判断。

    每个矩形带 kind、回指、定位。复杂度：合并与字段各一遍。
    """
    rects: list[dict[str, Any]] = []
    if not draft:
        return rects
    for index, merge in enumerate(_merges_from_block(block)):
        r0, c0, r1, c1 = merge
        if r0 < 0 or c0 < 0 or r1 <= r0 or c1 <= c0:
            continue
        rects.append(
            {
                "rect_id": "merge:%s" % index,
                "kind": "merge",
                "locator": {"r0": r0, "c0": c0, "r1": r1, "c1": c1},
                "back_ref": {"merge_index": index},
            }
        )
    for field in draft.get("fields") or []:
        field_id = field.get("field_id")
        role = field.get("role")
        for seg_i, seg in enumerate(field.get("segments") or []):
            col_range = seg.get("col_range") or []
            value_rows = seg.get("value_rows") or []
            if len(col_range) != 2 or not value_rows:
                continue
            c0, c1 = int(col_range[0]), int(col_range[1])
            if c1 <= c0:
                continue
            # 分组标签：顶带一行高；其它角色用 value_rows 的行包络
            if role == "group" and len(value_rows) == 1:
                r0 = int(value_rows[0])
                r1 = r0 + 1
            else:
                rows = [int(r) for r in value_rows]
                r0, r1 = min(rows), max(rows) + 1
            rects.append(
                {
                    "rect_id": "field:%s:%s" % (field_id, seg_i),
                    "kind": "group_label" if role == "group" else "field_segment",
                    "locator": {"r0": r0, "c0": c0, "r1": r1, "c1": c1},
                    "back_ref": {
                        "field_id": field_id,
                        "segment_index": seg_i,
                        "role": role,
                        "subtable_id": field.get("subtable_id"),
                    },
                }
            )
    return rects


def compute_containment_facts(rectangles: list[dict[str, Any]]) -> list[dict[str, str]]:
    """只在已收集矩形之间算包含 / 互不相交 / 相等 / 部分重叠。不重新扫网格。"""
    facts: list[dict[str, str]] = []
    n = len(rectangles)
    for i in range(n):
        for j in range(i + 1, n):
            a = rectangles[i]
            b = rectangles[j]
            la, lb = a["locator"], b["locator"]
            if _locator_key(la) == _locator_key(lb):
                relation = "equal"
            elif _contains(la, lb):
                relation = "contains"
                facts.append(
                    {"a": a["rect_id"], "b": b["rect_id"], "relation": relation}
                )
                continue
            elif _contains(lb, la):
                relation = "contains"
                facts.append(
                    {"a": b["rect_id"], "b": a["rect_id"], "relation": relation}
                )
                continue
            elif _disjoint(la, lb):
                relation = "disjoint"
            else:
                relation = "overlap"
            facts.append(
                {"a": a["rect_id"], "b": b["rect_id"], "relation": relation}
            )
    return facts


def attach_texts_for_model(
    rectangles: list[dict[str, Any]],
    grid: list,
    locator_ids: set[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """从已回指定位取单元格原文供模型阅读。不参与本地判断。

    方案 B：对本层（或本次复核子集）每个矩形定位包络内、已载入网格上的全部单元格取值。
    只遍历这些矩形的行列包络，不另扫全表其它区域。locator_ids 非空时只附子集。
    返回 {rect_id: [{r, c, text}, ...]}。
    """
    texts: dict[str, list[dict[str, Any]]] = {}
    for rect in rectangles:
        rid = rect["rect_id"]
        if locator_ids is not None and rid not in locator_ids:
            continue
        loc = rect["locator"]
        cells: list[dict[str, Any]] = []
        for r in range(loc["r0"], loc["r1"]):
            for c in range(loc["c0"], loc["c1"]):
                cells.append({"r": r, "c": c, "text": _cell(grid, r, c)})
        texts[rid] = cells
    return texts


def project_subtable_boundaries(draft: dict[str, Any] | None) -> list[dict[str, Any]]:
    """本地：每个已有子表标识的定位范围 = 该子表下全部字段 segment 定位的包络。

    回指已有 subtable_id 与 field_id 集合。无草案则空列表。不调模型。
    """
    if not draft:
        return []
    by_sub: dict[str, list[dict[str, int]]] = {}
    field_ids_by_sub: dict[str, list[str]] = {}
    for field in draft.get("fields") or []:
        sid = str(field.get("subtable_id"))
        fid = str(field.get("field_id") or "")
        field_ids_by_sub.setdefault(sid, [])
        if fid and fid not in field_ids_by_sub[sid]:
            field_ids_by_sub[sid].append(fid)
        for seg in field.get("segments") or []:
            col_range = seg.get("col_range") or []
            value_rows = seg.get("value_rows") or []
            if len(col_range) != 2 or not value_rows:
                continue
            c0, c1 = int(col_range[0]), int(col_range[1])
            rows = [int(x) for x in value_rows]
            loc = {"r0": min(rows), "c0": c0, "r1": max(rows) + 1, "c1": c1}
            by_sub.setdefault(sid, []).append(loc)
    # 草案声明的子表即使暂无字段也要出边界对象（空包络用 None）
    declared = [str(x) for x in (draft.get("subtable_ids") or [])]
    for sid in declared:
        by_sub.setdefault(sid, [])
        field_ids_by_sub.setdefault(sid, [])
    out: list[dict[str, Any]] = []
    for sid in sorted(by_sub.keys(), key=lambda x: (len(x), x)):
        locs = by_sub[sid]
        out.append(
            {
                "subtable_id": sid,
                "locator_envelope": _envelope(locs) if locs else None,
                "field_ids": list(field_ids_by_sub.get(sid) or []),
                "back_ref": {"subtable_id": sid},
            }
        )
    return out


def compute_object_nesting_facts(
    subtable_boundaries: list[dict[str, Any]],
    layers: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """子表边界包络与层包络之间的包含 / 互不相交 / 重叠。只在已有包络上算。"""
    facts: list[dict[str, str]] = []
    for boundary in subtable_boundaries:
        be = boundary.get("locator_envelope")
        if not be:
            continue
        bid = "subtable:" + str(boundary.get("subtable_id"))
        for layer in layers:
            le = layer.get("locator_envelope")
            if not le:
                continue
            lid = "layer:" + str(layer.get("layer_id"))
            if _locator_key(be) == _locator_key(le):
                relation = "equal"
                facts.append({"a": bid, "b": lid, "relation": relation})
            elif _contains(be, le):
                facts.append({"a": bid, "b": lid, "relation": "contains"})
            elif _contains(le, be):
                facts.append({"a": lid, "b": bid, "relation": "contains"})
            elif _disjoint(be, le):
                facts.append({"a": bid, "b": lid, "relation": "disjoint"})
            else:
                facts.append({"a": bid, "b": lid, "relation": "overlap"})
    return facts


def group_label_locator_union(
    draft: dict[str, Any],
    field_id: str,
) -> dict[str, int] | None:
    """某个分组标签字段全部 segment 的定位并集包络。"""
    for field in draft.get("fields") or []:
        if field.get("field_id") != field_id or field.get("role") != "group":
            continue
        r0 = c0 = None
        r1 = c1 = None
        for seg in field.get("segments") or []:
            col_range = seg.get("col_range") or []
            value_rows = seg.get("value_rows") or []
            if len(col_range) != 2 or not value_rows:
                continue
            sr0 = int(value_rows[0])
            sr1 = sr0 + 1
            sc0, sc1 = int(col_range[0]), int(col_range[1])
            r0 = sr0 if r0 is None else min(r0, sr0)
            c0 = sc0 if c0 is None else min(c0, sc0)
            r1 = sr1 if r1 is None else max(r1, sr1)
            c1 = sc1 if c1 is None else max(c1, sc1)
        if r0 is None:
            return None
        return {"r0": r0, "c0": c0, "r1": r1, "c1": c1}
    return None


def _layer_index(order: list[str], layer_id: str) -> int:
    try:
        return order.index(layer_id)
    except ValueError:
        return -1


def validate_layer_proposal(
    rectangles: list[dict[str, Any]],
    containment_facts: list[dict[str, str]],
    proposal: dict[str, Any],
    draft: dict[str, Any] | None,
    dimensions_for_block: dict[Any, list[str]] | None = None,
) -> dict[str, Any]:
    """结构校验。通过返回 {ok: True, ...归一化对象}；失败返回 {ok: False, reasons: [...]}。

    归属映射是「矩形 → 层标识」（多对一合法：同层可覆盖多个矩形）。
    层先后以有序列表下标为准；序号若出现且与列表冲突则失败。
    """
    reasons: list[str] = []
    if not isinstance(proposal, dict):
        return {"ok": False, "reasons": ["proposal_not_object"]}

    # Policy A: detect illicit envelope; never unwrap/guess intent.
    from pipeline.block_cross_table.layer_recognize_slice import (
        is_illicit_propose_envelope,
    )

    if is_illicit_propose_envelope(proposal):
        return {"ok": False, "reasons": ["envelope_wrapper_forbidden"]}

    rect_ids = {r["rect_id"] for r in rectangles}
    by_id = {r["rect_id"]: r for r in rectangles}

    raw_map = proposal.get("rectangle_layer_map")
    if not isinstance(raw_map, dict):
        return {"ok": False, "reasons": ["rectangle_layer_map_missing"]}
    # 归一化为 str→str；每个矩形恰好一条
    rect_to_layer: dict[str, str] = {}
    for key, value in raw_map.items():
        rid = str(key)
        if rid not in rect_ids:
            reasons.append("unknown_rect:" + rid)
            continue
        if not isinstance(value, str) or value == "":
            reasons.append("empty_layer_for:" + rid)
            continue
        rect_to_layer[rid] = value
    for rid in rect_ids:
        if rid not in rect_to_layer:
            reasons.append("rect_unmapped:" + rid)

    order_raw = proposal.get("layer_order")
    if not isinstance(order_raw, list) or not order_raw:
        reasons.append("layer_order_missing")
        order: list[str] = []
    else:
        order = []
        seen: set[str] = set()
        for item in order_raw:
            if not isinstance(item, str) or item == "":
                reasons.append("layer_order_bad_item")
                continue
            if item in seen:
                reasons.append("layer_order_duplicate:" + item)
                continue
            seen.add(item)
            order.append(item)

    used_layers = set(rect_to_layer.values())
    for layer_id in used_layers:
        if layer_id not in order:
            reasons.append("layer_not_in_order:" + layer_id)
    for layer_id in order:
        if layer_id not in used_layers:
            reasons.append("order_has_unused_layer:" + layer_id)

    # 若模型另带序号，只作冲突检测，不作主顺序
    ordinals = proposal.get("layer_ordinals")
    if isinstance(ordinals, dict) and order:
        for layer_id, ord_val in ordinals.items():
            lid = str(layer_id)
            if lid not in order:
                continue
            if not isinstance(ord_val, int) or isinstance(ord_val, bool):
                reasons.append("ordinal_not_int:" + lid)
                continue
            if ord_val != _layer_index(order, lid):
                reasons.append("ordinal_conflicts_order:" + lid)

    for fact in containment_facts:
        if fact.get("relation") != "contains":
            continue
        a, b = fact.get("a"), fact.get("b")
        if a not in rect_to_layer or b not in rect_to_layer:
            continue
        ia = _layer_index(order, rect_to_layer[a])
        ib = _layer_index(order, rect_to_layer[b])
        if ia < 0 or ib < 0:
            continue
        # 外层（包含方）必须更靠前：下标更小
        if not (ia < ib):
            reasons.append("containment_order_conflict:%s>%s" % (a, b))

    # 分组标签声明
    decls = proposal.get("group_label_decls") or []
    if decls and not isinstance(decls, list):
        reasons.append("group_label_decls_bad")
        decls = []
    group_field_ids = {
        str(f.get("field_id"))
        for f in (draft or {}).get("fields") or []
        if f.get("role") == "group"
    }
    for decl in decls:
        if not isinstance(decl, dict):
            reasons.append("group_decl_bad")
            continue
        fid = str(decl.get("field_id") or "")
        kind = decl.get("kind")
        layer_ids = decl.get("layer_ids") or []
        if fid not in group_field_ids:
            reasons.append("group_decl_unknown_field:" + fid)
            continue
        if kind not in _GROUP_KINDS:
            reasons.append("group_decl_bad_kind:" + fid)
            continue
        if not isinstance(layer_ids, list):
            reasons.append("group_decl_layers_bad:" + fid)
            continue
        layer_ids = [str(x) for x in layer_ids]
        union_loc = group_label_locator_union(draft or {}, fid)
        layer_rects = [
            by_id[rid]
            for rid, lid in rect_to_layer.items()
            if lid in layer_ids and rid in by_id
        ]
        if kind == _GROUP_NONE:
            if layer_ids:
                reasons.append("group_none_has_layers:" + fid)
            # 不删字段；不要求产生层对象
            continue
        if kind == _GROUP_EXACT:
            if len(layer_ids) != 1:
                reasons.append("group_exact_need_one_layer:" + fid)
                continue
            # 该层全部矩形定位并集包络须与分组标签列范围相同
            if not layer_rects or union_loc is None:
                reasons.append("group_exact_missing_geometry:" + fid)
                continue
            env = _envelope([r["locator"] for r in layer_rects])
            if env != union_loc:
                reasons.append("group_exact_set_mismatch:" + fid)
        if kind == _GROUP_SPANS:
            if len(layer_ids) < 2:
                reasons.append("group_spans_need_many:" + fid)
                continue
            if union_loc is None:
                reasons.append("group_spans_missing_geometry:" + fid)
                continue
            # 各层对象回指该列范围的一部分；并集等于该列范围
            env = _envelope([r["locator"] for r in layer_rects]) if layer_rects else None
            if env != union_loc:
                reasons.append("group_spans_union_mismatch:" + fid)
            for rect in layer_rects:
                if not _contains(union_loc, rect["locator"]) and _locator_key(
                    rect["locator"]
                ) != _locator_key(union_loc):
                    # 部分：矩形须落在并集内
                    if not (
                        union_loc["r0"] <= rect["locator"]["r0"]
                        and union_loc["c0"] <= rect["locator"]["c0"]
                        and union_loc["r1"] >= rect["locator"]["r1"]
                        and union_loc["c1"] >= rect["locator"]["c1"]
                    ):
                        reasons.append("group_spans_part_outside:" + fid)

    # 维度归属：模型声明；本地只校验回指与覆盖。空层列表合法（后续-7）。
    dim_decls = proposal.get("dimension_membership") or []
    known_dims: set[tuple[str, str]] = set()
    for sub_id, field_ids in (dimensions_for_block or {}).items():
        for fid in field_ids or []:
            known_dims.add((str(sub_id), str(fid)))
    if dim_decls and not isinstance(dim_decls, list):
        reasons.append("dimension_membership_bad")
        dim_decls = []
    covered_dims: set[tuple[str, str]] = set()
    for decl in dim_decls:
        if not isinstance(decl, dict):
            reasons.append("dim_decl_bad")
            continue
        sub_id = str(decl.get("subtable_id"))
        fid = str(decl.get("dimension_field_id") or "")
        if "layer_ids" not in decl:
            reasons.append("dim_layers_missing:%s" % fid)
            continue
        layer_ids = decl.get("layer_ids")
        if known_dims and (sub_id, fid) not in known_dims:
            reasons.append("dim_unknown:%s:%s" % (sub_id, fid))
        if not isinstance(layer_ids, list):
            reasons.append("dim_layers_not_list:%s" % fid)
            continue
        for lid in layer_ids:
            if str(lid) not in order:
                reasons.append("dim_layer_unknown:" + str(lid))
        covered_dims.add((sub_id, fid))
    if known_dims:
        for pair in known_dims:
            if pair not in covered_dims:
                reasons.append("dim_unassigned:%s:%s" % pair)

    if reasons:
        return {"ok": False, "reasons": reasons}

    layers = []
    for layer_id in order:
        member_rects = [rid for rid, lid in rect_to_layer.items() if lid == layer_id]
        locators = [by_id[rid]["locator"] for rid in member_rects if rid in by_id]
        layers.append(
            {
                "layer_id": layer_id,
                "rect_ids": member_rects,
                "locator_envelope": _envelope(locators) if locators else None,
            }
        )

    subtable_boundaries = project_subtable_boundaries(draft)
    object_nesting = compute_object_nesting_facts(subtable_boundaries, layers)
    header_completeness = _normalize_header_completeness(proposal)

    return {
        "ok": True,
        "rectangle_layer_map": rect_to_layer,
        "layer_order": order,
        "layers": layers,
        "subtable_boundaries": subtable_boundaries,
        "object_nesting_facts": object_nesting,
        "group_label_decls": list(decls),
        "dimension_membership": list(dim_decls),
        "complete_header": header_completeness == "complete",
        "header_completeness": header_completeness,
    }


def _envelope(locators: list[dict[str, int]]) -> dict[str, int]:
    return {
        "r0": min(l["r0"] for l in locators),
        "c0": min(l["c0"] for l in locators),
        "r1": max(l["r1"] for l in locators),
        "c1": max(l["c1"] for l in locators),
    }


_HEADER_COMPLETE = "complete"
_HEADER_INCOMPLETE = "incomplete"
_HEADER_NOT_HEADER = "not_header"
_OK_LAYER_FIELDS = (
    "rect_to_layer",
    "layer_order",
    "layers",
    "subtable_boundaries",
    "nesting_facts",
    "group_label_declaration",
    "dimension_membership",
    "header_completeness",
)


def _normalize_header_completeness(proposal: dict[str, Any]) -> str | None:
    """契约三值优先；兼容旧布尔 complete_header。缺省 → None（由【19】判缺）。"""
    raw = proposal.get("header_completeness")
    if isinstance(raw, str) and raw in {
        _HEADER_COMPLETE,
        _HEADER_INCOMPLETE,
        _HEADER_NOT_HEADER,
    }:
        return raw
    if "complete_header" in proposal:
        flag = proposal.get("complete_header")
        if flag is True:
            return _HEADER_COMPLETE
        if flag is False:
            return _HEADER_INCOMPLETE
    return None


def _header_judgment_text(header_completeness: str | None) -> str:
    return "header_completeness=%s" % (header_completeness,)


def build_ok_layer_result(validated: dict[str, Any]) -> dict[str, Any]:
    """对外成功层结果：契约 8 字段名；并保留旧键别名供既有测/复核载荷。"""
    rect_map = validated.get("rectangle_layer_map")
    nesting = validated.get("object_nesting_facts")
    groups = validated.get("group_label_decls")
    header = validated.get("header_completeness")
    return {
        "rect_to_layer": rect_map,
        "layer_order": validated.get("layer_order"),
        "layers": validated.get("layers"),
        "subtable_boundaries": validated.get("subtable_boundaries"),
        "nesting_facts": nesting,
        "group_label_declaration": groups,
        "dimension_membership": validated.get("dimension_membership"),
        "header_completeness": header,
        # 别名（复核载荷 / 旧测）
        "rectangle_layer_map": rect_map,
        "object_nesting_facts": nesting,
        "group_label_decls": groups,
        "complete_header": header == _HEADER_COMPLETE,
    }


def assert_ok_layer_fields_complete(layer_result: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for key in _OK_LAYER_FIELDS:
        if key not in layer_result or layer_result.get(key) is None:
            # dimension_membership / nesting_facts / layers 允许空列表
            if key in (
                "dimension_membership",
                "nesting_facts",
                "layers",
                "subtable_boundaries",
                "group_label_declaration",
            ) and key in layer_result and isinstance(layer_result.get(key), list):
                continue
            missing.append(key)
    return missing


def portrait_metrics_absent(block_id: Any) -> dict[str, Any]:
    """有序布局无矩阵草案：声明上游无此结构，不编造层。"""
    return {
        "block_id": block_id,
        "upstream_structure": "absent",
        "rectangle_count": 0,
        "containment_fact_count": 0,
        "layer_count": 0,
        "layers": [],
    }


def portrait_metrics_from_result(
    block_id: Any,
    rectangles: list[dict[str, Any]],
    facts: list[dict[str, str]],
    validated: dict[str, Any],
) -> dict[str, Any]:
    """写入画像的度量：只有计数与标识集合，无「该不该切」布尔结论。"""
    boundaries = validated.get("subtable_boundaries") or []
    dim_mem = validated.get("dimension_membership") or []
    return {
        "block_id": block_id,
        "upstream_structure": "matrix_draft",
        "rectangle_count": len(rectangles),
        "containment_fact_count": len(facts),
        "layer_count": len(validated.get("layer_order") or []),
        "layer_ids": list(validated.get("layer_order") or []),
        "subtable_boundary_count": len(boundaries),
        "subtable_ids": [b.get("subtable_id") for b in boundaries],
        "dimension_membership_count": len(dim_mem),
        "containment_pairs": [
            [f["a"], f["b"]]
            for f in facts
            if f.get("relation") == "contains"
        ],
    }


def review_payload_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def build_review_payload(
    upstream: dict[str, Any],
    layer_result: dict[str, Any],
    cell_texts: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    return {
        "upstream": upstream,
        "layer_result": {
            "rect_to_layer": layer_result.get("rect_to_layer")
            or layer_result.get("rectangle_layer_map"),
            "rectangle_layer_map": layer_result.get("rectangle_layer_map")
            or layer_result.get("rect_to_layer"),
            "layer_order": layer_result.get("layer_order"),
            "layers": layer_result.get("layers"),
            "subtable_boundaries": layer_result.get("subtable_boundaries"),
            "nesting_facts": layer_result.get("nesting_facts")
            or layer_result.get("object_nesting_facts"),
            "object_nesting_facts": layer_result.get("object_nesting_facts")
            or layer_result.get("nesting_facts"),
            "group_label_declaration": layer_result.get("group_label_declaration")
            or layer_result.get("group_label_decls"),
            "group_label_decls": layer_result.get("group_label_decls")
            or layer_result.get("group_label_declaration"),
            "dimension_membership": layer_result.get("dimension_membership"),
            "header_completeness": layer_result.get("header_completeness"),
            "complete_header": layer_result.get("complete_header"),
        },
        "cell_texts": cell_texts,
    }


def shrink_review_locator_ids(
    recognized: dict[str, Any],
    requested_ids: list[Any],
) -> set[str]:
    """复核缩范围：只缩小「本次复核覆盖的定位」，不重定义层。

    requested 必须是已识别矩形标识的子集。返回通过校验的集合。
    不等于重做 A'：不改 rectangle_layer_map / layer_order。
    """
    known = set((recognized.get("rectangle_layer_map") or {}).keys())
    if not known and recognized.get("layers"):
        for layer in recognized["layers"]:
            known.update(layer.get("rect_ids") or [])
    out: set[str] = set()
    for item in requested_ids or []:
        rid = str(item)
        if rid not in known:
            raise CrossTableError(
                "cross_table_proposal_invalid",
                "review locator not in recognized set",
                {"rect_id": rid},
            )
        out.add(rid)
    if not out:
        raise CrossTableError(
            "cross_table_proposal_invalid",
            "review locator subset empty",
        )
    return out


def assert_review_within_capacity(
    payload: dict[str, Any],
    capacity: int | None,
) -> None:
    """容量必须来自契约（由调用方传入）。缺容量则结构错误。禁止截断原文。"""
    if capacity is None:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "review input capacity missing",
        )
    if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "review input capacity invalid",
        )
    if review_payload_size(payload) > capacity:
        raise CrossTableError(
            "cross_table_proposal_invalid",
            "review input over capacity",
            {"size": review_payload_size(payload), "capacity": capacity},
        )


def _ask_max_rounds() -> int:
    """方案甲：只读契约 layer_recognize_ask_max_rounds；不读旧键 user_retry_max_rounds。"""
    from pipeline.block_cross_table.layer_recognize_limits import (
        read_layer_recognize_ask_max_rounds,
    )

    return read_layer_recognize_ask_max_rounds()


def _usage_dict(n: int, m: int, k: int) -> dict[str, int]:
    return {
        "proposal": int(n),
        "subset": int(m),
        "review": int(k),
        "total": int(n + m + k),
    }


def _conflict_result(
    *,
    block_id: Any,
    rectangles: list[dict[str, Any]],
    facts: list[dict[str, str]],
    validated: dict[str, Any] | None,
    reason_codes: list[str],
    n: int,
    m: int,
    k: int,
    draft_retained: bool = True,
    extra_reasons: list[str] | None = None,
) -> dict[str, Any]:
    codes = [str(c) for c in reason_codes if c]
    if not codes:
        codes = ["proposal_retries_exhausted"]
    metrics_src = validated if validated is not None else {"layer_order": []}
    return {
        "status": "conflict",
        "reason_codes": codes,
        "ask_rounds_used": n + m + k,
        "ask_usage": _usage_dict(n, m, k),
        "layer_result_empty": True,
        "draft_retained": draft_retained,
        "portrait_metric_hint": portrait_metrics_from_result(
            block_id, rectangles, facts, metrics_src
        ),
        "metrics": portrait_metrics_from_result(
            block_id, rectangles, facts, metrics_src
        ),
        "layer_result": None,
        "rolled_back": True,
        "reasons": list(extra_reasons or []) + codes,
        "stage_snapshot_kept_draft": draft_retained,
    }


def _review_failed_result(
    *,
    block_id: Any,
    rectangles: list[dict[str, Any]],
    facts: list[dict[str, str]],
    rejection_reason: str,
    review_locator_ids: list[str] | None,
    n: int,
    m: int,
    k: int,
    draft_retained: bool = True,
) -> dict[str, Any]:
    return {
        "status": "review_failed",
        "rejection_reason": rejection_reason,
        "review_locator_ids": list(review_locator_ids or []),
        "ask_rounds_used": n + m + k,
        "ask_usage": _usage_dict(n, m, k),
        "layer_result_empty": True,
        "draft_retained": draft_retained,
        "metrics": portrait_metrics_from_result(
            block_id, rectangles, facts, {"layer_order": []}
        ),
        "layer_result": None,
        "rolled_back": True,
        "reasons": [rejection_reason],
        "stage_snapshot_kept_draft": draft_retained,
    }


def propose_layer_recognition(
    user_utterance: str,
    rectangles: list[dict[str, Any]],
    containment_facts: list[dict[str, str]],
    cell_texts: dict[str, list[dict[str, Any]]],
    draft: dict[str, Any] | None,
    dimensions_for_block: dict[Any, list[str]] | None,
    subtable_boundaries: list[dict[str, Any]] | None = None,
    *,
    prompt_out: dict[str, Any] | None = None,
    prebuilt_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """每次只调模型一次。不内部重试。facts 经 propose 过滤（无 disjoint）。"""
    from pipeline.block_cross_table.api import _one_model_call
    from pipeline.block_cross_table.layer_recognize_slice import (
        PROPOSE_PREFIX,
        build_propose_payload,
        filter_containment_facts_for_propose,
    )

    if prebuilt_payload is not None:
        payload = prebuilt_payload
    else:
        filtered = filter_containment_facts_for_propose(containment_facts)
        payload = build_propose_payload(
            user_utterance=user_utterance,
            rectangles=rectangles,
            containment_facts=filtered,
            cell_texts=cell_texts,
            draft=draft,
            dimensions_for_block=dimensions_for_block,
            subtable_boundaries=subtable_boundaries,
        )
    prompt = PROPOSE_PREFIX + json.dumps(payload, ensure_ascii=False)
    if prompt_out is not None:
        prompt_out["prompt"] = prompt
        prompt_out["prompt_shape"] = prompt_shape_from_propose_payload(payload)
        prompt_out["started"] = True
        if "facts_filter" not in prompt_out:
            rels = sorted(
                {
                    str(f.get("relation") or "")
                    for f in (payload.get("containment_facts") or [])
                    if isinstance(f, dict)
                }
            )
            prompt_out["facts_filter"] = rels
    raw = _one_model_call(prompt)
    if prompt_out is not None:
        prompt_out["raw"] = raw
        prompt_out["raw_received"] = True
    if not isinstance(raw, dict):
        raise CrossTableError("cross_table_proposal_invalid", "layer proposal not object")
    return raw


def review_layer_recognition(
    review_payload: dict[str, Any],
    *,
    prompt_out: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """模型复核。返回 {passed: bool, reason: str, review_locator_ids?: list}。

    review_locator_ids 若出现：表示请求缩小「本次复核覆盖的定位」，不是重定义层。
    """
    from pipeline.block_cross_table.api import _one_model_call

    prompt = (
        "A' layer review. Reply JSON with passed (bool), reason (string), "
        "optional review_locator_ids (array of rect_id subset for this review only). "
        + json.dumps(review_payload, ensure_ascii=False)
    )
    if prompt_out is not None:
        prompt_out["prompt"] = prompt
        prompt_out["prompt_shape"] = prompt_shape_from_review_payload(review_payload)
        prompt_out["started"] = True
    raw = _one_model_call(prompt)
    if prompt_out is not None:
        prompt_out["raw"] = raw
        prompt_out["raw_received"] = True
    if not isinstance(raw, dict):
        raise CrossTableError("cross_table_proposal_invalid", "layer review not object")
    return raw


def _propose_validate_reasons(
    *,
    propose_exc: BaseException | None,
    validate_exc: BaseException | None,
    checked: dict[str, Any] | None,
) -> list[str]:
    if propose_exc is not None:
        return ["propose_exception:%s" % type(propose_exc).__name__]
    if validate_exc is not None:
        return ["validate_exception:%s" % type(validate_exc).__name__]
    if checked is not None and checked.get("ok") is True:
        return []
    if checked is not None and checked.get("ok") is False:
        reasons = checked.get("reasons") or []
        if reasons:
            return [str(x) for x in reasons]
        return ["validate_false_with_empty_reasons"]
    return []


def _write_started_trace(
    *,
    trace_session: LayerRecognizeTraceSession | None,
    call_kind: str,
    prompt_out: dict[str, Any],
    n: int,
    m: int,
    k: int,
    validate_reasons: list[str],
    exception_for_raw: BaseException | None,
    exception_prefix: str,
    propose_exc: BaseException | None,
    validate_exc: BaseException | None,
) -> None:
    if trace_session is None:
        return
    body = build_layer_recognize_trace_body(
        prompt=str(prompt_out.get("prompt") or ""),
        prompt_shape=dict(prompt_out.get("prompt_shape") or {}),
        ask_usage_after={"proposal": n, "subset": m, "review": k},
        validate_reasons=validate_reasons,
        prompt_out=prompt_out,
        exception_for_raw=exception_for_raw,
        exception_prefix=exception_prefix,
        contract_data=trace_session.contract_data,
    )
    try:
        trace_session.write(call_kind, body)
    except Exception as trace_exc:
        business = propose_exc or validate_exc
        if business is not None:
            raise business from trace_exc
        raise


def run_layer_recognize_block(
    block: dict[str, Any],
    draft: dict[str, Any] | None,
    tables_by_id: dict[str, Any],
    dimensions_for_block: dict[Any, list[str]] | None,
    user_utterance: str,
    review_input_capacity: int | None,
    chat: Callable[[str], Any] | None = None,
    *,
    run_id: Optional[str] = None,
    entry_seq: int = 1,
    clock: Optional[Callable[[], str]] = None,
) -> dict[str, Any]:
    """单块 A'：共用追问计数 N+M+K；提案/要子集预留正式复核；失败回滚丢本层、保留草案。

    review_input_capacity 必须来自契约（调用方传入）。本函数不写死容量数值。
    追问上限只读 layer_recognize_ask_max_rounds（方案甲）。
    传入 run_id 时，提案/要子集/正式复核各经既有 write_trace 落点（五元组 meta）。
    未传 run_id 时不落点（兼容旧调用）；主动写盘路径缺 run_id 由 trace 模块抛审计结构无效。
    """
    from pipeline.block_cross_table import api as cross_table_api

    block_id = (block or {}).get("block_id")
    stage_snapshot = {"layer_objects": None, "draft": draft}
    trace_session: LayerRecognizeTraceSession | None = None
    if run_id is not None and str(run_id).strip() != "":
        _clock = clock or (
            lambda: datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        )
        trace_session = LayerRecognizeTraceSession(
            str(run_id), block_id, int(entry_seq), _clock
        )

    if (block or {}).get("layout") != "matrix" or not draft:
        # 无此结构：允许缺回调
        return {
            "status": "no_structure",
            "metrics": portrait_metrics_absent(block_id),
            "layer_result": None,
            "layer_result_empty": True,
            "draft_retained": True,
            "ask_rounds_used": 0,
            "ask_usage": _usage_dict(0, 0, 0),
            "rolled_back": False,
            "stage_snapshot_kept_draft": True,
        }

    # 【18】草案非空但网格缺 → 输入无效类（不是无此结构）
    parent = (block.get("parent_table_ids") or [None])[0]
    parent_table = (tables_by_id or {}).get(parent)
    if not isinstance(parent_table, dict) or "grid" not in parent_table:
        raise CrossTableError(
            "cross_table_input_invalid",
            "matrix draft present but grid missing",
            {"block_id": block_id, "parent_table_id": parent},
        )
    grid = parent_table.get("grid")
    if not isinstance(grid, list):
        raise CrossTableError(
            "cross_table_input_invalid",
            "matrix draft present but grid invalid",
            {"block_id": block_id, "parent_table_id": parent},
        )

    # 【11】(b) 矩阵路径必用回调；入参与已绑定通道皆无 → 输入无效类
    # 经模块属性读 _CHAT，避免 from-import 拿到旧绑定
    if chat is not None:
        cross_table_api.bind_chat_json(chat)
    if cross_table_api._CHAT is None:
        raise CrossTableError(
            "cross_table_input_invalid",
            "chat client is not bound",
            {"block_id": block_id},
        )

    n = m = k = 0

    rectangles = project_upstream_rectangles(block, draft)
    facts = compute_containment_facts(rectangles)
    subtable_boundaries = project_subtable_boundaries(draft)
    texts_all = attach_texts_for_model(rectangles, grid, locator_ids=None)

    from pipeline.block_cross_table.layer_recognize_limits import (
        read_layer_recognize_propose_input_max_chars,
    )
    from pipeline.block_cross_table.layer_recognize_slice import (
        build_merge_input,
        build_slice_propose_payload,
        filter_containment_facts_for_propose,
        merge_layer_recognition,
        plan_propose_slices,
        trim_merge_input_to_capacity,
        validate_merge_result,
    )

    facts_model = filter_containment_facts_for_propose(facts)
    ask_max = _ask_max_rounds()
    try:
        max_propose = read_layer_recognize_propose_input_max_chars()
        plans = plan_propose_slices(
            rectangles=rectangles,
            facts_model=facts_model,
            texts_all=texts_all,
            user_utterance=user_utterance,
            draft=draft,
            dimensions_for_block=dimensions_for_block,
            subtable_boundaries=subtable_boundaries,
            max_chars=max_propose,
        )
    except CrossTableError as err:
        codes = (err.details or {}).get("reason_codes") or [
            "propose_single_rect_over_capacity"
        ]
        return _conflict_result(
            block_id=block_id,
            rectangles=rectangles,
            facts=facts,
            validated=None,
            reason_codes=list(codes),
            n=n,
            m=m,
            k=k,
        )

    slice_count = len(plans)
    multi_slice = slice_count > 1
    if multi_slice and slice_count + 1 > ask_max:
        return _conflict_result(
            block_id=block_id,
            rectangles=rectangles,
            facts=facts,
            validated=None,
            reason_codes=["propose_slice_count_exceeds_ask_budget"],
            n=n,
            m=m,
            k=k,
            extra_reasons=["S=%s ask_max=%s" % (slice_count, ask_max)],
        )

    last_reasons: list[str] = []
    validated: dict[str, Any] | None = None
    skip_formal_review = False

    if multi_slice:
        slice_checked: list[dict[str, Any]] = []
        for plan in plans:
            prompt_out: dict[str, Any] = {
                "slice_index": plan.slice_index,
                "slice_count": slice_count,
                "slice_rect_ids": list(plan.rect_ids),
                "facts_filter": ["contains", "overlap", "equal"],
                "merge_phase": False,
            }
            propose_exc: Exception | None = None
            validate_exc: Exception | None = None
            checked: dict[str, Any] | None = None
            payload = build_slice_propose_payload(
                user_utterance=user_utterance,
                window_rects=plan.rectangles,
                facts_model=facts_model,
                texts_all=texts_all,
                draft=draft,
                dimensions_for_block=dimensions_for_block,
                subtable_boundaries=subtable_boundaries,
            )
            try:
                try:
                    proposal = propose_layer_recognition(
                        user_utterance,
                        plan.rectangles,
                        facts_model,
                        {
                            rid: texts_all[rid]
                            for rid in plan.rect_ids
                            if rid in texts_all
                        },
                        draft,
                        dimensions_for_block,
                        subtable_boundaries,
                        prompt_out=prompt_out,
                        prebuilt_payload=payload,
                    )
                except Exception as exc:
                    propose_exc = exc
                    raise
                try:
                    checked = validate_layer_proposal(
                        plan.rectangles,
                        [
                            f
                            for f in facts_model
                            if f.get("a") in set(plan.rect_ids)
                            and f.get("b") in set(plan.rect_ids)
                        ],
                        proposal,
                        draft,
                        dimensions_for_block,
                    )
                except Exception as exc:
                    validate_exc = exc
                    raise
            finally:
                if prompt_out.get("started"):
                    n += 1
                    reasons = _propose_validate_reasons(
                        propose_exc=propose_exc,
                        validate_exc=validate_exc,
                        checked=checked,
                    )
                    _write_started_trace(
                        trace_session=trace_session,
                        call_kind=KIND_PROPOSE_SLICE,
                        prompt_out=prompt_out,
                        n=n,
                        m=m,
                        k=k,
                        validate_reasons=reasons,
                        exception_for_raw=propose_exc,
                        exception_prefix="propose",
                        propose_exc=propose_exc,
                        validate_exc=validate_exc,
                    )
            if checked is None or not checked.get("ok"):
                return _conflict_result(
                    block_id=block_id,
                    rectangles=rectangles,
                    facts=facts,
                    validated=None,
                    reason_codes=["propose_slice_validate_failed"],
                    n=n,
                    m=m,
                    k=k,
                    extra_reasons=list((checked or {}).get("reasons") or [])
                    + ["slice_index=%s" % plan.slice_index],
                )
            slice_checked.append(checked)

        merge_in = build_merge_input(
            plans=plans,
            slice_checked=slice_checked,
            facts_all=facts,
            texts_all=texts_all,
        )
        trim_level = 0
        if isinstance(review_input_capacity, int) and review_input_capacity > 0:
            try:
                merge_in, trim_level = trim_merge_input_to_capacity(
                    merge_in, review_input_capacity
                )
            except CrossTableError as err:
                codes = (err.details or {}).get("reason_codes") or [
                    "cross_slice_merge_input_over_capacity"
                ]
                return _conflict_result(
                    block_id=block_id,
                    rectangles=rectangles,
                    facts=facts,
                    validated=None,
                    reason_codes=list(codes),
                    n=n,
                    m=m,
                    k=k,
                )

        prompt_out_m: dict[str, Any] = {
            "merge_phase": True,
            "slice_count": slice_count,
            "merge_input_trim_level": trim_level,
            "facts_filter": ["contains", "overlap", "equal"],
        }
        merge_exc: Exception | None = None
        merge_raw: dict[str, Any] | None = None
        try:
            try:
                merge_raw = merge_layer_recognition(
                    merge_in, prompt_out=prompt_out_m
                )
            except Exception as exc:
                merge_exc = exc
                raise
        finally:
            if prompt_out_m.get("started"):
                k += 1
                vr = (
                    ["merge_exception:%s" % type(merge_exc).__name__]
                    if merge_exc is not None
                    else []
                )
                _write_started_trace(
                    trace_session=trace_session,
                    call_kind=KIND_MERGE_LAYERS,
                    prompt_out=prompt_out_m,
                    n=n,
                    m=m,
                    k=k,
                    validate_reasons=vr,
                    exception_for_raw=merge_exc,
                    exception_prefix="review",
                    propose_exc=merge_exc,
                    validate_exc=None,
                )
        assert merge_raw is not None
        merged = validate_merge_result(
            raw=merge_raw,
            rectangles=rectangles,
            facts_all=facts,
            draft=draft,
            dimensions_for_block=dimensions_for_block,
        )
        if merged["ok_status"] == "conflict":
            return _conflict_result(
                block_id=block_id,
                rectangles=rectangles,
                facts=facts,
                validated=None,
                reason_codes=list(merged.get("reason_codes") or []),
                n=n,
                m=m,
                k=k,
                extra_reasons=[str(merged.get("rejection_reason") or "")],
            )
        if merged["ok_status"] == "review_failed":
            return _review_failed_result(
                block_id=block_id,
                rectangles=rectangles,
                facts=facts,
                rejection_reason=str(merged.get("rejection_reason") or "review_failed"),
                review_locator_ids=[],
                n=n,
                m=m,
                k=k,
            )
        validated = merged["validated"]
        skip_formal_review = True
    else:
        # 单片：既有提案循环（facts_model；可重试）+ 后续 formal_review
        while validated is None:
            ask_max = _ask_max_rounds()
            if (n + m + k) + 1 + 1 > ask_max:
                if n == 0:
                    code = "ask_budget_exhausted_proposal"
                elif (n + m + k) >= ask_max:
                    code = "proposal_retries_exhausted"
                else:
                    code = "ask_budget_exhausted_proposal"
                return _conflict_result(
                    block_id=block_id,
                    rectangles=rectangles,
                    facts=facts,
                    validated=None,
                    reason_codes=[code],
                    n=n,
                    m=m,
                    k=k,
                    extra_reasons=last_reasons,
                )
            prompt_out = {
                "facts_filter": ["contains", "overlap", "equal"],
                "slice_count": 1,
                "merge_phase": False,
            }
            propose_exc = None
            validate_exc = None
            checked = None
            try:
                try:
                    proposal = propose_layer_recognition(
                        user_utterance,
                        rectangles,
                        facts_model,
                        texts_all,
                        draft,
                        dimensions_for_block,
                        subtable_boundaries,
                        prompt_out=prompt_out,
                    )
                except Exception as exc:
                    propose_exc = exc
                    raise
                try:
                    checked = validate_layer_proposal(
                        rectangles, facts, proposal, draft, dimensions_for_block
                    )
                except Exception as exc:
                    validate_exc = exc
                    raise
            finally:
                if prompt_out.get("started"):
                    n += 1
                    reasons = _propose_validate_reasons(
                        propose_exc=propose_exc,
                        validate_exc=validate_exc,
                        checked=checked,
                    )
                    _write_started_trace(
                        trace_session=trace_session,
                        call_kind=KIND_PROPOSE,
                        prompt_out=prompt_out,
                        n=n,
                        m=m,
                        k=k,
                        validate_reasons=reasons,
                        exception_for_raw=propose_exc,
                        exception_prefix="propose",
                        propose_exc=propose_exc,
                        validate_exc=validate_exc,
                    )
            if checked is not None and checked.get("ok"):
                validated = checked
                break
            last_reasons = list((checked or {}).get("reasons") or [])
            ask_max = _ask_max_rounds()
            if (n + m + k) + 1 + 1 > ask_max:
                return _conflict_result(
                    block_id=block_id,
                    rectangles=rectangles,
                    facts=facts,
                    validated=None,
                    reason_codes=["ask_budget_exhausted_proposal"],
                    n=n,
                    m=m,
                    k=k,
                    extra_reasons=last_reasons,
                )

    assert validated is not None
    layer_result = build_ok_layer_result(validated)
    missing = assert_ok_layer_fields_complete(layer_result)
    if missing:
        return _conflict_result(
            block_id=block_id,
            rectangles=rectangles,
            facts=facts,
            validated=validated,
            reason_codes=["success_payload_incomplete"],
            n=n,
            m=m,
            k=k,
            extra_reasons=["missing:" + x for x in missing],
        )

    if skip_formal_review:
        return {
            "status": "ok",
            "metrics": portrait_metrics_from_result(
                block_id, rectangles, facts, validated
            ),
            "layer_result": layer_result,
            "layer_result_empty": False,
            "draft_retained": True,
            "ask_rounds_used": n + m + k,
            "ask_usage": _usage_dict(n, m, k),
            "rolled_back": False,
            "review_locator_ids": None,
            "stage_snapshot_kept_draft": True,
        }

    upstream = {
        "block_id": block_id,
        "rectangles": rectangles,
        "containment_facts": facts,
        "subtable_boundaries": subtable_boundaries,
        "subtable_ids": list(draft.get("subtable_ids") or []),
    }

    review_ids: set[str] | None = None
    # 要子集循环：同样预留正式复核
    while True:
        texts = attach_texts_for_model(rectangles, grid, locator_ids=review_ids)
        payload = build_review_payload(upstream, layer_result, texts)
        try:
            assert_review_within_capacity(payload, review_input_capacity)
        except CrossTableError as err:
            if err.message != "review input over capacity":
                raise
            # 要子集请求本身：若 shrink 请求体也超容量且无法发起模型调用
            shrink_ask = {
                "status": "need_review_scope_shrink",
                "known_rect_ids": sorted(
                    (layer_result.get("rect_to_layer") or {}).keys()
                ),
                "capacity": review_input_capacity,
                "size": review_payload_size(payload),
                "note": (
                    "Shrink review coverage locators only; "
                    "do not redefine layers or layer_order."
                ),
            }
            shrink_size = review_payload_size(shrink_ask)
            if (
                isinstance(review_input_capacity, int)
                and shrink_size > review_input_capacity
            ):
                return _conflict_result(
                    block_id=block_id,
                    rectangles=rectangles,
                    facts=facts,
                    validated=validated,
                    reason_codes=["ask_subset_request_over_capacity"],
                    n=n,
                    m=m,
                    k=k,
                )
            ask_max = _ask_max_rounds()
            if (n + m + k) + 1 + 1 > ask_max:
                return _conflict_result(
                    block_id=block_id,
                    rectangles=rectangles,
                    facts=facts,
                    validated=validated,
                    reason_codes=["ask_budget_exhausted_subset"],
                    n=n,
                    m=m,
                    k=k,
                )
            prompt_out_sub: dict[str, Any] = {}
            subset_exc: Exception | None = None
            subset_reasons: list[str] | None = None
            subset_return: dict[str, Any] | None = None
            try:
                try:
                    raw = review_layer_recognition(
                        shrink_ask, prompt_out=prompt_out_sub
                    )
                except Exception as exc:
                    subset_exc = exc
                    raise
                requested = raw.get("review_locator_ids")
                if not isinstance(requested, list):
                    subset_reasons = ["review_locator_ids_missing"]
                    subset_return = _conflict_result(
                        block_id=block_id,
                        rectangles=rectangles,
                        facts=facts,
                        validated=validated,
                        reason_codes=["shrink_failed"],
                        n=n,
                        m=m + (1 if prompt_out_sub.get("started") else 0),
                        k=k,
                    )
                elif raw.get("already_minimal") is True:
                    subset_reasons = ["already_minimal"]
                    subset_return = _conflict_result(
                        block_id=block_id,
                        rectangles=rectangles,
                        facts=facts,
                        validated=validated,
                        reason_codes=["already_minimal_still_over_capacity"],
                        n=n,
                        m=m + (1 if prompt_out_sub.get("started") else 0),
                        k=k,
                    )
                else:
                    try:
                        review_ids = shrink_review_locator_ids(validated, requested)
                        subset_reasons = []
                    except Exception as exc:
                        subset_reasons = [
                            "shrink_exception:%s" % type(exc).__name__
                        ]
                        subset_return = _conflict_result(
                            block_id=block_id,
                            rectangles=rectangles,
                            facts=facts,
                            validated=validated,
                            reason_codes=["shrink_failed"],
                            n=n,
                            m=m + (1 if prompt_out_sub.get("started") else 0),
                            k=k,
                        )
            finally:
                if prompt_out_sub.get("started"):
                    m += 1
                    if subset_exc is not None:
                        vr = [
                            "ask_subset_exception:%s"
                            % type(subset_exc).__name__
                        ]
                    else:
                        vr = list(subset_reasons or [])
                    _write_started_trace(
                        trace_session=trace_session,
                        call_kind=KIND_ASK_SUBSET,
                        prompt_out=prompt_out_sub,
                        n=n,
                        m=m,
                        k=k,
                        validate_reasons=vr,
                        exception_for_raw=subset_exc,
                        exception_prefix="ask_subset",
                        propose_exc=subset_exc,
                        validate_exc=None,
                    )
            if subset_return is not None:
                return subset_return
            continue

        # 正式复核：路径乙无内部重试；须至少 1 次预算
        ask_max = _ask_max_rounds()
        if (n + m + k) + 1 > ask_max:
            return _conflict_result(
                block_id=block_id,
                rectangles=rectangles,
                facts=facts,
                validated=validated,
                reason_codes=["ask_budget_exhausted_review"],
                n=n,
                m=m,
                k=k,
            )
        prompt_out_rev: dict[str, Any] = {}
        review_exc: Exception | None = None
        review_reasons: list[str] | None = None
        review_return: dict[str, Any] | None = None
        try:
            try:
                raw_review = review_layer_recognition(
                    payload, prompt_out=prompt_out_rev
                )
            except Exception as exc:
                review_exc = exc
                raise
            passed = raw_review.get("passed")
            loc_list = sorted(review_ids) if review_ids else []
            header = layer_result.get("header_completeness")
            header_ok = header == _HEADER_COMPLETE
            if passed is True and header_ok:
                review_reasons = []
                review_return = {
                    "status": "ok",
                    "metrics": portrait_metrics_from_result(
                        block_id, rectangles, facts, validated
                    ),
                    "layer_result": layer_result,
                    "layer_result_empty": False,
                    "draft_retained": True,
                    "ask_rounds_used": n + m + k + (
                        1 if prompt_out_rev.get("started") else 0
                    ),
                    "ask_usage": _usage_dict(
                        n,
                        m,
                        k + (1 if prompt_out_rev.get("started") else 0),
                    ),
                    "rolled_back": False,
                    "review_locator_ids": loc_list or None,
                    "stage_snapshot_kept_draft": True,
                }
            elif not header_ok:
                reason = _header_judgment_text(header)
                model_reason = raw_review.get("reason")
                if model_reason:
                    reason = "%s; model_reason=%s" % (reason, model_reason)
                review_reasons = [reason] if reason else ["reason_missing"]
                review_return = _review_failed_result(
                    block_id=block_id,
                    rectangles=rectangles,
                    facts=facts,
                    rejection_reason=reason,
                    review_locator_ids=loc_list,
                    n=n,
                    m=m,
                    k=k + (1 if prompt_out_rev.get("started") else 0),
                    draft_retained=stage_snapshot["draft"] is draft,
                )
            else:
                model_reason = raw_review.get("reason")
                if model_reason is None or (
                    isinstance(model_reason, str)
                    and model_reason.strip() == ""
                ):
                    review_reasons = ["reason_missing"]
                    rejection = "review_failed"
                else:
                    review_reasons = [str(model_reason)]
                    rejection = str(model_reason)
                review_return = _review_failed_result(
                    block_id=block_id,
                    rectangles=rectangles,
                    facts=facts,
                    rejection_reason=rejection,
                    review_locator_ids=loc_list,
                    n=n,
                    m=m,
                    k=k + (1 if prompt_out_rev.get("started") else 0),
                    draft_retained=stage_snapshot["draft"] is draft,
                )
        finally:
            if prompt_out_rev.get("started"):
                k += 1
                if review_exc is not None:
                    vr = [
                        "review_exception:%s" % type(review_exc).__name__
                    ]
                else:
                    vr = list(review_reasons or [])
                _write_started_trace(
                    trace_session=trace_session,
                    call_kind=KIND_FORMAL_REVIEW,
                    prompt_out=prompt_out_rev,
                    n=n,
                    m=m,
                    k=k,
                    validate_reasons=vr,
                    exception_for_raw=review_exc,
                    exception_prefix="review",
                    propose_exc=review_exc,
                    validate_exc=None,
                )
        if review_return is not None:
            return review_return
