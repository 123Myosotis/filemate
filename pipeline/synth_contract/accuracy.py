# -*- coding: utf-8 -*-
"""
P0/P1 mechanical checks: norm replay, empty classes, row accounting,
deriv edges, key-norm equality, review-anchor independence.
Operators come from the contract version. No industry branches.
"""
from __future__ import annotations

import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .registry import ContractVersion
from .validate import ValidationResult


def replay_norm_steps(value: Any, steps: Sequence[dict], allowed: Set[str]) -> Any:
    s = "" if value is None else (value if isinstance(value, str) else str(value))
    for step in steps:
        op = str(step.get("op") or "")
        if op not in allowed:
            raise ValueError("norm op not registered: %s" % op)
        param = step.get("param")
        if op == "identity":
            # raw stringify only — no trim/lower/local normalization
            continue
        elif op == "trim":
            s = s.strip()
        elif op == "casefold":
            s = s.casefold()
        elif op == "width_fold":
            s = unicodedata.normalize("NFKC", s)
        elif op == "strip_grouping_sep":
            sep = str(param or "")
            if sep:
                s = s.replace(sep, "")
        elif op == "scale":
            try:
                n = int(param)
                s = ("%.*f" % (n, float(s)))
            except (TypeError, ValueError):
                raise ValueError("scale param not int-compatible")
        elif op == "empty_token":
            # mapping applied by caller; this op is a marker
            continue
        elif op == "unit_factor":
            num = (param or {}).get("num") if isinstance(param, dict) else None
            den = (param or {}).get("den") if isinstance(param, dict) else None
            if not den:
                raise ValueError("unit_factor den missing")
            s = str(float(s) * float(num) / float(den))
        else:
            raise ValueError("norm op has no mechanical impl: %s" % op)
    return s


def validate_norm_spec(cv: ContractVersion, spec: dict, sample: Any,
                       declared_norm_value: Any) -> ValidationResult:
    r = ValidationResult(ok=True)
    steps = spec.get("steps") if isinstance(spec, dict) else None
    if not isinstance(steps, list) or not steps:
        return r.fail("norm_steps_missing", "norm_spec.steps empty", "norm_spec")
    if spec.get("idempotent") is not True:
        return r.fail("norm_not_idempotent_flag", "idempotent must be true", "norm_spec")
    allowed = set(str(x) for x in (cv.data.get("normalize_ops") or []))
    # extend allowed with P0 ops listed on contract if present
    allowed |= set(str(x) for x in (cv.data.get("norm_ops") or []))
    try:
        once = replay_norm_steps(sample, steps, allowed)
        twice = replay_norm_steps(once, steps, allowed)
    except ValueError as e:
        return r.fail("norm_replay_error", str(e), "norm_spec")
    if once != twice:
        return r.fail("norm_not_idempotent", "second pass differs", "norm_spec")
    if declared_norm_value is not None and str(declared_norm_value) != str(once):
        return r.fail("norm_replay_mismatch", "norm_value != replay", "norm_spec")
    return r


def classify_empty(raw: Any, mapping: Sequence[dict],
                   candidates: Sequence[str] = None) -> str:
    """
    Mapped raw → declared enum.
    Listed as an empty candidate but unmapped → unclassified_empty (must not compare).
    Otherwise a normal value.
    """
    s = "" if raw is None else str(raw)
    for row in mapping or []:
        if str(row.get("raw")) == s:
            return str(row.get("enum") or "unclassified_empty")
    cand = set(str(c) for c in (candidates or []))
    if s in cand or s == "":
        return "unclassified_empty"
    return "value"


def compare_empty(left_class: str, right_class: str, op: str) -> str:
    """Return true|false|incomparable. Classes vacant/unknown/na never cross-equal."""
    empties = {"vacant", "unknown", "na"}
    if left_class == "unclassified_empty" or right_class == "unclassified_empty":
        return "incomparable"
    if left_class in empties or right_class in empties:
        if op == "eq":
            return "true" if left_class == right_class and left_class in empties else "false"
        return "incomparable"
    return "value"


def validate_row_accounting(account: dict) -> ValidationResult:
    """Three sets pairwise disjoint; union equals input rows. Region-scoped."""
    r = ValidationResult(ok=True)
    if not isinstance(account, dict):
        return r.fail("row_accounting_missing", "not object", "row_accounting")
    inp = set(str(x) for x in (account.get("input_row_coords") or []))
    processed = set(str(x) for x in (account.get("processed") or []))
    excluded = set(str(x) for x in (account.get("excluded") or []))
    discarded = set(str(x) for x in (account.get("discarded") or []))
    if not account.get("scope"):
        r.fail("row_accounting_scope", "scope missing", "row_accounting.scope")
    if processed & excluded or processed & discarded or excluded & discarded:
        r.fail("row_accounting_overlap", "dispositions overlap", "row_accounting",
               state_id="fail")
    union = processed | excluded | discarded
    if union != inp:
        r.fail(
            "row_accounting_cover",
            "union!=input missing=%d extra=%d" % (len(inp - union), len(union - inp)),
            "row_accounting",
            state_id="fail",
        )
    return r


def validate_block_rows(blocks: Sequence[dict], region_rows: Set[str]) -> ValidationResult:
    r = ValidationResult(ok=True)
    seen: Set[str] = set()
    for i, b in enumerate(blocks or []):
        rows = set(str(x) for x in (b.get("row_coords") or []))
        if seen & rows:
            r.fail("block_overlap", "block rows overlap", "blocks[%d]" % i, state_id="fail")
        seen |= rows
    if seen != set(region_rows):
        r.fail("block_cover", "block union != region rows", "blocks", state_id="fail")
    return r


def validate_deriv_edges(edges: Sequence[dict], snapshot_coords: Set[str],
                         dsl_ops: Set[str]) -> ValidationResult:
    r = ValidationResult(ok=True)
    if not edges:
        return r.fail("deriv_edge_missing", "no deriv_edge", "deriv_edge")
    for i, e in enumerate(edges):
        if not isinstance(e, dict):
            r.fail("deriv_edge_bad", "not object", "deriv_edge[%d]" % i)
            continue
        op = str(e.get("op_id") or "")
        if op not in dsl_ops:
            r.fail("deriv_op_missing", "op_id not in dsl", "deriv_edge[%d]" % i)
        kind = str(e.get("kind") or "cell_edge")
        if kind == "cell_edge":
            coords = [str(e.get("input_coord") or "")]
        else:
            coords = [str(c) for c in (e.get("input_coords") or [])]
        for c in coords:
            if not c or c not in snapshot_coords:
                r.fail("deriv_coord_missing", "input coord not in snapshot: %s" % c,
                       "deriv_edge[%d]" % i)
    return r


def validate_key_norm_pair(left_steps: Sequence[dict], right_steps: Sequence[dict]) -> ValidationResult:
    r = ValidationResult(ok=True)
    if not isinstance(left_steps, list) or not left_steps:
        return r.fail("norm_steps_missing", "left steps empty — identity must be explicit",
                      "key_norm_id")
    if not isinstance(right_steps, list) or not right_steps:
        return r.fail("norm_steps_missing", "right steps empty — identity must be explicit",
                      "key_norm_id")

    def norm(steps):
        return [(str(s.get("op")), s.get("param")) for s in steps]

    if norm(left_steps) != norm(right_steps):
        return r.fail("key_norm_mismatch", "join key norm steps differ", "key_norm_id")
    return r


def norm_specs_index(plan: dict) -> Dict[str, dict]:
    idx: Dict[str, dict] = {}
    for s in list(plan.get("norm_specs") or []) + list(plan.get("normalize_specs") or []):
        if not isinstance(s, dict):
            continue
        for k in ("norm_id", "spec_id", "key_norm_id"):
            if s.get(k):
                idx[str(s[k])] = s
    return idx


def resolve_norm_steps(plan: dict, norm_id: str) -> Optional[List[dict]]:
    spec = norm_specs_index(plan).get(str(norm_id or ""))
    if not spec:
        return None
    steps = spec.get("steps")
    if isinstance(steps, list):
        return steps
    # normalize_specs use "ops" string list — not valid for key_norm (must be steps objects)
    return None


def enforce_join_key_norms(plan: dict, cv: ContractVersion) -> ValidationResult:
    """Every join must declare key_norm_id(s); steps non-empty and equal."""
    r = ValidationResult(ok=True)
    joins = list(plan.get("contract_joins") or []) + list(plan.get("joins") or [])
    if not joins:
        return r
    allowed = set(str(x) for x in (cv.data.get("norm_ops") or []))
    allowed |= set(str(x) for x in (cv.data.get("normalize_ops") or []))
    for i, j in enumerate(joins):
        if not isinstance(j, dict):
            r.fail("join_bad", "not object", "joins[%d]" % i)
            continue
        shared = str(j.get("key_norm_id") or "").strip()
        left_id = str(j.get("left_key_norm_id") or shared or "").strip()
        right_id = str(j.get("right_key_norm_id") or shared or "").strip()
        if not left_id or not right_id:
            r.fail(
                "key_norm_id_missing",
                "join missing key_norm_id",
                "joins[%d]" % i,
                state_id="contract_incomplete",
            )
            continue
        left_steps = resolve_norm_steps(plan, left_id)
        right_steps = resolve_norm_steps(plan, right_id)
        if left_steps is None:
            r.fail("key_norm_unknown", "left key_norm_id not found: %s" % left_id,
                   "joins[%d]" % i, state_id="contract_incomplete")
            continue
        if right_steps is None:
            r.fail("key_norm_unknown", "right key_norm_id not found: %s" % right_id,
                   "joins[%d]" % i, state_id="contract_incomplete")
            continue
        r = r.merge(validate_key_norm_pair(left_steps, right_steps))
        for side, steps in (("left", left_steps), ("right", right_steps)):
            for st in steps:
                op = str((st or {}).get("op") or "")
                if op not in allowed:
                    r.fail("norm_op_unknown", "op %s not registered" % op,
                           "joins[%d].%s" % (i, side), state_id="version_rejected")
    return r


def validate_review_anchor(mode: str, plan_anchor: dict, review_anchor: dict) -> ValidationResult:
    r = ValidationResult(ok=True)
    if mode == "disjoint_sample":
        a = str(plan_anchor.get("sample_manifest_id") or "")
        b = str(review_anchor.get("sample_manifest_id") or "")
        sa = set(str(x) for x in (plan_anchor.get("coords") or []))
        sb = set(str(x) for x in (review_anchor.get("coords") or []))
        if not a or not b or a == b or (sa & sb):
            return r.fail("review_reread", "review sample not disjoint", "review",
                          state_id="review_reread")
        return r
    if mode == "cross_basis":
        if str(plan_anchor.get("based_on") or "") == str(review_anchor.get("based_on") or ""):
            return r.fail("review_reread", "based_on not crossed", "review",
                          state_id="review_reread")
        return r
    return r.fail("review_mode_unknown", "review_anchor_mode not registered", "review")


def enforce_declared_accuracy(plan: dict, cv: ContractVersion,
                              group_rows_by_key: dict = None,
                              col_by_catalog: dict = None) -> ValidationResult:
    """
    Objects the plan actually declares are checked. Absence is not filled in.
    A declared object that fails rejects the region result.
    """
    r = ValidationResult(ok=True)
    for i, spec in enumerate(plan.get("norm_specs") or []):
        if not isinstance(spec, dict):
            r.fail("norm_spec_bad", "not object", "norm_specs[%d]" % i)
            continue
        sample = spec.get("sample")
        declared = spec.get("norm_value")
        r = r.merge(validate_norm_spec(cv, spec, sample, declared))
    if plan.get("row_accounting") is not None:
        r = r.merge(validate_row_accounting(plan["row_accounting"]))
    if plan.get("blocks") is not None:
        region_rows = set(str(x) for x in (
            (plan.get("row_accounting") or {}).get("input_row_coords") or []))
        r = r.merge(validate_block_rows(plan.get("blocks") or [], region_rows))
    if plan.get("deriv_edges") is not None:
        snap = set(str(c) for c in (plan.get("sheet_data_coords") or []))
        ops = set(str(s.get("op") or s.get("step_id") or "")
                  for s in (plan.get("dsl_steps") or []) if isinstance(s, dict))
        ops.add("project")
        r = r.merge(validate_deriv_edges(plan.get("deriv_edges") or [], snap, ops))
    if plan.get("review_anchor_mode"):
        r = r.merge(validate_review_anchor(
            str(plan.get("review_anchor_mode")),
            plan.get("plan_anchor") or {},
            plan.get("review_anchor") or {},
        ))
    joins = list(plan.get("contract_joins") or []) + list(plan.get("joins") or [])
    if joins:
        r = r.merge(enforce_join_key_norms(plan, cv))
    for i, j in enumerate(joins):
        if not isinstance(j, dict):
            continue
        if j.get("left_norm_steps") is not None or j.get("right_norm_steps") is not None:
            r = r.merge(validate_key_norm_pair(
                j.get("left_norm_steps") or [],
                j.get("right_norm_steps") or [],
            ))
    # Force-layer empty_token_map by handbook role table
    from .empty_roles import enforce_empty_token_maps, collect_role_fields
    if collect_role_fields(plan):
        r = r.merge(enforce_empty_token_maps(plan))
    if plan.get("agg_specs"):
        r = r.merge(validate_agg_semantics(plan, cv))
    # P1 calc / order / dedupe / agg / group_edge
    needs_calc = any(plan.get(k) is not None for k in (
        "calc_specs", "order_specs", "dedupe_specs", "agg_specs", "group_edges"))
    if needs_calc:
        from .calc import enforce_calc_and_order
        r = r.merge(enforce_calc_and_order(
            plan, cv,
            group_rows_by_key=group_rows_by_key,
            col_by_catalog=col_by_catalog,
        ))
    # P1 xref / entity / caliber
    needs_xref = any(plan.get(k) is not None for k in (
        "entity_reg", "alias_map", "xref", "calibers", "metric_calibers",
        "caliber_map", "batch_id"))
    if needs_xref:
        from .xref import enforce_xref_and_caliber
        r = r.merge(enforce_xref_and_caliber(plan))
    # budget + DAG + review batches
    if plan.get("budgets") is not None or plan.get("meter") is not None:
        from .budget import enforce_budgets
        r = r.merge(enforce_budgets(plan, cv))
    if plan.get("regions") is not None or plan.get("review_batches") is not None:
        from .dag import enforce_dag_and_review
        r = r.merge(enforce_dag_and_review(plan))
    # unknown trigger ids in plan
    for i, t in enumerate(plan.get("triggers") or []):
        tid = str(t.get("trigger") if isinstance(t, dict) else t)
        if tid and not cv.accepts_enum("trigger", tid):
            r.fail(
                "trigger_unknown",
                "trigger not in registry: %s" % tid,
                "triggers[%d]" % i,
                state_id="version_rejected",
            )
    return r


def validate_agg_semantics(plan: dict, cv: ContractVersion) -> ValidationResult:
    """Each agg op must declare all contract empty classes. unclassified_empty is fail."""
    r = ValidationResult(ok=True)
    classes = [str(x) for x in (cv.data.get("agg_semantics_classes") or [])]
    actions = set(cv.enum_values("agg_semantics_action"))
    required = cv.data.get("agg_semantics_required_action") or {}
    if not classes or not actions:
        return r.fail(
            "agg_semantics_contract",
            "agg_semantics_classes or agg_semantics_action missing from contract",
            "agg_semantics",
            state_id="version_rejected",
        )
    calc_by = {
        str(s.get("calc_id")): s
        for s in (plan.get("calc_specs") or [])
        if isinstance(s, dict) and s.get("calc_id")
    }
    for i, spec in enumerate(plan.get("agg_specs") or []):
        if not isinstance(spec, dict):
            continue
        calc = calc_by.get(str(spec.get("calc_id") or "")) or {}
        sem = spec.get("agg_semantics")
        if sem is None:
            sem = calc.get("agg_semantics")
        ops = []
        for step in calc.get("steps") or []:
            if isinstance(step, dict) and step.get("op"):
                ops.append(str(step["op"]))
        if not ops:
            ops = [str(spec.get("op") or "count")]
        for op in ops:
            block = sem.get(op) if isinstance(sem, dict) else None
            if not isinstance(block, dict):
                r.fail(
                    "agg_semantics_missing",
                    "agg_semantics.%s missing" % op,
                    "agg_specs[%d].agg_semantics" % i,
                    state_id="contract_incomplete",
                )
                continue
            for cls in classes:
                if cls not in block:
                    r.fail(
                        "agg_semantics_incomplete",
                        "agg_semantics.%s missing class %s" % (op, cls),
                        "agg_specs[%d].agg_semantics.%s" % (i, op),
                        state_id="contract_incomplete",
                    )
                    continue
                action = str(block.get(cls) or "")
                if action not in actions:
                    r.fail(
                        "agg_semantics_action",
                        "action %s not in agg_semantics_action" % action,
                        "agg_specs[%d].agg_semantics.%s.%s" % (i, op, cls),
                        state_id="contract_incomplete",
                    )
            for cls, must in required.items():
                if str(block.get(cls) or "") != str(must):
                    r.fail(
                        "agg_semantics_unclassified",
                        "agg_semantics.%s.%s must be %s" % (op, cls, must),
                        "agg_specs[%d].agg_semantics.%s.%s" % (i, op, cls),
                        state_id="contract_incomplete",
                    )
    return r


def duplicate_keys(values: Sequence[Any], cardinality: str) -> bool:
    if cardinality not in ("1", "1:N", "1:1"):
        return False
    seen = set()
    for v in values:
        if v in seen:
            return True
        seen.add(v)
    return False
