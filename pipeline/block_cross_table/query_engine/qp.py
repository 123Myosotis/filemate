# -*- coding: utf-8 -*-
"""B4 QP: input assemble + AST validate. AI substituted by plan fixture."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .errors import QueryEngineError
from .op_catalog import (
    ARG_EXTREMUM_OP_ID,
    GROUP_TOP_K_OP_ID,
    OpCatalogError,
    SORT_TRUNCATE_OP_ID,
    validate_op_catalog,
    validate_plan_ops_linkable,
)
from .result_projection import validate_result_projection

FAIL_LOCUS_OTHER = "other"
FAIL_LOCUS_GROUP_LATEST = "group_latest"
FAIL_LOCUS_PROJECTION = "projection"
FAIL_LOCUS_TIME_WINDOW = "time_window"
FAIL_LOCUS_EXISTENCE = "existence"

ROLLBACK_QUERY_PLANNER_START = "query_planner_start"

_H_OPS = frozenset({GROUP_TOP_K_OP_ID, ARG_EXTREMUM_OP_ID})
_WINDOW_OPS = frozenset({"op_stamp_window", "op_interval_build"})


def load_plan_fixture(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise QueryEngineError("plan fixture must be object", fail_locus=FAIL_LOCUS_OTHER)
    return data


def _validate_dag_type_links(
    catalog: dict[str, Any],
    steps: list[dict[str, Any]],
) -> None:
    """Producer out_type must be accepted by consumer in_types (src:* → table)."""
    ops = catalog["operators"]
    handle_out: dict[str, str] = {}
    for step in steps:
        op_id = str(step.get("op") or "")
        spec = ops[op_id]
        out_types = list(spec.get("out_types") or [])
        out_t = str(out_types[0]) if out_types else "table"
        output = str(step.get("output") or step.get("step_id") or "")
        if output:
            handle_out[output] = out_t
        for ref in step.get("inputs") or []:
            ref_s = str(ref)
            if ref_s.startswith("src:"):
                produced = "table"
            elif ref_s in handle_out:
                produced = handle_out[ref_s]
            else:
                # Forward ref / unknown — skip (exec will fail); types check known producers.
                continue
            in_types = list(spec.get("in_types") or [])
            if in_types and produced not in in_types:
                raise QueryEngineError(
                    "type mismatch: handle %s produces %s, op %s accepts %s"
                    % (ref_s, produced, op_id, in_types),
                    fail_locus=FAIL_LOCUS_OTHER,
                    detail={"input": ref_s, "produced": produced, "op": op_id},
                )


def assemble_qp_input(
    *,
    utterance: str,
    portraits: list[dict[str, Any]],
    catalog: dict[str, Any],
    ask_history: list[dict[str, Any]] | None = None,
    confirmed_bindings: list[dict[str, Any]] | None = None,
    query_time_basis_candidates: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble QP input envelope (no AI)."""
    return {
        "utterance": utterance,
        "portraits": portraits,
        "operator_catalog_op_ids": list((catalog.get("operators") or {}).keys()),
        "ask_history": list(ask_history or []),
        "confirmed_bindings": list(confirmed_bindings or []),
        "query_time_basis_candidates": list(query_time_basis_candidates or []),
    }


def _provenance_ok(prov: Any) -> bool:
    if not isinstance(prov, dict):
        return False
    return bool(prov.get("utterance_span") or prov.get("portrait_path"))


def _collect_condition_kinds(node: Any, out: set[str]) -> None:
    if not isinstance(node, dict):
        return
    kind = node.get("node_kind")
    if kind:
        out.add(str(kind))
    for child in node.get("children") or []:
        _collect_condition_kinds(child, out)


def validate_query_plan_ast(
    plan: Mapping[str, Any],
    catalog: dict[str, Any],
    *,
    require_full_capability_cover: bool = True,
) -> dict[str, Any]:
    """
    Validate QueryPlanAst against catalog + structural hard rules.
    On success returns {"status": "plan_ready", "plan": plan}.
    """
    try:
        validate_op_catalog(catalog)
    except OpCatalogError as exc:
        raise QueryEngineError(
            str(exc),
            fail_locus="budget" if "complexity_bound" in str(exc) else FAIL_LOCUS_OTHER,
        ) from exc
    ops = catalog["operators"]

    status = plan.get("status")
    if status == "needs_user":
        return {"status": "needs_user", "plan": dict(plan)}

    if status not in (None, "plan_ready"):
        raise QueryEngineError(
            "plan status invalid: %s" % status,
            fail_locus=FAIL_LOCUS_OTHER,
        )

    basis = plan.get("query_time_basis")
    if not basis:
        raise QueryEngineError(
            "query_time_basis not selected",
            fail_locus=FAIL_LOCUS_OTHER,
        )

    anchors = plan.get("rollback_anchors")
    if not isinstance(anchors, list) or ROLLBACK_QUERY_PLANNER_START not in anchors:
        raise QueryEngineError(
            "rollback_anchors must include query_planner_start",
            fail_locus=FAIL_LOCUS_OTHER,
        )

    projection = plan.get("result_projection")
    if not isinstance(projection, dict):
        raise QueryEngineError(
            "result_projection missing",
            fail_locus=FAIL_LOCUS_PROJECTION,
        )
    validate_result_projection(projection)

    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise QueryEngineError("steps missing", fail_locus=FAIL_LOCUS_OTHER)

    for step in steps:
        if not isinstance(step, dict):
            raise QueryEngineError("step must be object", fail_locus=FAIL_LOCUS_OTHER)
        op_id = step.get("op")
        if op_id == "op_result_projection" or op_id == "result_projection":
            raise QueryEngineError(
                "result_projection cannot be a plan step op",
                fail_locus=FAIL_LOCUS_PROJECTION,
            )
        if op_id not in ops:
            raise QueryEngineError(
                "unknown op: %s" % op_id,
                fail_locus=FAIL_LOCUS_OTHER,
                detail={"op": op_id},
            )
        bound = ops[op_id].get("complexity_bound")
        if not isinstance(bound, dict) or not bound.get("class"):
            raise QueryEngineError(
                "op missing complexity_bound: %s" % op_id,
                fail_locus="budget",
            )
        if not _provenance_ok(step.get("provenance")):
            raise QueryEngineError(
                "step provenance empty/unresolvable: %s" % step.get("step_id"),
                fail_locus=FAIL_LOCUS_OTHER,
                detail={"step_id": step.get("step_id")},
            )

    # Op membership + group_latest rules (DAG: skip linear type chain).
    try:
        validate_plan_ops_linkable(catalog, list(steps), linear_type_check=False)
    except OpCatalogError as exc:
        locus = getattr(exc, "fail_locus", FAIL_LOCUS_OTHER)
        msg = str(exc)
        if "group_latest" in msg:
            locus = FAIL_LOCUS_GROUP_LATEST
        raise QueryEngineError(msg, fail_locus=locus) from exc

    # Edge/handle type compatibility (DAG).
    _validate_dag_type_links(catalog, list(steps))

    # Ban bare sort_truncate claiming group latest (also covered by linkable).
    for step in steps:
        if step.get("claims_group_latest") and step.get("op") == SORT_TRUNCATE_OP_ID:
            if step.get("rewrite_of") not in (
                GROUP_TOP_K_OP_ID,
                ARG_EXTREMUM_OP_ID,
                "GroupTopK",
                "ArgExtremum",
            ):
                raise QueryEngineError(
                    "sort_truncate cannot alone express group_latest",
                    fail_locus=FAIL_LOCUS_GROUP_LATEST,
                )

    op_ids = {str(s.get("op")) for s in steps}
    edges = plan.get("edges")
    if not isinstance(edges, list):
        raise QueryEngineError("edges must be list", fail_locus=FAIL_LOCUS_OTHER)

    condition_kinds: set[str] = set()
    trees = plan.get("condition_trees") or {}
    if isinstance(trees, dict):
        for tree in trees.values():
            _collect_condition_kinds(tree, condition_kinds)
    for step in steps:
        if step.get("op") == "op_exists_any":
            condition_kinds.add("EXISTS")

    if require_full_capability_cover:
        if not (op_ids & _H_OPS):
            raise QueryEngineError(
                "plan missing H op (GroupTopK/ArgExtremum)",
                fail_locus=FAIL_LOCUS_GROUP_LATEST,
            )
        if not (op_ids & _WINDOW_OPS):
            raise QueryEngineError(
                "plan missing window/interval ops",
                fail_locus=FAIL_LOCUS_TIME_WINDOW,
            )
        stamp_count = sum(1 for s in steps if s.get("op") == "op_stamp_window")
        if stamp_count < 2:
            raise QueryEngineError(
                "plan requires multi-step time windows (stamp_window >= 2)",
                fail_locus=FAIL_LOCUS_TIME_WINDOW,
            )
        if "EXISTS" not in condition_kinds and "ANY" not in condition_kinds:
            raise QueryEngineError(
                "plan missing EXISTS/ANY",
                fail_locus=FAIL_LOCUS_EXISTENCE,
            )
        has_one_to_many = any(
            isinstance(e, dict) and e.get("cardinality") == "one_to_many" for e in edges
        )
        if not has_one_to_many:
            raise QueryEngineError(
                "plan missing one_to_many dataflow edge",
                fail_locus=FAIL_LOCUS_OTHER,
            )

    demand_points = plan.get("demand_points")
    if not isinstance(demand_points, list) or not demand_points:
        raise QueryEngineError("demand_points missing", fail_locus=FAIL_LOCUS_OTHER)

    return {"status": "plan_ready", "plan": dict(plan)}


def qp_from_fixture(
    plan_path: str | Path,
    catalog: dict[str, Any],
    *,
    qp_input: Mapping[str, Any] | None = None,
    require_full_capability_cover: bool = True,
) -> dict[str, Any]:
    """B4: load fixed plan fixture as QP output, then validate."""
    plan = load_plan_fixture(plan_path)
    result = validate_query_plan_ast(
        plan,
        catalog,
        require_full_capability_cover=require_full_capability_cover,
    )
    result["qp_input"] = dict(qp_input or {})
    return result
