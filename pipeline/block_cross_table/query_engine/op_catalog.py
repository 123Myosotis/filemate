# -*- coding: utf-8 -*-
"""B2: query operator catalog validate / cost gate (contract-slot coeffs; not CURRENT)."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

FAIL_LOCUS_BUDGET = "budget"
FAIL_LOCUS_OTHER = "other"

SORT_TRUNCATE_OP_ID = "op_sort_truncate"
GROUP_TOP_K_OP_ID = "op_group_top_k"
ARG_EXTREMUM_OP_ID = "op_arg_extremum"

_REQUIRED_OP_KEYS = (
    "op_id",
    "params_schema",
    "in_types",
    "out_types",
    "complexity_bound",
    "description",
)

_H_REWRITE_TOKENS = frozenset(
    {
        GROUP_TOP_K_OP_ID,
        ARG_EXTREMUM_OP_ID,
        "GroupTopK",
        "ArgExtremum",
    }
)


class OpCatalogError(ValueError):
    """Catalog / plan / budget gate rejection."""

    def __init__(self, message: str, *, fail_locus: str = FAIL_LOCUS_OTHER):
        super().__init__(message)
        self.fail_locus = fail_locus


def load_op_catalog(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "operators" not in data:
        raise OpCatalogError("catalog root must be object with operators")
    return data


def load_contract_slice(path: str | Path) -> dict[str, Any]:
    """Load a contract JSON root (fixture or version file). Prefer load_cross_table_from_current."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise OpCatalogError("contract slice root must be object")
    return data


def load_cross_table_from_current() -> dict[str, Any]:
    """Load cross_table block from contracts/CURRENT (production path after merge)."""
    from pipeline.synth_contract.registry import load_contract

    data = load_contract().data or {}
    block = data.get("cross_table")
    if not isinstance(block, dict):
        raise OpCatalogError("cross_table missing in CURRENT contract")
    return {"cross_table": block}


def read_complexity_bound_coeffs(contract_slice: Mapping[str, Any]) -> dict[str, float]:
    """
    Read coeff map from contract slots (cross_table.complexity_bound_coeffs).
    Production values may be null until budget card; callers needing numbers must
    pass an explicit override map (tests) — never invent production defaults.
    """
    block = contract_slice.get("cross_table") or contract_slice
    raw = block.get("complexity_bound_coeffs")
    if not isinstance(raw, dict) or not raw:
        raise OpCatalogError(
            "complexity_bound_coeffs missing in contract slots",
            fail_locus=FAIL_LOCUS_BUDGET,
        )
    out: dict[str, float] = {}
    for k, v in raw.items():
        if v is None:
            continue
        out[str(k)] = float(v)
    if not out:
        raise OpCatalogError(
            "complexity_bound_coeffs has no numeric values (budget card pending)",
            fail_locus=FAIL_LOCUS_BUDGET,
        )
    return out


def read_complexity_bound_eval(contract_slice: Mapping[str, Any]) -> dict[str, Any]:
    """Read per-class eval form map from contract slots."""
    block = contract_slice.get("cross_table") or contract_slice
    raw = block.get("complexity_bound_eval")
    if not isinstance(raw, dict) or not raw:
        raise OpCatalogError("complexity_bound_eval missing in contract slots")
    return dict(raw)


def read_query_exec_budget(contract_slice: Mapping[str, Any]) -> dict[str, Any]:
    """Read query_exec_budget structure from contract slots (values may be null)."""
    block = contract_slice.get("cross_table") or contract_slice
    raw = block.get("query_exec_budget")
    if not isinstance(raw, dict):
        raise OpCatalogError(
            "query_exec_budget missing in contract slots",
            fail_locus=FAIL_LOCUS_BUDGET,
        )
    return dict(raw)


def load_op_catalog_from_current() -> dict[str, Any]:
    """Load query_operator_catalog from CURRENT cross_table."""
    slice_ = load_cross_table_from_current()
    cat = (slice_.get("cross_table") or {}).get("query_operator_catalog")
    if not isinstance(cat, dict) or not cat:
        raise OpCatalogError("query_operator_catalog missing in CURRENT")
    return {"operators": dict(cat)}


def validate_op_catalog(catalog: dict[str, Any]) -> list[str]:
    """Return list of op_ids if valid; raise OpCatalogError on reject."""
    ops = catalog.get("operators")
    if not isinstance(ops, dict) or not ops:
        raise OpCatalogError("operators must be non-empty object")
    accepted: list[str] = []
    for op_id, spec in ops.items():
        if not isinstance(spec, dict):
            raise OpCatalogError("operator spec must be object: %s" % op_id)
        for key in _REQUIRED_OP_KEYS:
            if key not in spec:
                raise OpCatalogError("missing %s on op %s" % (key, op_id))
        if spec.get("op_id") != op_id:
            raise OpCatalogError("op_id mismatch for key %s" % op_id)
        bound = spec.get("complexity_bound")
        if not isinstance(bound, dict) or not bound.get("class"):
            raise OpCatalogError("complexity_bound missing/invalid: %s" % op_id)
        # Production coeffs live in contract slots, not on the op object.
        if "coeff" in bound:
            raise OpCatalogError(
                "complexity_bound.coeff must not live on op; use contract slots: %s"
                % op_id
            )
        if op_id == SORT_TRUNCATE_OP_ID:
            if not spec.get("forbids_as_group_latest"):
                raise OpCatalogError(
                    "sort_truncate must set forbids_as_group_latest=true"
                )
        accepted.append(op_id)
    return accepted


def _scale_value(input_scale: Mapping[str, float], key: str, default_key: str = "nrows") -> float:
    if key in input_scale:
        return float(input_scale[key])
    if default_key in input_scale:
        return float(input_scale[default_key])
    return 0.0


def estimate_step_cost(
    catalog: dict[str, Any],
    op_id: str,
    input_scale: Mapping[str, float],
    *,
    coeff_slots: Mapping[str, float],
    bound_eval: Mapping[str, Any],
) -> float:
    """
    Upper-bound function × input scale → estimated cost.
    Coeff and eval form come from contract slots, never from production fixtures hardcoded in code.
    """
    ops = catalog["operators"]
    if op_id not in ops:
        raise OpCatalogError("unknown op: %s" % op_id)
    cls = str(ops[op_id]["complexity_bound"]["class"])
    if cls not in coeff_slots:
        raise OpCatalogError(
            "coeff slot missing for class %s" % cls,
            fail_locus=FAIL_LOCUS_BUDGET,
        )
    if cls not in bound_eval:
        raise OpCatalogError("bound_eval missing for class %s" % cls)
    coeff = float(coeff_slots[cls])
    spec = bound_eval[cls]
    form = str(spec.get("form") or "")

    if form == "linear":
        key = str(spec.get("scale_key") or "nrows")
        return coeff * _scale_value(input_scale, key)
    if form == "max_pair":
        keys = list(spec.get("scale_keys") or [])
        if len(keys) < 2:
            raise OpCatalogError("max_pair requires two scale_keys")
        a = _scale_value(input_scale, keys[0])
        b = _scale_value(input_scale, keys[1])
        return coeff * max(a, b)
    if form == "sum_pair":
        keys = list(spec.get("scale_keys") or [])
        if len(keys) < 2:
            raise OpCatalogError("sum_pair requires two scale_keys")
        a = _scale_value(input_scale, keys[0])
        b = _scale_value(input_scale, keys[1])
        return coeff * (a + b)
    if form == "nlogn":
        key = str(spec.get("scale_key") or "nrows")
        n = max(_scale_value(input_scale, key), 1.0)
        return coeff * n * math.log2(n)
    raise OpCatalogError("unknown bound_eval.form: %s" % form)


def gate_step_budget(
    catalog: dict[str, Any],
    op_id: str,
    input_scale: Mapping[str, float],
    *,
    query_exec_budget: Mapping[str, Any],
    coeff_slots: Mapping[str, float],
    bound_eval: Mapping[str, Any],
) -> float:
    """Compare estimated cost to query_exec_budget from contract slots."""
    estimated = estimate_step_cost(
        catalog,
        op_id,
        input_scale,
        coeff_slots=coeff_slots,
        bound_eval=bound_eval,
    )
    max_per_step = query_exec_budget.get("max_cost_per_step")
    if max_per_step is None:
        raise OpCatalogError(
            "query_exec_budget.max_cost_per_step required in contract slots",
            fail_locus=FAIL_LOCUS_BUDGET,
        )
    if estimated > float(max_per_step):
        raise OpCatalogError(
            "estimated_cost %.4f exceeds max_cost_per_step %.4f"
            % (estimated, float(max_per_step)),
            fail_locus=FAIL_LOCUS_BUDGET,
        )
    return estimated


def validate_plan_ops_linkable(
    catalog: dict[str, Any],
    steps: list[dict[str, Any]],
    *,
    linear_type_check: bool = True,
) -> None:
    """
    Reject unknown ops + bare sort_truncate as group-latest.
    When linear_type_check=True (B2 default), also enforce sequential out→in types.
    DAG plans (B4) pass linear_type_check=False and validate edges separately.
    """
    ops = catalog["operators"]
    prev_out: str | None = None
    for step in steps:
        op_id = step.get("op")
        if op_id not in ops:
            raise OpCatalogError("plan references unknown op: %s" % op_id)
        spec = ops[op_id]
        in_types = list(spec.get("in_types") or [])
        out_types = list(spec.get("out_types") or [])
        if (
            linear_type_check
            and prev_out is not None
            and in_types
            and prev_out not in in_types
        ):
            raise OpCatalogError(
                "type unlink: prev out %s not in %s for %s"
                % (prev_out, in_types, op_id)
            )
        if step.get("claims_group_latest"):
            rewrite = step.get("rewrite_of")
            is_h = op_id in (GROUP_TOP_K_OP_ID, ARG_EXTREMUM_OP_ID)
            rewrite_ok = rewrite in _H_REWRITE_TOKENS
            if op_id == SORT_TRUNCATE_OP_ID and not rewrite_ok:
                raise OpCatalogError(
                    "sort_truncate cannot alone claim group_latest"
                )
            if not is_h and not rewrite_ok:
                raise OpCatalogError(
                    "group_latest requires GroupTopK/ArgExtremum or rewrite_of"
                )
        if out_types:
            prev_out = out_types[0]
