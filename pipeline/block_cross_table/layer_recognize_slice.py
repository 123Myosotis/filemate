# -*- coding: utf-8 -*-
"""A' 提案分片 / 跨片合并（v1.8.0）。片内 cell_texts 不精简；facts 过滤 disjoint。"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.layer_recognize_limits import (
    read_layer_recognize_propose_input_max_chars,
)

PROPOSE_PREFIX = "A' layer recognition. "
MERGE_PREFIX = "A' layer merge. "
SAMPLE_TEXT_MAX = 200
KEEP_RELATIONS_DEFAULT = frozenset({"contains", "overlap", "equal"})

# Top-level keys the model must emit (no contract bump; mirrors validate + MergeResult).
PROPOSE_TOP_LEVEL_KEYS = (
    "rectangle_layer_map",
    "layer_order",
    "group_label_decls",
    "dimension_membership",
    "header_completeness",
)
MERGE_TOP_LEVEL_KEYS = (
    "passed",
    "reason",
    "global_rectangle_layer_map",
    "global_layer_order",
    "group_label_decls",
    "dimension_membership",
    "header_completeness",
    "overall_sanity",
)
_ENVELOPE_WRAPPER_KEYS = frozenset({"answer", "data", "result", "output", "response"})

# Abstract few-shot (5 rects): full coverage + dim entries + contains-consistent order.
PROPOSE_EXAMPLE_INPUT_SKETCH = {
    "rectangles": [
        {"rect_id": "r_group", "kind": "group_label"},
        {"rect_id": "r_field_a", "kind": "field_segment"},
        {"rect_id": "r_field_b", "kind": "field_segment"},
        {"rect_id": "r_merge_outer", "kind": "merge"},
        {"rect_id": "r_merge_inner", "kind": "merge"},
    ],
    "containment_facts": [
        {"a": "r_field_a", "b": "r_merge_inner", "relation": "contains"}
    ],
    "dimensions_by_subtable": {"0": ["d_header", "d_body"]},
}
PROPOSE_EXAMPLE_LEGAL = {
    "rectangle_layer_map": {
        "r_group": "L1",
        "r_field_a": "L1",
        "r_field_b": "L2",
        "r_merge_outer": "L1",
        "r_merge_inner": "L2",
    },
    "layer_order": ["L1", "L2"],
    "group_label_decls": [
        {"field_id": "f_group", "kind": "not_a_layer", "layer_ids": []}
    ],
    "dimension_membership": [
        {
            "dimension_field_id": "d_header",
            "subtable_id": "0",
            "layer_ids": ["L1"],
        },
        {
            "dimension_field_id": "d_body",
            "subtable_id": "0",
            "layer_ids": [],
        },
    ],
    "header_completeness": "complete",
}
MERGE_EXAMPLE_LEGAL = {
    "passed": True,
    "reason": "ok",
    "global_rectangle_layer_map": {
        "r_group": "G1",
        "r_field_a": "G1",
        "r_field_b": "G2",
        "r_merge_outer": "G1",
        "r_merge_inner": "G2",
    },
    "global_layer_order": ["G1", "G2"],
    "group_label_decls": [],
    "dimension_membership": [
        {
            "dimension_field_id": "d_header",
            "subtable_id": "0",
            "layer_ids": ["G1"],
        },
        {
            "dimension_field_id": "d_body",
            "subtable_id": "0",
            "layer_ids": [],
        },
    ],
    "header_completeness": "complete",
    "overall_sanity": {"passed": True, "reason": "coherent"},
}

_CRITICAL_COVERAGE = (
    "CRITICAL — Coverage requirement: Every rect_id in the input `rectangles` array "
    "MUST appear as a key in `rectangle_layer_map`. Partial maps are invalid. "
    "If a rect_id looks like noise, you still must assign it to some layer; "
    "do NOT omit any rect_id."
)
_CRITICAL_DIM = (
    "CRITICAL — Dimension coverage: For EVERY (subtable_id, dimension_field_id) pair "
    "in `dimensions_by_subtable`, `dimension_membership` MUST contain exactly one entry. "
    "If the dimension does not belong to any layer, the entry still must exist with "
    "`layer_ids: []` (empty list, legal). Missing entries are invalid."
)
_CRITICAL_ORDER = (
    "CRITICAL — Layer order rule: For each containment_fact with relation='contains' "
    "(a contains b), the layer assigned to `a` MUST appear BEFORE the layer assigned "
    "to `b` in `layer_order`. `a` is the outer/broader rectangle; `b` is the "
    "inner/narrower one. Violating this makes your output invalid."
)
_CRITICAL_GROUP = (
    "CRITICAL — Group label semantic: When you declare a group_label_decl with "
    "kind='exact_one_layer', the layer you reference MUST contain ONLY the rectangles "
    "geometrically inside that group label's field columns AND in the top band "
    "(rows where the label physically lives). Do NOT point to a layer that spans the "
    "entire subtable. If the group label does not correspond to a specific layer, use "
    "kind='not_a_layer' instead. The union envelope of all rectangles in the referenced "
    "layer MUST equal the group label's column range AND stay within the top band."
)

# Contrast examples for exact_one_layer vs not_a_layer (abstract ids only).
PROPOSE_GROUP_LABEL_CONTRAST = {
    "exact_one_layer_ok": {
        "note": (
            "Layer Lg contains only the group-label top-band rects for that field; "
            "union envelope equals the group label column range in the top band."
        ),
        "group_label_decls": [
            {
                "field_id": "f_group",
                "kind": "exact_one_layer",
                "layer_ids": ["Lg"],
            }
        ],
        "rectangle_layer_map_fragment": {
            "r_group": "Lg",
            "r_field_under_group": "Lg",
        },
    },
    "not_a_layer_ok": {
        "note": (
            "Group label is not itself a layer; declare not_a_layer and still map "
            "every rect_id onto other layers."
        ),
        "group_label_decls": [
            {"field_id": "f_group", "kind": "not_a_layer", "layer_ids": []}
        ],
    },
    "exact_one_layer_wrong": {
        "note": (
            "WRONG: pointing exact_one_layer at a layer that spans the whole subtable."
        ),
        "group_label_decls": [
            {
                "field_id": "f_group",
                "kind": "exact_one_layer",
                "layer_ids": ["L_whole_sheet"],
            }
        ],
    },
}

# For chat_json(..., system=...); client already sends response_format=json_object.
LAYER_RECOGNIZE_JSON_SYSTEM = (
    "Return ONE JSON object only. "
    "The top-level MUST be the required keys exactly, not wrapped inside any other key. "
    'Do NOT wrap in {"answer": ...} or any other envelope. '
    + _CRITICAL_COVERAGE
    + " "
    + _CRITICAL_DIM
    + " "
    + _CRITICAL_ORDER
    + " "
    + _CRITICAL_GROUP
    + " ILLUSTRATIVE few-shot only (do NOT copy example rect ids unless they appear "
    "in THIS request rectangles): "
    + json.dumps(
        {
            "example_input_sketch": PROPOSE_EXAMPLE_INPUT_SKETCH,
            "example_legal_output": PROPOSE_EXAMPLE_LEGAL,
            "group_label_contrast": PROPOSE_GROUP_LABEL_CONTRAST,
        },
        ensure_ascii=False,
    )
)


def propose_output_contract() -> dict[str, Any]:
    """① schema + CRITICAL rules; ② abstract few-shot (no business words)."""
    keys = ", ".join(PROPOSE_TOP_LEVEL_KEYS)
    return {
        "system_rules": (
            "Return ONE JSON object. The top-level MUST be these keys exactly, "
            "not wrapped inside any other key: "
            + keys
            + '. Do NOT wrap in {"answer": ...} or any other envelope. '
            + _CRITICAL_COVERAGE
            + " "
            + _CRITICAL_DIM
            + " "
            + _CRITICAL_ORDER
            + " "
            + _CRITICAL_GROUP
            + " layer_order MUST list every layer_id that appears in rectangle_layer_map. "
            "header_completeness must be one of complete|incomplete|not_header. "
            "ILLUSTRATIVE ONLY — use rect_id / dimension_field_id values from THIS request, "
            "never copy example ids unless they literally appear in the input."
        ),
        "top_level_keys_exactly": list(PROPOSE_TOP_LEVEL_KEYS),
        "forbid_wrappers": sorted(_ENVELOPE_WRAPPER_KEYS),
        "example_is_illustrative_only": True,
        "example_input_sketch": dict(PROPOSE_EXAMPLE_INPUT_SKETCH),
        "example_legal_output": dict(PROPOSE_EXAMPLE_LEGAL),
        "group_label_contrast": dict(PROPOSE_GROUP_LABEL_CONTRAST),
        "mapping_rule": (
            "rectangle_layer_map keys MUST equal the set of rect_id from rectangles "
            "in this request. Empty or partial maps are forbidden."
        ),
        "dimension_rule": (
            "Emit one dimension_membership entry per (subtable_id, dimension_field_id) "
            "in dimensions_by_subtable; layer_ids may be []."
        ),
        "contains_order_rule": (
            "For relation=contains (a contains b): index(layer_of_a) < index(layer_of_b) "
            "in layer_order."
        ),
        "group_label_rule": (
            "exact_one_layer => referenced layer union equals group-label top-band "
            "column envelope; otherwise use not_a_layer."
        ),
    }


def merge_output_contract() -> dict[str, Any]:
    keys = ", ".join(MERGE_TOP_LEVEL_KEYS)
    return {
        "system_rules": (
            "Return ONE JSON object. The top-level MUST be these keys exactly, "
            "not wrapped inside any other key: "
            + keys
            + '. Do NOT wrap in {"answer": ...} or any other envelope. '
            "CRITICAL — Coverage: every rect_id across all slices MUST appear in "
            "global_rectangle_layer_map. "
            "CRITICAL — Dimension coverage: emit dimension_membership covering every "
            "required dimension pair; layer_ids may be []. "
            "CRITICAL — Layer order: for contains(a,b), layer of a before layer of b "
            "in global_layer_order. "
            "ILLUSTRATIVE ONLY — do not copy example rect ids."
        ),
        "top_level_keys_exactly": list(MERGE_TOP_LEVEL_KEYS),
        "forbid_wrappers": sorted(_ENVELOPE_WRAPPER_KEYS),
        "example_is_illustrative_only": True,
        "example_legal_output": dict(MERGE_EXAMPLE_LEGAL),
        "mapping_rule": (
            "global_rectangle_layer_map keys MUST cover rect_ids from all slices "
            "in this request. Empty or partial maps are forbidden."
        ),
    }


def is_illicit_propose_envelope(raw: Any) -> bool:
    """Detect answer/data/... wrapper. Does NOT unwrap (policy A)."""
    if not isinstance(raw, dict):
        return False
    if "rectangle_layer_map" in raw:
        return False
    for key in _ENVELOPE_WRAPPER_KEYS:
        if key not in raw:
            continue
        inner = raw[key]
        if isinstance(inner, dict) and "rectangle_layer_map" in inner:
            return True
        # lone wrapper (e.g. {"answer": "Your answer here"})
        if len(raw) == 1:
            return True
    return False


def is_illicit_merge_envelope(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    if "global_rectangle_layer_map" in raw or "passed" in raw:
        return False
    for key in _ENVELOPE_WRAPPER_KEYS:
        if key not in raw:
            continue
        inner = raw[key]
        if isinstance(inner, dict) and (
            "global_rectangle_layer_map" in inner or "passed" in inner
        ):
            return True
        if len(raw) == 1:
            return True
    return False


def read_propose_slice_relation_keep(
    contract_data: Optional[Mapping[str, Any]] = None,
) -> frozenset[str]:
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
    enums = block.get("enums") or {}
    keep = enums.get("propose_slice_relation_keep")
    if not isinstance(keep, list) or not keep:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "propose_slice_relation_keep missing",
        )
    out = frozenset(str(x) for x in keep)
    if not out:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "propose_slice_relation_keep empty",
        )
    return out


def filter_containment_facts_for_propose(
    facts: list[dict[str, str]],
    *,
    keep: Optional[frozenset[str]] = None,
    contract_data: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, str]]:
    allowed = keep if keep is not None else read_propose_slice_relation_keep(contract_data)
    return [
        f
        for f in facts
        if isinstance(f, dict) and str(f.get("relation") or "") in allowed
    ]


def sort_rectangles_geometric(
    rectangles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    def key(r: dict[str, Any]):
        loc = r.get("locator") or {}
        return (
            int(loc.get("r0", 0)),
            int(loc.get("c0", 0)),
            int(loc.get("r1", 0)),
            int(loc.get("c1", 0)),
            str(r.get("rect_id") or ""),
        )

    return sorted(rectangles, key=key)


def _rect_payload(rectangles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "rect_id": r["rect_id"],
            "kind": r["kind"],
            "locator": r["locator"],
            "back_ref": r["back_ref"],
        }
        for r in rectangles
    ]


def build_propose_payload(
    *,
    user_utterance: str,
    rectangles: list[dict[str, Any]],
    containment_facts: list[dict[str, str]],
    cell_texts: dict[str, list[dict[str, Any]]],
    draft: dict[str, Any] | None,
    dimensions_for_block: dict[Any, list[str]] | None,
    subtable_boundaries: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    from pipeline.block_cross_table.layer_recognize import project_subtable_boundaries

    # Order: ① output_contract (rules+example) then ③ task payload.
    return {
        "output_contract": propose_output_contract(),
        "user_utterance": user_utterance,
        "rectangles": _rect_payload(rectangles),
        "containment_facts": containment_facts,
        "subtable_boundaries": subtable_boundaries
        if subtable_boundaries is not None
        else project_subtable_boundaries(draft),
        "cell_texts": cell_texts,
        "group_field_ids": [
            f.get("field_id")
            for f in (draft or {}).get("fields") or []
            if f.get("role") == "group"
        ],
        "dimensions_by_subtable": dimensions_for_block or {},
        "instructions": (
            "Emit the reply object at the TOP LEVEL using output_contract.top_level_keys_exactly. "
            "Obey CRITICAL rules in output_contract.system_rules "
            "(coverage, dimension coverage, contains layer order, group label semantic). "
            "Follow mapping_rule, dimension_rule, contains_order_rule, group_label_rule. "
            "example_legal_output / example_input_sketch / group_label_contrast "
            "are ILLUSTRATIVE ONLY. "
            "Field semantics: "
            "rectangle_layer_map (object rect_id->layer_id; keys = every rectangles[].rect_id; "
            "many rects may share one layer_id), "
            "layer_order (array of layer_id; outer/contains-a before inner/contains-b), "
            "group_label_decls (array of {field_id, kind one of "
            "exact_one_layer|spans_multiple_layers|not_a_layer, layer_ids}), "
            "dimension_membership (one entry per dimensions_by_subtable pair; "
            "layer_ids may be []), "
            "header_completeness (one of complete|incomplete|not_header; "
            "judge using cell_texts on each layer; must be complete to succeed). "
            "Do not use ordinals as the order source; layer_order is authoritative. "
            'Do NOT nest the reply under "answer" or any other wrapper key.'
        ),
    }


def estimate_propose_prompt_chars(payload: dict[str, Any]) -> int:
    return len(PROPOSE_PREFIX + json.dumps(payload, ensure_ascii=False))


def build_slice_propose_payload(
    *,
    user_utterance: str,
    window_rects: list[dict[str, Any]],
    facts_model: list[dict[str, str]],
    texts_all: dict[str, list[dict[str, Any]]],
    draft: dict[str, Any] | None,
    dimensions_for_block: dict[Any, list[str]] | None,
    subtable_boundaries: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    ids = {r["rect_id"] for r in window_rects}
    facts = [
        f
        for f in facts_model
        if f.get("a") in ids and f.get("b") in ids
    ]
    texts = {rid: texts_all[rid] for rid in ids if rid in texts_all}
    return build_propose_payload(
        user_utterance=user_utterance,
        rectangles=window_rects,
        containment_facts=facts,
        cell_texts=texts,
        draft=draft,
        dimensions_for_block=dimensions_for_block,
        subtable_boundaries=subtable_boundaries,
    )


class SlicePlan:
    __slots__ = ("slice_index", "rectangles", "rect_ids")

    def __init__(
        self, slice_index: int, rectangles: list[dict[str, Any]]
    ) -> None:
        self.slice_index = slice_index
        self.rectangles = rectangles
        self.rect_ids = [r["rect_id"] for r in rectangles]


def plan_propose_slices(
    *,
    rectangles: list[dict[str, Any]],
    facts_model: list[dict[str, str]],
    texts_all: dict[str, list[dict[str, Any]]],
    user_utterance: str,
    draft: dict[str, Any] | None,
    dimensions_for_block: dict[Any, list[str]] | None,
    subtable_boundaries: list[dict[str, Any]] | None,
    max_chars: Optional[int] = None,
    contract_data: Optional[Mapping[str, Any]] = None,
) -> list[SlicePlan]:
    """容量驱动贪心开窗。单矩形超容量 → CrossTableError 带 propose_single_rect_over_capacity。"""
    cap = (
        max_chars
        if max_chars is not None
        else read_layer_recognize_propose_input_max_chars(contract_data)
    )
    ordered = sort_rectangles_geometric(rectangles)
    plans: list[SlicePlan] = []
    current: list[dict[str, Any]] = []
    idx = 1

    def fits(window: list[dict[str, Any]]) -> bool:
        payload = build_slice_propose_payload(
            user_utterance=user_utterance,
            window_rects=window,
            facts_model=facts_model,
            texts_all=texts_all,
            draft=draft,
            dimensions_for_block=dimensions_for_block,
            subtable_boundaries=subtable_boundaries,
        )
        return estimate_propose_prompt_chars(payload) <= cap

    for rect in ordered:
        trial = current + [rect]
        if fits(trial):
            current = trial
            continue
        if current:
            plans.append(SlicePlan(idx, list(current)))
            idx += 1
            current = [rect]
            if not fits(current):
                raise CrossTableError(
                    "cross_table_proposal_invalid",
                    "propose_single_rect_over_capacity",
                    {
                        "reason_codes": ["propose_single_rect_over_capacity"],
                        "rect_id": rect.get("rect_id"),
                        "max_chars": cap,
                    },
                )
        else:
            raise CrossTableError(
                "cross_table_proposal_invalid",
                "propose_single_rect_over_capacity",
                {
                    "reason_codes": ["propose_single_rect_over_capacity"],
                    "rect_id": rect.get("rect_id"),
                    "max_chars": cap,
                },
            )
    if current:
        plans.append(SlicePlan(idx, list(current)))
    return plans


def _first_row_text(cells: list[dict[str, Any]]) -> str:
    if not cells:
        return ""
    rows: dict[int, list[str]] = {}
    for c in cells:
        if not isinstance(c, dict):
            continue
        r = int(c.get("r", 0))
        text = "" if c.get("text") is None else str(c.get("text"))
        rows.setdefault(r, []).append(text)
    if not rows:
        return ""
    r0 = min(rows.keys())
    return "".join(rows[r0]).strip()


def _first_nonempty_cell(cells: list[dict[str, Any]]) -> str:
    for c in cells:
        if not isinstance(c, dict):
            continue
        text = "" if c.get("text") is None else str(c.get("text")).strip()
        if text:
            return text
    return ""


def _clip200(s: str) -> str:
    if len(s) <= SAMPLE_TEXT_MAX:
        return s
    return s[:SAMPLE_TEXT_MAX]


def build_layer_summaries_for_merge(
    *,
    layer_order: list[str],
    rectangle_layer_map: dict[str, str],
    window_rects: list[dict[str, Any]],
    texts_all: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    by_id = {r["rect_id"]: r for r in window_rects}
    out: list[dict[str, Any]] = []
    for lid in layer_order:
        rect_ids = [
            rid
            for rid, mapped in rectangle_layer_map.items()
            if mapped == lid and rid in by_id
        ]
        ordered = sort_rectangles_geometric(
            [by_id[rid] for rid in rect_ids if rid in by_id]
        )
        take = ordered[:3]
        samples: list[str] = []
        seen: set[str] = set()
        for r in take:
            t = _clip200(_first_row_text(texts_all.get(r["rect_id"]) or []))
            if t and t not in seen:
                seen.add(t)
                samples.append(t)
        if not samples:
            for r in take:
                t = _clip200(_first_nonempty_cell(texts_all.get(r["rect_id"]) or []))
                if t and t not in seen:
                    seen.add(t)
                    samples.append(t)
        entry: dict[str, Any] = {
            "layer_id": lid,
            "rect_ids": [r["rect_id"] for r in ordered],
            "sample_texts": samples,
        }
        if not samples:
            entry["empty_text_ratio"] = 1.0
        out.append(entry)
    return out


def build_header_band_texts_for_merge(
    *,
    window_rects: list[dict[str, Any]],
    texts_all: dict[str, list[dict[str, Any]]],
) -> dict[str, str]:
    if not window_rects:
        return {}
    min_r0 = min(int((r.get("locator") or {}).get("r0", 0)) for r in window_rects)
    band: dict[str, str] = {}
    for r in window_rects:
        kind = str(r.get("kind") or "")
        loc = r.get("locator") or {}
        # group_label (契约名) 或上游 kind=group / 顶带行
        if kind in ("group_label", "group") or int(loc.get("r0", -1)) == min_r0:
            rid = r["rect_id"]
            cells = texts_all.get(rid) or []
            text = "".join(
                "" if c.get("text") is None else str(c.get("text"))
                for c in cells
                if isinstance(c, dict)
            )
            band[rid] = text
    return band


def _boundary_rect_ids(
    *,
    window_ids: set[str],
    all_plans: list[SlicePlan],
    facts_all: list[dict[str, str]],
) -> list[str]:
    other_ids: set[str] = set()
    for p in all_plans:
        for rid in p.rect_ids:
            if rid not in window_ids:
                other_ids.add(rid)
    boundary: set[str] = set()
    for f in facts_all:
        if not isinstance(f, dict):
            continue
        rel = str(f.get("relation") or "")
        if rel not in ("contains", "overlap"):
            continue
        a, b = str(f.get("a") or ""), str(f.get("b") or "")
        if a in window_ids and b in other_ids:
            boundary.add(a)
        if b in window_ids and a in other_ids:
            boundary.add(b)
    return sorted(boundary)


def cross_slice_contains_candidates(
    *,
    plans: list[SlicePlan],
    facts_all: list[dict[str, str]],
) -> list[dict[str, str]]:
    slice_of: dict[str, int] = {}
    for p in plans:
        for rid in p.rect_ids:
            slice_of[rid] = p.slice_index
    out: list[dict[str, str]] = []
    for f in facts_all:
        if not isinstance(f, dict) or f.get("relation") != "contains":
            continue
        a, b = str(f.get("a") or ""), str(f.get("b") or "")
        if a not in slice_of or b not in slice_of:
            continue
        if slice_of[a] != slice_of[b]:
            out.append({"a": a, "b": b, "relation": "contains"})
    return out


def _cells_obj(
    rect_ids: list[str], texts_all: dict[str, list[dict[str, Any]]]
) -> dict[str, list[dict[str, Any]]]:
    return {rid: texts_all.get(rid) or [] for rid in rect_ids}


def build_merge_slice_summary(
    *,
    plan: SlicePlan,
    all_plans: list[SlicePlan],
    checked: dict[str, Any],
    facts_all: list[dict[str, str]],
    texts_all: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    window_ids = set(plan.rect_ids)
    boundary = _boundary_rect_ids(
        window_ids=window_ids, all_plans=all_plans, facts_all=facts_all
    )
    layer_order = list(checked.get("layer_order") or [])
    rect_map = dict(checked.get("rectangle_layer_map") or {})
    return {
        "slice_index": plan.slice_index,
        "rect_ids": list(plan.rect_ids),
        "boundary_rect_ids": boundary,
        "layer_order": layer_order,
        "rectangle_layer_map": rect_map,
        "header_completeness_slice": str(
            checked.get("header_completeness") or ""
        ),
        "cell_texts_boundary": _cells_obj(boundary, texts_all),
        "layer_summaries": build_layer_summaries_for_merge(
            layer_order=layer_order,
            rectangle_layer_map=rect_map,
            window_rects=plan.rectangles,
            texts_all=texts_all,
        ),
        "header_band_texts": build_header_band_texts_for_merge(
            window_rects=plan.rectangles, texts_all=texts_all
        ),
    }


def estimate_merge_prompt_chars(merge_input: dict[str, Any]) -> int:
    return len(MERGE_PREFIX + json.dumps(merge_input, ensure_ascii=False, default=str))


def trim_merge_input_to_capacity(
    merge_input: dict[str, Any],
    capacity: int,
) -> tuple[dict[str, Any], int]:
    """返回 (trimmed_input, trim_level)。level 0=未裁；1..3=裁剪级；仍超则抛 conflict 语义。"""
    cur = json.loads(json.dumps(merge_input, ensure_ascii=False, default=str))
    if estimate_merge_prompt_chars(cur) <= capacity:
        return cur, 0

    # level 1: boundary → only contains candidate endpoints
    cand_ids: set[str] = set()
    for f in cur.get("cross_slice_contains_candidates") or []:
        if isinstance(f, dict):
            cand_ids.add(str(f.get("a") or ""))
            cand_ids.add(str(f.get("b") or ""))
    for sl in cur.get("slices") or []:
        if not isinstance(sl, dict):
            continue
        boundary = sl.get("cell_texts_boundary") or {}
        if not isinstance(boundary, dict):
            continue
        kept = {
            rid: boundary[rid]
            for rid in boundary
            if rid in cand_ids
        }
        if not kept and boundary:
            # keep one arbitrary to avoid {}
            first = next(iter(boundary))
            kept = {first: boundary[first]}
        sl["cell_texts_boundary"] = kept
    if estimate_merge_prompt_chars(cur) <= capacity:
        return cur, 1

    # level 2: header_band_texts keep first 3 geometric per slice
    for sl in cur.get("slices") or []:
        if not isinstance(sl, dict):
            continue
        band = sl.get("header_band_texts") or {}
        if not isinstance(band, dict) or len(band) <= 3:
            continue
        # order by rect_id as stable proxy when locator not in band
        keys = sorted(band.keys())[:3]
        sl["header_band_texts"] = {k: band[k] for k in keys}
    if estimate_merge_prompt_chars(cur) <= capacity:
        return cur, 2

    # level 3: sample_texts → 1 each
    for sl in cur.get("slices") or []:
        if not isinstance(sl, dict):
            continue
        for entry in sl.get("layer_summaries") or []:
            if not isinstance(entry, dict):
                continue
            samples = entry.get("sample_texts") or []
            if isinstance(samples, list) and len(samples) > 1:
                entry["sample_texts"] = samples[:1]
    if estimate_merge_prompt_chars(cur) <= capacity:
        return cur, 3

    raise CrossTableError(
        "cross_table_proposal_invalid",
        "cross_slice_merge_input_over_capacity",
        {
            "reason_codes": ["cross_slice_merge_input_over_capacity"],
            "size": estimate_merge_prompt_chars(cur),
            "capacity": capacity,
            "merge_input_trim_level": 3,
        },
    )


def build_merge_input(
    *,
    plans: list[SlicePlan],
    slice_checked: list[dict[str, Any]],
    facts_all: list[dict[str, str]],
    texts_all: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    slices = [
        build_merge_slice_summary(
            plan=p,
            all_plans=plans,
            checked=checked,
            facts_all=facts_all,
            texts_all=texts_all,
        )
        for p, checked in zip(plans, slice_checked)
    ]
    return {
        "output_contract": merge_output_contract(),
        "slices": slices,
        "cross_slice_contains_candidates": cross_slice_contains_candidates(
            plans=plans, facts_all=facts_all
        ),
        "instructions": (
            "Cross-slice layer merge. Emit the reply object at the TOP LEVEL using "
            "output_contract.top_level_keys_exactly. Follow output_contract.system_rules "
            "and output_contract.mapping_rule. "
            "example_legal_output is ILLUSTRATIVE ONLY — do not copy its placeholder rect ids. "
            "Field semantics: passed (bool), reason (string), "
            "global_rectangle_layer_map, global_layer_order, group_label_decls, "
            "dimension_membership, header_completeness (re-judge whole block using "
            "header_band_texts), overall_sanity {passed:bool, reason:non-empty string}. "
            "Do not locally assume same local layer_id means same global layer; decide "
            "using layer_summaries sample_texts. overall_sanity must always include reason. "
            'Do NOT nest the reply under "answer" or any other wrapper key.'
        ),
    }


def merge_layer_recognition(
    merge_payload: dict[str, Any],
    *,
    prompt_out: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from pipeline.block_cross_table.api import _one_model_call

    prompt = MERGE_PREFIX + json.dumps(merge_payload, ensure_ascii=False, default=str)
    if prompt_out is not None:
        prompt_out["prompt"] = prompt
        prompt_out["prompt_shape"] = {
            "rectangles": sum(
                len(s.get("rect_ids") or [])
                for s in (merge_payload.get("slices") or [])
                if isinstance(s, dict)
            ),
            "containment_facts": len(
                merge_payload.get("cross_slice_contains_candidates") or []
            ),
            "cell_texts_total_chars": 0,
            "subtable_boundaries": 0,
            "group_field_ids": 0,
            "dimensions": 0,
        }
        prompt_out["started"] = True
    raw = _one_model_call(prompt)
    if prompt_out is not None:
        prompt_out["raw"] = raw
        prompt_out["raw_received"] = True
    if not isinstance(raw, dict):
        raise CrossTableError(
            "cross_table_proposal_invalid", "layer merge not object"
        )
    return raw


def validate_merge_result(
    *,
    raw: dict[str, Any],
    rectangles: list[dict[str, Any]],
    facts_all: list[dict[str, str]],
    draft: dict[str, Any] | None,
    dimensions_for_block: dict[Any, list[str]] | None,
) -> dict[str, Any]:
    """返回 {ok_status, ...}：ok / conflict / review_failed。"""
    from pipeline.block_cross_table.layer_recognize import (
        _HEADER_COMPLETE,
        _header_judgment_text,
        validate_layer_proposal,
    )

    # Policy A: detect illicit envelope; never unwrap.
    if is_illicit_merge_envelope(raw):
        return {
            "ok_status": "conflict",
            "reason_codes": ["envelope_wrapper_forbidden"],
            "rejection_reason": "envelope_wrapper_forbidden",
        }

    if raw.get("passed") is not True:
        reason = raw.get("reason")
        if reason is None or (isinstance(reason, str) and reason.strip() == ""):
            reason = "reason_missing"
        return {
            "ok_status": "conflict",
            "reason_codes": ["cross_slice_layer_unresolved"],
            "rejection_reason": str(reason),
        }

    sanity = raw.get("overall_sanity")
    if not isinstance(sanity, dict):
        return {
            "ok_status": "conflict",
            "reason_codes": ["cross_slice_merge_invalid"],
            "rejection_reason": "overall_sanity missing",
        }
    s_reason = sanity.get("reason")
    if s_reason is None or (
        isinstance(s_reason, str) and str(s_reason).strip() == ""
    ):
        return {
            "ok_status": "conflict",
            "reason_codes": ["cross_slice_merge_invalid"],
            "rejection_reason": "overall_sanity.reason empty",
        }
    if sanity.get("passed") is False:
        return {
            "ok_status": "review_failed",
            "reason_codes": ["cross_slice_overall_sanity_failed"],
            "rejection_reason": str(s_reason),
        }

    proposal = {
        "rectangle_layer_map": raw.get("global_rectangle_layer_map"),
        "layer_order": raw.get("global_layer_order"),
        "group_label_decls": raw.get("group_label_decls") or [],
        "dimension_membership": raw.get("dimension_membership") or [],
        "header_completeness": raw.get("header_completeness"),
    }
    checked = validate_layer_proposal(
        rectangles, facts_all, proposal, draft, dimensions_for_block
    )
    if not checked.get("ok"):
        return {
            "ok_status": "conflict",
            "reason_codes": ["cross_slice_merge_invalid"],
            "rejection_reason": ",".join(checked.get("reasons") or []),
            "validate_reasons": list(checked.get("reasons") or []),
        }

    header = checked.get("header_completeness")
    if header != _HEADER_COMPLETE:
        return {
            "ok_status": "review_failed",
            "reason_codes": ["header_incomplete"],
            "rejection_reason": _header_judgment_text(header),
            "validated": checked,
        }

    return {"ok_status": "ok", "validated": checked, "raw": raw}
