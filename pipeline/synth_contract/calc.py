# -*- coding: utf-8 -*-
"""
P1-1/P1-2: calc_spec replay, order/dedupe/agg specs, group_edge.
Operators and round modes come from the contract version only.
"""
from __future__ import annotations

import decimal
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .registry import ContractVersion
from .validate import ValidationResult


def _half_even(value: float, scale: int) -> str:
    q = decimal.Decimal("1").scaleb(-int(scale))
    d = decimal.Decimal(str(value)).quantize(q, rounding=decimal.ROUND_HALF_EVEN)
    return format(d, "f")


def _as_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_empty(v: Any, empty_tokens: Sequence[str]) -> bool:
    if v is None:
        return True
    s = str(v).strip()
    if s == "":
        return True
    toks = {str(t).casefold() for t in empty_tokens}
    return s.casefold() in toks


def validate_agg_null_policy(cv: ContractVersion, calc_op: str, policy: str
                             ) -> ValidationResult:
    r = ValidationResult(ok=True)
    if not cv.accepts_enum("calc_op", calc_op):
        return r.fail("calc_op_unknown", "calc_op not registered: %s" % calc_op, "calc_op")
    if not cv.accepts_enum("agg_null_policy", policy):
        return r.fail("agg_null_unknown", "agg_null_policy not registered: %s" % policy,
                      "agg_null_policy")
    matrix = cv.data.get("agg_null_matrix") or {}
    row = matrix.get(calc_op) or {}
    if not row.get(policy):
        return r.fail(
            "agg_null_disallowed",
            "op %s forbids null policy %s" % (calc_op, policy),
            "agg_null_policy",
        )
    return r


def apply_null_policy(values: Sequence[Any], policy: str,
                      empty_tokens: Sequence[str]) -> Tuple[List[Any], Optional[str]]:
    """
    Returns (filtered_or_transformed values, error_or_None).
    reject → error; propagate → sentinel None meaning whole group null.
    """
    out = []
    saw_empty = False
    for v in values:
        if _is_empty(v, empty_tokens):
            saw_empty = True
            if policy == "skip":
                continue
            if policy == "zero":
                out.append(0)
                continue
            if policy == "reject":
                return [], "null_rejected"
            if policy == "propagate":
                return [], "propagate"
            continue
        out.append(v)
    if saw_empty and policy == "propagate":
        return [], "propagate"
    return out, None


def replay_calc_step(op: str, values: Sequence[Any], param: Any = None) -> Any:
    if op == "sum":
        total = 0.0
        for v in values:
            n = _as_float(v)
            if n is None:
                raise ValueError("sum non-numeric")
            total += n
        return total
    if op == "count":
        return len(values)
    if op == "count_distinct":
        return len(set(str(v) for v in values))
    if op == "min":
        nums = [_as_float(v) for v in values]
        if any(n is None for n in nums) or not nums:
            raise ValueError("min empty/non-numeric")
        return min(nums)  # type: ignore[type-var]
    if op == "max":
        nums = [_as_float(v) for v in values]
        if any(n is None for n in nums) or not nums:
            raise ValueError("max empty/non-numeric")
        return max(nums)  # type: ignore[type-var]
    if op == "concat":
        sep = str(param or "") if param is not None else ""
        return sep.join(str(v) for v in values)
    if op == "filter_then":
        # values already filtered by caller; identity passthrough list
        return list(values)
    raise ValueError("calc op has no mechanical impl: %s" % op)


def replay_calc_spec(cv: ContractVersion, spec: dict, group_rows: Sequence[dict],
                     col_by_catalog: Dict[str, str],
                     empty_tokens: Sequence[str] = None) -> ValidationResult:
    """
    Replay steps against group_rows; compare to declared norm_value / result_value.
    group_rows must be the rows named by the group's deriv/group_edge — caller supplies.
    """
    r = ValidationResult(ok=True)
    if not isinstance(spec, dict):
        return r.fail("calc_spec_bad", "not object", "calc_spec")
    for k in ("calc_id", "calc_version", "result_catalog_id", "steps"):
        if not spec.get(k) and spec.get(k) != 0:
            r.fail("calc_field_missing", "missing %s" % k, "calc_spec.%s" % k)
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        return r.fail("calc_steps_missing", "steps empty", "calc_spec.steps")
    allowed = set(str(x) for x in (cv.data.get("calc_ops") or cv.enum_values("calc_op")))
    tokens = list(empty_tokens if empty_tokens is not None else (
        cv.data.get("null_tokens_default") or ["", "null", "none", "-"]))
    null_policy = str(spec.get("agg_null_policy") or "skip")
    current: Any = None
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            return r.fail("calc_step_bad", "step not object", "calc_spec.steps[%d]" % i)
        op = str(step.get("op") or "")
        if op not in allowed:
            return r.fail("calc_op_unknown", "op not registered: %s" % op,
                          "calc_spec.steps[%d]" % i)
        chk = validate_agg_null_policy(cv, op, null_policy)
        if not chk.ok:
            return r.merge(chk)
        src = str(step.get("source_catalog_id") or step.get("catalog_id") or "")
        col = col_by_catalog.get(src) or str(step.get("column") or "")
        if op == "filter_then":
            # keep rows matching exact eq on column/value declared in step
            col_f = col_by_catalog.get(str(step.get("filter_catalog_id") or "")) or str(
                step.get("filter_column") or "")
            needle = str(step.get("filter_value") or "")
            filtered = []
            for row in group_rows:
                cell = "" if not isinstance(row, dict) else str(row.get(col_f, "") or "")
                if cell == needle:
                    filtered.append(row)
            group_rows = filtered
            current = list(group_rows)
            continue
        raw_vals = []
        for row in group_rows:
            if not isinstance(row, dict):
                continue
            raw_vals.append(row.get(col, "") if col else "")
        vals, err = apply_null_policy(raw_vals, null_policy, tokens)
        if err == "null_rejected":
            return r.fail("agg_null_reject", "null rejected in group", "calc_spec.steps[%d]" % i)
        if err == "propagate":
            current = None
            break
        try:
            current = replay_calc_step(op, vals, step.get("param"))
        except ValueError as e:
            return r.fail("calc_replay_error", str(e), "calc_spec.steps[%d]" % i,
                          state_id="fail")
    round_spec = spec.get("round_spec")
    if isinstance(round_spec, dict) and current is not None and isinstance(current, (int, float)):
        mode = str(round_spec.get("mode") or "half_even")
        if not cv.accepts_enum("round_mode", mode):
            return r.fail("round_mode_unknown", "round mode not registered", "round_spec.mode")
        try:
            scale = int(round_spec.get("scale"))
        except (TypeError, ValueError):
            return r.fail("round_scale_bad", "scale not int", "round_spec.scale")
        if mode == "half_even":
            current = _half_even(float(current), scale)
    declared = spec.get("norm_value")
    if declared is None:
        declared = spec.get("result_value")
    if declared is not None and str(declared) != str(current):
        return r.fail(
            "calc_replay_mismatch",
            "replay %r != declared %r" % (current, declared),
            "calc_spec",
            state_id="fail",
        )
    return r


def validate_order_spec(cv: ContractVersion, spec: dict) -> ValidationResult:
    r = ValidationResult(ok=True)
    if not isinstance(spec, dict):
        return r.fail("order_spec_bad", "not object", "order_spec")
    keys = spec.get("keys")
    if not isinstance(keys, list) or not keys:
        return r.fail("order_keys_missing", "keys empty", "order_spec.keys")
    for i, k in enumerate(keys):
        if not isinstance(k, dict):
            r.fail("order_key_bad", "not object", "order_spec.keys[%d]" % i)
            continue
        if not k.get("catalog_id") and not k.get("column"):
            r.fail("order_key_id", "catalog_id or column missing",
                   "order_spec.keys[%d]" % i, state_id="contract_incomplete")
        direction = str(k.get("dir") or k.get("direction") or "")
        if not cv.accepts_enum("order_dir", direction):
            r.fail("order_dir_unknown", "dir not registered", "order_spec.keys[%d]" % i)
    if not spec.get("tie_break"):
        r.fail("order_tie_break", "tie_break missing", "order_spec.tie_break")
    no = str(spec.get("null_order") or "")
    if not cv.accepts_enum("null_order", no):
        r.fail("null_order_unknown", "null_order not registered", "order_spec.null_order")
    return r


def validate_dedupe_spec(cv: ContractVersion, spec: dict,
                         order_ids: Set[str], norm_ids: Set[str]) -> ValidationResult:
    r = ValidationResult(ok=True)
    if not isinstance(spec, dict):
        return r.fail("dedupe_spec_bad", "not object", "dedupe_spec")
    kid = str(spec.get("key_norm_id") or "")
    if not kid or kid not in norm_ids:
        r.fail("dedupe_key_norm", "key_norm_id missing or unknown", "dedupe_spec.key_norm_id")
    keep = str(spec.get("keep") or "")
    if not cv.accepts_enum("dedupe_keep", keep):
        r.fail("dedupe_keep_unknown", "keep not registered", "dedupe_spec.keep")
    oid = str(spec.get("order_id") or spec.get("order_spec_id") or "")
    if not oid or oid not in order_ids:
        r.fail("dedupe_order", "order_id missing or unknown", "dedupe_spec.order_id")
    return r


def validate_agg_spec(spec: dict, calc_ids: Set[str], order_ids: Set[str]) -> ValidationResult:
    r = ValidationResult(ok=True)
    if not isinstance(spec, dict):
        return r.fail("agg_spec_bad", "not object", "agg_spec")
    groups = spec.get("group_catalog_ids")
    if not isinstance(groups, list) or not groups:
        groups = spec.get("group_columns")
    if not isinstance(groups, list) or not groups:
        r.fail("agg_groups_missing",
               "group_catalog_ids or group_columns empty",
               "agg_spec.group_catalog_ids",
               state_id="contract_incomplete")
    cid = str(spec.get("calc_id") or "")
    if not cid or cid not in calc_ids:
        r.fail("agg_calc_id", "calc_id missing or unknown", "agg_spec.calc_id")
    oid = str(spec.get("order_id") or spec.get("order_spec_id") or "")
    if oid and oid not in order_ids:
        r.fail("agg_order_id", "order_id unknown", "agg_spec.order_id")
    return r


def validate_group_edge(edge: dict, snapshot_coords: Set[str],
                        dsl_ops: Set[str]) -> ValidationResult:
    """Group-level deriv edge: input_coords[] must all land in snapshot; op in dsl."""
    r = ValidationResult(ok=True)
    if not isinstance(edge, dict):
        return r.fail("group_edge_bad", "not object", "group_edge")
    op = str(edge.get("op_id") or "")
    if op and op not in dsl_ops:
        r.fail("group_edge_op", "op_id not in dsl", "group_edge.op_id")
    coords = [str(c) for c in (edge.get("input_coords") or [])]
    if not coords:
        return r.fail("group_edge_coords", "input_coords empty", "group_edge.input_coords")
    for c in coords:
        if c not in snapshot_coords:
            r.fail("group_edge_coord_missing", "coord not in snapshot: %s" % c,
                   "group_edge.input_coords")
    if not edge.get("group_key"):
        r.fail("group_edge_key", "group_key missing", "group_edge.group_key")
    return r


def enforce_calc_and_order(plan: dict, cv: ContractVersion,
                           group_rows_by_key: Dict[str, List[dict]] = None,
                           col_by_catalog: Dict[str, str] = None) -> ValidationResult:
    """Validate declared order/dedupe/agg/calc/group_edge objects on the plan."""
    r = ValidationResult(ok=True)
    order_specs = {
        str(s.get("id") or s.get("order_spec_id") or s.get("spec_id") or ""): s
        for s in (plan.get("order_specs") or []) if isinstance(s, dict)
    }
    for sid, spec in order_specs.items():
        if sid:
            r = r.merge(validate_order_spec(cv, spec))
    norm_ids = set()
    for s in plan.get("norm_specs") or []:
        if isinstance(s, dict) and s.get("norm_id"):
            norm_ids.add(str(s["norm_id"]))
        if isinstance(s, dict) and s.get("spec_id"):
            norm_ids.add(str(s["spec_id"]))
    for s in plan.get("normalize_specs") or []:
        if isinstance(s, dict) and s.get("spec_id"):
            norm_ids.add(str(s["spec_id"]))
    for i, d in enumerate(plan.get("dedupe_specs") or []):
        if isinstance(d, dict):
            r = r.merge(validate_dedupe_spec(cv, d, set(order_specs), norm_ids))
    calc_specs = {}
    for s in plan.get("calc_specs") or []:
        if isinstance(s, dict) and s.get("calc_id"):
            calc_specs[str(s["calc_id"])] = s
    for i, a in enumerate(plan.get("agg_specs") or []):
        if isinstance(a, dict):
            r = r.merge(validate_agg_spec(a, set(calc_specs), set(order_specs)))
    snap = set(str(c) for c in (plan.get("sheet_data_coords") or []))
    ops = set(str(s.get("op") or s.get("step_id") or "")
              for s in (plan.get("dsl_steps") or []) if isinstance(s, dict))
    ops |= {"aggregate", "derive", "project"}
    for i, e in enumerate(plan.get("group_edges") or []):
        if isinstance(e, dict):
            r = r.merge(validate_group_edge(e, snap, ops))
    # replay if rows provided (caller map, plan.calc_group_rows, or spec.input_rows)
    rows_map = dict(group_rows_by_key or {})
    for k, v in (plan.get("calc_group_rows") or {}).items():
        if k not in rows_map:
            rows_map[str(k)] = v
    cmap = dict(col_by_catalog or {})
    for e in plan.get("catalog") or []:
        if isinstance(e, dict) and e.get("catalog_id") and e.get("field_name"):
            cmap.setdefault(str(e["catalog_id"]), str(e["field_name"]))
    for cid, spec in calc_specs.items():
        gkey = str(spec.get("group_key") or "")
        rows = (rows_map.get(gkey) or rows_map.get(cid)
                or spec.get("input_rows") or [])
        # always structural-check ops
        for j, step in enumerate(spec.get("steps") or []):
            if not isinstance(step, dict):
                continue
            op = str(step.get("op") or "")
            if op and op not in set(str(x) for x in (
                    cv.data.get("calc_ops") or cv.enum_values("calc_op"))):
                r.fail("calc_op_unknown", "op not registered: %s" % op,
                       "calc_specs[%s].steps[%d]" % (cid, j))
            pol = str(spec.get("agg_null_policy") or "skip")
            if op:
                r = r.merge(validate_agg_null_policy(cv, op, pol))
        declared = spec.get("norm_value")
        if declared is None:
            declared = spec.get("result_value")
        if declared is not None and not rows:
            r.fail(
                "calc_rows_missing",
                "declared result but no input_rows/group_rows for replay",
                "calc_specs[%s]" % cid,
                state_id="contract_incomplete",
            )
        elif rows:
            r = r.merge(replay_calc_spec(cv, spec, rows, cmap))
    return r
