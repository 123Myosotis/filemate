# -*- coding: utf-8 -*-
"""
v1.1.0 ops[] validation and fixed-order dispatch iteration.
Static OPERATOR_DISPATCH lives here — AST-scanned by authority_scan.

Execution: walk contract fixed_op_order; for each op present in ops[],
resolve param object by id; call the named apply_* in executor (DISPATCH).
There is no conversion to a v1.0.0 free-key plan (no second interpreter).
"""
from __future__ import annotations

from typing import Dict, Iterator, List, Optional, Tuple

from .registry import ContractVersion
from .validate import ValidationResult

# Literal keys only. Do not build from operator_set at runtime.
OPERATOR_DISPATCH: Dict[str, str] = {
    "join": "apply_join",
    "filter": "apply_filter",
    "derive": "apply_derive",
    "map_values": "apply_map_values",
    "order": "apply_order",
    "dedupe": "apply_dedupe",
    "aggregate": "apply_aggregate",
    "project": "apply_project",
}

_FORBIDDEN_TOP = (
    "keep_row_when",
    "derived_columns",
    "value_mappings",
    "output_columns",
    "column_origins",
)


def is_ops_contract(cv: ContractVersion) -> bool:
    """Direction-2+ plans only (v1.1.x)."""
    return str(cv.version).startswith("v1.1")


# Back-compat alias used by older call sites during migration
def is_v11(cv: ContractVersion) -> bool:
    return is_ops_contract(cv)


def _oset(cv: ContractVersion) -> List[dict]:
    raw = cv.data.get("operator_set") or []
    return [x for x in raw if isinstance(x, dict) and x.get("op")]


def _ref_key(entry: dict) -> str:
    return str(entry.get("param_ref") or "")


def _obj_name(entry: dict) -> str:
    return str(entry.get("param_object") or "")


def validate_ops_plan(plan: dict, cv: ContractVersion) -> ValidationResult:
    r = ValidationResult(ok=True)
    if not is_ops_contract(cv):
        r.fail(
            "ops_version_required",
            "executor accepts v1.1.x ops[] only; got %s" % cv.version,
            "plan.contract_version",
            state_id="version_rejected",
        )
        return r
    for k in _FORBIDDEN_TOP:
        if plan.get(k) is not None:
            r.fail(
                "legacy_field_forbidden",
                "v1.1.0 forbids top-level %s" % k,
                "plan.%s" % k,
                state_id="contract_incomplete",
            )
    ops = plan.get("ops")
    if ops is None:
        r.fail("ops_missing", "ops missing", "ops", state_id="contract_incomplete")
        return r
    if not isinstance(ops, list):
        r.fail("ops_bad", "ops not list", "ops", state_id="contract_incomplete")
        return r
    if len(ops) == 0:
        r.fail("empty_ops", "ops empty", "ops", state_id="contract_incomplete")
        return r

    by_op = {str(e["op"]): e for e in _oset(cv)}
    seen_refs: Dict[str, str] = {}
    for i, step in enumerate(ops):
        if not isinstance(step, dict):
            r.fail("ops_step_bad", "not object", "ops[%d]" % i)
            continue
        op = str(step.get("op") or "")
        if op not in by_op:
            r.fail(
                "unknown_op",
                "op not in operator_set: %s" % op,
                "ops[%d].op" % i,
                state_id="version_rejected",
            )
            continue
        if op not in OPERATOR_DISPATCH:
            r.fail(
                "op_no_dispatch",
                "op has no static dispatch: %s" % op,
                "ops[%d].op" % i,
                state_id="version_rejected",
            )
            continue
        meta = by_op[op]
        rk = _ref_key(meta)
        if not rk or rk not in step or not str(step.get(rk) or "").strip():
            r.fail(
                "param_ref_missing",
                "missing %s" % rk,
                "ops[%d]" % i,
                state_id="contract_incomplete",
            )
            continue
        ref_val = str(step[rk])
        if ref_val in seen_refs:
            r.fail(
                "duplicate_param_ref",
                "param_ref value %s repeated" % ref_val,
                "ops[%d].%s" % (i, rk),
                state_id="contract_incomplete",
            )
        seen_refs[ref_val] = op
        arr_name = _obj_name(meta)
        obj = _find_by_id(plan.get(arr_name) or [], ref_val)
        if obj is None:
            r.fail(
                "param_ref_unresolved",
                "%s id=%s not found" % (arr_name, ref_val),
                "ops[%d].%s" % (i, rk),
                state_id="contract_incomplete",
            )
            continue
        cmp_enum = meta.get("cmp_enum")
        if cmp_enum:
            cmp = str(obj.get("op") or "")
            if cmp and not cv.accepts_enum(str(cmp_enum), cmp):
                r.fail(
                    "cmp_op_unknown",
                    "cmp %s not in %s" % (cmp, cmp_enum),
                    "%s[%s].op" % (arr_name, ref_val),
                    state_id="version_rejected",
                )
            for cond in obj.get("conditions") or []:
                if isinstance(cond, dict):
                    c_op = str(cond.get("op") or "")
                    if c_op and not cv.accepts_enum(str(cmp_enum), c_op):
                        r.fail(
                            "cmp_op_unknown",
                            "cmp %s not in %s" % (c_op, cmp_enum),
                            "%s[%s].conditions" % (arr_name, ref_val),
                            state_id="version_rejected",
                        )
        if "order_spec_id" in obj:
            r.fail(
                "order_spec_id_forbidden",
                "use order_id / id only",
                "%s[%s]" % (arr_name, ref_val),
                state_id="contract_incomplete",
            )
    order_ids = {
        str(s.get("id") or "")
        for s in (plan.get("order_specs") or [])
        if isinstance(s, dict) and s.get("id")
    }
    for name in ("dedupe_specs", "agg_specs"):
        for j, spec in enumerate(plan.get(name) or []):
            if not isinstance(spec, dict):
                continue
            if spec.get("order_spec_id") is not None:
                r.fail(
                    "order_spec_id_forbidden",
                    "use order_id",
                    "%s[%d]" % (name, j),
                    state_id="contract_incomplete",
                )
            oid = str(spec.get("order_id") or "")
            if oid and oid not in order_ids:
                r.fail(
                    "order_id_unknown",
                    "order_id %s missing" % oid,
                    "%s[%d].order_id" % (name, j),
                    state_id="contract_incomplete",
                )
    return r


def _find_by_id(arr: list, oid: str) -> Optional[dict]:
    for item in arr:
        if isinstance(item, dict) and str(item.get("id") or "") == oid:
            return item
    return None


def iter_ops_fixed_order(
    plan: dict, cv: ContractVersion
) -> Iterator[Tuple[str, str, dict]]:
    """
    Yield (op, dispatch_fn_name, param_object) in contract fixed_op_order.
    ops[] writing order is ignored for sequencing; only membership + refs matter.
    """
    by_op = {str(e["op"]): e for e in _oset(cv)}
    fixed = [str(x) for x in (cv.data.get("fixed_op_order") or [])]
    if not fixed:
        fixed = list(OPERATOR_DISPATCH.keys())
    # Preserve relative order among same-op steps as written in ops[]
    steps_by_op: Dict[str, List[dict]] = {k: [] for k in fixed}
    for step in plan.get("ops") or []:
        if not isinstance(step, dict):
            continue
        op = str(step.get("op") or "")
        if op in steps_by_op:
            steps_by_op[op].append(step)
    for op in fixed:
        meta = by_op.get(op) or {}
        rk = _ref_key(meta)
        arr_name = _obj_name(meta)
        fn = OPERATOR_DISPATCH.get(op)
        if not fn:
            continue
        for step in steps_by_op.get(op) or []:
            ref = str(step.get(rk) or "")
            obj = _find_by_id(plan.get(arr_name) or [], ref)
            if obj is None:
                continue
            yield op, fn, dict(obj)
