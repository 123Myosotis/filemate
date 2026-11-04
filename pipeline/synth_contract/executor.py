# -*- coding: utf-8 -*-
"""
Mechanical plan executor. Only exact names, catalog ids, and declared coords.
No fuzzy match, no ffill, no inject-from-user, no cardinality veto.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Set, Tuple

from .registry import ContractVersion, load_contract
from .validate import ValidationResult, validate_row_cover


def _reject(notes: list, code: str, path: str = "",
            state_id: str = "contract_incomplete", plan: dict = None) -> dict:
    notes.append("[%s] %s" % (code, path))
    return {
        "name": "合成表",
        "columns": [],
        "rows": [],
        "plan_notes": notes,
        "synth_plan": plan,
        "contract_rejected": True,
        "contract_status": state_id,
        "contract_issues": [
            {"code": code, "message": code, "path": path, "state_id": state_id}
        ],
        "traces": [],
        "trace_ok": False,
        "_mechanical": False,
    }


def _exact_table(tables: dict, name: str) -> Optional[dict]:
    if not name or not tables:
        return None
    name = str(name).strip()
    if name in tables and isinstance(tables[name], dict):
        return tables[name]
    for k, t in tables.items():
        if str(k) == name:
            return t if isinstance(t, dict) else None
        if isinstance(t, dict) and str(t.get("name") or "") == name:
            return t
    return None


def _exact_col(columns: list, name: str) -> Optional[str]:
    name = str(name or "").strip()
    if not name:
        return None
    for c in columns or []:
        if str(c) == name:
            return str(c)
    return None


def _catalog_name_map(plan: dict) -> Dict[str, str]:
    """catalog_id → field_name (display / project name)."""
    out = {}
    for e in plan.get("catalog") or []:
        if isinstance(e, dict) and e.get("catalog_id"):
            out[str(e["catalog_id"])] = str(e.get("field_name") or e.get("orig_col_name") or "")
    return out


def _binding_source_map(plan: dict) -> Dict[str, str]:
    """output field name or catalog id → source column name (exact)."""
    out = {}
    for b in plan.get("bindings") or []:
        if not isinstance(b, dict):
            continue
        src = str(b.get("orig_col_name") or "").strip()
        tid = str(b.get("target_field_id") or "").strip()
        tname = str(b.get("target_field_name") or "").strip()
        if src and tid:
            out[tid] = src
        if src and tname:
            out[tname] = src
    # legacy column_bindings
    for k, v in (plan.get("column_bindings") or {}).items():
        if isinstance(v, dict):
            src = str(v.get("source_column") or "").strip()
        else:
            src = str(v or "").strip()
        if src:
            out[str(k)] = src
    return out


def apply_fills(working: dict, fills: list, notes: list) -> List[dict]:
    """
    fills: [{row_index, column, value, fill_source_coord, ai_binding_id}]
    Only writes when target cell is empty. Separates filled from raw via _fill_meta.
    """
    traces = []
    rows = working.get("rows") or []
    cols = list(working.get("columns") or [])
    meta = working.setdefault("_fill_meta", {})
    for i, spec in enumerate(fills or []):
        if not isinstance(spec, dict):
            notes.append("fills[%d] not object — skipped" % i)
            continue
        try:
            ri = int(spec.get("row_index"))
        except (TypeError, ValueError):
            notes.append("fills[%d] missing row_index — skipped" % i)
            continue
        col = _exact_col(cols, str(spec.get("column") or ""))
        if col is None:
            notes.append("fills[%d] column not found — skipped" % i)
            continue
        if ri < 0 or ri >= len(rows):
            notes.append("fills[%d] row_index out of range — skipped" % i)
            continue
        src = str(spec.get("fill_source_coord") or "").strip()
        if not src:
            notes.append("fills[%d] missing fill_source_coord — skipped" % i)
            continue
        cur = str(rows[ri].get(col, "") or "").strip()
        if cur:
            continue
        val = spec.get("value")
        rows[ri][col] = val
        key = "%d:%s" % (ri, col)
        meta[key] = {"fill_source_coord": src, "ai_binding_id": str(spec.get("ai_binding_id") or "")}
        traces.append({
            "output_coord": key,
            "ai_binding_id": str(spec.get("ai_binding_id") or ""),
            "input_coords": [src],
            "op_id": "apply_fills",
            "catalog_id": str(spec.get("catalog_id") or ""),
        })
    working["rows"] = rows
    return traces


def apply_map_values(working: dict, mappings: list, notes: list) -> List[dict]:
    """Exact when_column + when_contains substring (AI-declared op semantics)."""
    traces = []
    cols = list(working.get("columns") or [])
    rows = working.get("rows") or []
    for i, vm in enumerate(mappings or []):
        if not isinstance(vm, dict):
            continue
        when_col = _exact_col(cols, str(vm.get("when_column") or ""))
        set_col = str(vm.get("set_column") or "").strip()
        needle = str(vm.get("when_contains") or "")
        set_val = str(vm.get("set_value") or "")
        if not when_col or not set_col:
            notes.append("map_values[%d] invalid columns — skipped" % i)
            continue
        if set_col not in cols:
            cols.append(set_col)
            working["columns"] = cols
        n = 0
        for ri, r in enumerate(rows):
            cell = str(r.get(when_col, "") or "")
            if needle and needle not in cell:
                continue
            if not needle and str(vm.get("when_empty") or "") != "1":
                continue
            r[set_col] = set_val
            n += 1
            traces.append({
                "output_coord": "%d:%s" % (ri, set_col),
                "ai_binding_id": str(vm.get("ai_binding_id") or ""),
                "input_coords": ["%d:%s" % (ri, when_col)],
                "op_id": "value_mapping",
                "catalog_id": str(vm.get("catalog_id") or ""),
            })
        notes.append("map_values[%d]: wrote %d rows → %s" % (i, n, set_col))
    return traces


_CMP_ALIASES = {
    "==": "eq",
    "!=": "neq",
    "not_eq": "neq",
    "contain": "contains",
    "nonempty": "not_empty",
}


def _op_allowed(cv: Optional[ContractVersion], op: str) -> bool:
    if cv is None:
        return False
    return cv.accepts_enum("cmp_op", op)


def apply_filter(working: dict, conditions: list, notes: list,
                        cv: Optional[ContractVersion] = None,
                        plan: dict = None) -> Optional[str]:
    """Mechanical filter via classify_empty + exact compare. No is_null path."""
    if not conditions:
        return None
    from .accuracy import classify_empty, compare_empty
    plan = plan or {}
    cols = list(working.get("columns") or [])
    kept = []
    for r in working.get("rows") or []:
        ok = True
        for cond in conditions:
            if not isinstance(cond, dict):
                ok = False
                break
            col = _exact_col(cols, str(cond.get("column") or ""))
            if not col:
                ok = False
                break
            cell = r.get(col, "")
            op = str(cond.get("op") or "eq").lower()
            op = _CMP_ALIASES.get(op, op)
            if not _op_allowed(cv, op):
                notes.append("filter op %r not in contract cmp_op" % op)
                return "filter_op_unknown"
            val = cond.get("value")
            mapping, candidates = _empty_map_for_column(plan, col)
            left_c = classify_empty(cell, mapping, candidates)
            if left_c == "unclassified_empty":
                return "unclassified_empty"
            if op in ("eq", "neq"):
                right_c = classify_empty(val, mapping, candidates)
                if right_c == "unclassified_empty":
                    return "unclassified_empty"
                cmp = compare_empty(left_c, right_c, "eq")
                if cmp == "incomparable":
                    return "incomparable"
                if cmp == "value":
                    # both value class — exact string equality on raw stringify (no strip)
                    ls = "" if cell is None else str(cell)
                    rs = "" if val is None else str(val)
                    eq = ls == rs
                else:
                    eq = cmp == "true"
                ok = eq if op == "eq" else (not eq)
            elif op == "contains":
                if left_c != "value":
                    return "incomparable"
                ok = str(val or "") in str(cell or "")
            elif op == "not_contains":
                if left_c != "value":
                    return "incomparable"
                ok = str(val or "") not in str(cell or "")
            elif op == "not_empty":
                ok = left_c == "value"
            elif op in ("contains_any", "contains_all"):
                if left_c != "value":
                    return "incomparable"
                vals = [str(v) for v in (cond.get("values") or []) if str(v).strip()]
                hay = str(cell or "")
                if op == "contains_any":
                    ok = any(v in hay for v in vals) if vals else False
                else:
                    ok = all(v in hay for v in vals) if vals else False
            else:
                ok = False
            if not ok:
                break
        if ok:
            kept.append(r)
    notes.append("filter: %d → %d rows" % (len(working.get("rows") or []), len(kept)))
    working["rows"] = kept
    return None


def apply_order(working: dict, plan: dict, cv: ContractVersion,
                notes: list, spec: dict = None) -> Optional[str]:
    """Stable total order via keys + tie_break. Returns error or None."""
    if spec is not None:
        specs = [spec]
    else:
        specs = [s for s in (plan.get("order_specs") or []) if isinstance(s, dict)]
    if not specs:
        return None
    from .accuracy import classify_empty
    cols = list(working.get("columns") or [])
    rows = list(working.get("rows") or [])
    for spec in specs:
        keys = spec.get("keys") or []
        if not keys:
            return "order_keys_missing"
        tb = spec.get("tie_break")
        if not tb:
            return "order_tie_break"
        null_order = str(spec.get("null_order") or "last")

        class _Desc:
            __slots__ = ("v",)

            def __init__(self, v):
                self.v = v

            def __lt__(self, o):
                return self.v > o.v

            def __eq__(self, o):
                return self.v == o.v

        def sort_key(item):
            r, idx = item
            parts = []
            for k in keys:
                if not isinstance(k, dict):
                    continue
                col = str(k.get("column") or k.get("catalog_id") or "")
                c = _exact_col(cols, col) or col
                raw = r.get(c, "") if isinstance(r, dict) else ""
                mapping, candidates = _empty_map_for_column(plan, c if c in cols else col)
                klass = classify_empty(raw, mapping, candidates)
                direction = str(k.get("dir") or k.get("direction") or "asc")
                empty_rank = 0 if null_order == "first" else 1
                if klass != "value":
                    parts.append((empty_rank, ""))
                else:
                    s = "" if raw is None else str(raw)
                    parts.append((0 if null_order == "last" else 1,
                                  s if direction == "asc" else _Desc(s)))
            if isinstance(tb, dict):
                tcol = str(tb.get("column") or tb.get("catalog_id") or "")
                parts.append(str(r.get(tcol, "")))
            else:
                parts.append(str(tb))
            parts.append(idx)
            return tuple(parts)

        pairs = [(rows[i], i) for i in range(len(rows))]
        try:
            pairs.sort(key=sort_key)
        except Exception as e:
            notes.append("order_spec sort failed: %s" % e)
            return "order_failed"
        working["rows"] = [p[0] for p in pairs]
        notes.append("order_spec %s: %d rows" % (
            spec.get("id") or spec.get("order_spec_id"), len(pairs)))
    return None


def apply_dedupe(working: dict, plan: dict, cv: ContractVersion,
                 notes: list, spec: dict = None) -> Optional[str]:
    if spec is not None:
        specs = [spec]
    else:
        specs = [s for s in (plan.get("dedupe_specs") or []) if isinstance(s, dict)]
    if not specs:
        return None
    # Must already be ordered; keep first_in_order only
    for spec in specs:
        keep = str(spec.get("keep") or "")
        if keep != "first_in_order":
            return "dedupe_keep_unknown"
        if not (spec.get("order_id") or spec.get("order_spec_id")):
            return "dedupe_order"
        nid = str(spec.get("key_norm_id") or "")
        col = str(spec.get("key_column") or spec.get("key_catalog_id") or "")
        c = _exact_col(working.get("columns") or [], col) or col
        if not nid or not c:
            return "dedupe_key_norm"
        seen = set()
        kept = []
        for r in working.get("rows") or []:
            nv, err = _norm_value_for_key(r.get(c), plan, nid, cv, c)
            if err == "unclassified_empty":
                return "unclassified_empty"
            if err and err.startswith("empty_class_"):
                key = "__empty__:%s" % err
            elif err:
                return err
            else:
                key = nv
            if key in seen:
                continue
            seen.add(key)
            kept.append(r)
        working["rows"] = kept
        notes.append("dedupe_spec: %d rows kept" % len(kept))
    return None


def apply_aggregate(working: dict, plan: dict, cv: ContractVersion,
                    notes: list, spec: dict = None) -> Optional[str]:
    from .calc import replay_calc_spec, validate_agg_null_policy
    from .accuracy import classify_empty
    if spec is not None:
        specs = [spec]
    else:
        specs = [s for s in (plan.get("agg_specs") or []) if isinstance(s, dict)]
    if not specs:
        return None
    calc_by_id = {
        str(s.get("calc_id")): s
        for s in (plan.get("calc_specs") or []) if isinstance(s, dict) and s.get("calc_id")
    }
    cols = list(working.get("columns") or [])
    for spec in specs:
        cid = str(spec.get("calc_id") or "")
        calc = calc_by_id.get(cid)
        if not calc:
            return "agg_calc_id"
        groups = [str(x) for x in (spec.get("group_columns") or spec.get("group_catalog_ids") or [])]
        if not groups:
            return "agg_groups_missing"
        gcols = []
        for g in groups:
            gc = _exact_col(cols, g) or g
            gcols.append(gc)
        semantics = spec.get("agg_semantics") or calc.get("agg_semantics") or {}
        op0 = ""
        for step in calc.get("steps") or []:
            if isinstance(step, dict) and step.get("op"):
                op0 = str(step["op"])
                break
        # bucket rows
        buckets: Dict[Tuple[str, ...], List[dict]] = {}
        for r in working.get("rows") or []:
            key_parts = []
            for gc in gcols:
                mapping, candidates = _empty_map_for_column(plan, gc)
                klass = classify_empty(r.get(gc), mapping, candidates)
                if klass == "unclassified_empty" or _agg_action(semantics, op0, klass) == "fail":
                    return "agg_semantics_fail"
                if klass != "value":
                    key_parts.append("__%s__" % klass)
                else:
                    key_parts.append("" if r.get(gc) is None else str(r.get(gc)))
            buckets.setdefault(tuple(key_parts), []).append(r)
        out_col = str(calc.get("result_column") or calc.get("result_catalog_id") or "agg_result")
        if out_col not in cols:
            cols.append(out_col)
            working["columns"] = cols
        new_rows = []
        semantics = spec.get("agg_semantics") or calc.get("agg_semantics") or {}
        policy = str(calc.get("agg_null_policy") or "skip")
        for step in calc.get("steps") or []:
            if isinstance(step, dict):
                op = str(step.get("op") or "")
                chk = validate_agg_null_policy(cv, op, policy)
                if not chk.ok:
                    return "agg_null_disallowed"
        col_by_catalog = {}
        for e in plan.get("catalog") or []:
            if isinstance(e, dict) and e.get("catalog_id"):
                col_by_catalog[str(e["catalog_id"])] = str(
                    e.get("field_name") or e.get("orig_col_name") or "")
        for step in calc.get("steps") or []:
            if isinstance(step, dict) and step.get("column"):
                col_by_catalog.setdefault(str(step.get("source_catalog_id") or ""),
                                         str(step.get("column")))
        for gkey, grows in buckets.items():
            # apply empty semantics: drop incomparable
            for gc in gcols:
                pass
            # filter agg inputs by empty class
            in_col = str((calc.get("steps") or [{}])[0].get("column") or "")
            if in_col:
                mapping, candidates = _empty_map_for_column(plan, in_col)
                filtered = []
                for gr in grows:
                    klass = classify_empty(gr.get(in_col), mapping, candidates)
                    if klass == "value":
                        filtered.append(gr)
                    else:
                        op0 = str(((calc.get("steps") or [{}])[0]).get("op") or "sum")
                        rule = _agg_action(semantics, op0, klass)
                        if klass == "unclassified_empty" or rule == "fail":
                            return "agg_semantics_fail"
                        if rule == "incomparable":
                            return "incomparable"
                        if rule in ("skip", None) and klass != "unclassified_empty":
                            if rule is None and policy == "reject":
                                return "agg_null_reject"
                            if rule is None and policy == "propagate":
                                return "incomparable"
                            continue
                        if rule == "count_as_zero":
                            filtered.append(gr)
                        else:
                            filtered.append(gr)
                grows_use = filtered
            else:
                grows_use = grows
            # set norm_value for replay check if declared; else compute into row
            replay = dict(calc)
            # strip declared norm_value for computation then write
            declared = replay.pop("norm_value", None)
            replay.pop("result_value", None)
            from .calc import replay_calc_step, apply_null_policy
            # simplified: run steps manually for sum/count
            result_val = None
            for step in calc.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                op = str(step.get("op") or "")
                scol = str(step.get("column") or in_col)
                vals = [gr.get(scol) for gr in grows_use]
                tokens = list(cv.data.get("null_tokens_default") or [])
                vals2, err = apply_null_policy(vals, policy, tokens)
                if err == "null_rejected":
                    return "agg_null_reject"
                if err == "propagate":
                    return "incomparable"
                try:
                    result_val = replay_calc_step(op, vals2, step.get("param"))
                except ValueError:
                    return "calc_replay_error"
            if declared is not None and str(declared) != str(result_val):
                return "calc_replay_mismatch"
            row_out = {gc: grows[0].get(gc, "") for gc in gcols}
            row_out[out_col] = result_val
            new_rows.append(row_out)
        working["rows"] = new_rows
        notes.append("agg_spec %s: %d groups" % (cid, len(new_rows)))
    return None


def _agg_action(semantics: dict, op: str, klass: str):
    block = semantics.get(op) if isinstance(semantics, dict) else None
    if not isinstance(block, dict):
        return None
    if klass not in block:
        return None
    return str(block.get(klass) or "")


def _empty_map_for_column(plan: dict, column: str, source_table: str = None,
                          side: str = None) -> Tuple[list, list]:
    from .empty_roles import resolve_empty_map
    return resolve_empty_map(plan, column, source_table=source_table, side=side)


def _norm_value_for_key(raw: Any, plan: dict, norm_id: str, cv: ContractVersion,
                        column: str, source_table: str = None,
                        side: str = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (norm_value, error_code).
    Uses classify_empty then replay_norm_steps — never strip/lower locally.
    """
    from .accuracy import classify_empty, replay_norm_steps, resolve_norm_steps
    mapping, candidates = _empty_map_for_column(plan, column)
    klass = classify_empty(raw, mapping, candidates)
    if klass == "unclassified_empty":
        return None, "unclassified_empty"
    if klass in ("vacant", "unknown", "na"):
        # empty classes are not joinable as values unless both sides same class —
        # join equality uses norm_value only for class 'value'
        return None, "empty_class_%s" % klass
    steps = resolve_norm_steps(plan, norm_id)
    if not steps:
        return None, "key_norm_unknown"
    allowed = set(str(x) for x in (cv.data.get("norm_ops") or []))
    allowed |= set(str(x) for x in (cv.data.get("normalize_ops") or []))
    try:
        nv = replay_norm_steps(raw, steps, allowed)
    except ValueError as e:
        return None, "norm_replay_error:%s" % e
    return str(nv), None


def apply_contract_joins(working: dict, tables: dict, joins: list, notes: list,
                         plan: dict = None, cv: Optional[ContractVersion] = None
                         ) -> Tuple[List[dict], Optional[str]]:
    """
    Join on key_norm replay equality only.
    Returns (traces, error_code). error_code set → caller rejects.
    """
    plan = plan or {}
    traces: List[dict] = []
    if cv is None:
        return traces, "contract_incomplete"
    for i, j in enumerate(joins or []):
        if not isinstance(j, dict):
            return traces, "join_bad"
        right_name = str(
            j.get("right_table")
            or (j.get("right_scope") or {}).get("file_id")
            or ""
        ).strip()
        left_col = str(j.get("left_column") or j.get("left_catalog_id") or "").strip()
        right_col = str(j.get("right_column") or j.get("right_catalog_id") or "").strip()
        cat_left = str(j.get("left_field_name") or "").strip()
        cat_right = str(j.get("right_field_name") or "").strip()
        if cat_left:
            left_col = cat_left
        if cat_right:
            right_col = cat_right
        shared = str(j.get("key_norm_id") or "").strip()
        left_nid = str(j.get("left_key_norm_id") or shared or "").strip()
        right_nid = str(j.get("right_key_norm_id") or shared or "").strip()
        if not left_nid or not right_nid:
            return traces, "key_norm_id_missing"
        right = _exact_table(tables, right_name)
        if right is None:
            return traces, "right_table_missing"
        lc = _exact_col(working.get("columns") or [], left_col)
        rc = _exact_col(right.get("columns") or [], right_col)
        if not lc or not rc:
            return traces, "join_columns_missing"
        how = str(j.get("how") or j.get("join_type") or "left").lower()
        if how not in ("left", "inner"):
            how = "left"
        index: Dict[str, List[dict]] = {}
        right_keys: List[str] = []
        for rr in right.get("rows") or []:
            nv, err = _norm_value_for_key(
                rr.get(rc), plan, right_nid, cv, rc,
                source_table=right_name, side="right")
            if err == "unclassified_empty":
                return traces, "unclassified_empty"
            if err and err.startswith("empty_class_"):
                continue  # empty-class rows do not index
            if err:
                return traces, err
            assert nv is not None
            right_keys.append(nv)
            index.setdefault(nv, []).append(rr)
        exp = str(j.get("expected_cardinality") or "")
        if exp in ("1", "1:1") and len(right_keys) != len(set(right_keys)):
            notes.append("duplicate_key on right side of joins[%d]" % i)
            return traces, "duplicate_key"
        new_cols = list(working.get("columns") or [])
        add_cols = []
        for c in right.get("columns") or []:
            if c == rc and rc in new_cols:
                continue
            name = c if c not in new_cols else ("%s~2" % c)
            if name not in new_cols:
                new_cols.append(name)
                add_cols.append((c, name))
        new_rows = []
        for ri, r1 in enumerate(working.get("rows") or []):
            nv, err = _norm_value_for_key(
                r1.get(lc), plan, left_nid, cv, lc,
                source_table=str(plan.get("primary_table") or ""), side="left")
            if err == "unclassified_empty":
                return traces, "unclassified_empty"
            if err and err.startswith("empty_class_"):
                if how == "inner":
                    continue
                matches = [{}]
            elif err:
                return traces, err
            else:
                matches = index.get(nv or "") or []
                if how == "inner" and not matches:
                    continue
                if not matches:
                    matches = [{}]
                if exp in ("1:1", "many:1", "1") and len(matches) > 1:
                    notes.append("duplicate_key matches on joins[%d]" % i)
                    return traces, "duplicate_key"
            for r2 in matches:
                merged = dict(r1)
                for src, dst in add_cols:
                    merged[dst] = r2.get(src, "") if r2 else ""
                new_rows.append(merged)
                traces.append({
                    "output_coord": "join:%d:%d" % (i, ri),
                    "ai_binding_id": str(j.get("ai_binding_id") or ""),
                    "input_coords": ["left:%s" % lc, "right:%s" % rc],
                    "op_id": "join",
                    "catalog_id": str(j.get("left_catalog_id") or ""),
                    "key_norm_id": left_nid,
                })
        working["columns"] = new_cols
        working["rows"] = new_rows
        from .accuracy import resolve_norm_steps
        left_steps = resolve_norm_steps(plan, left_nid)
        right_steps = resolve_norm_steps(plan, right_nid)
        notes.append(
            "join[%d] %s left_key_norm_id=%s left_steps=%s "
            "right_key_norm_id=%s right_steps=%s steps_itemwise_equal=%s "
            "on norm(%s)=norm(%s) → %d rows" % (
                i, how, left_nid,
                json.dumps(left_steps, ensure_ascii=False, sort_keys=True),
                right_nid,
                json.dumps(right_steps, ensure_ascii=False, sort_keys=True),
                left_steps == right_steps,
                lc, rc, len(new_rows),
            )
        )
    return traces, None


def apply_derive(working: dict, derived: list, notes: list) -> List[dict]:
    """Exact condition ops on exact columns."""
    traces = []
    cols = list(working.get("columns") or [])
    rows = working.get("rows") or []
    for i, dc in enumerate(derived or []):
        if not isinstance(dc, dict):
            continue
        set_col = str(dc.get("set_column") or "").strip()
        if not set_col:
            continue
        if set_col not in cols:
            cols.append(set_col)
            working["columns"] = cols
        true_v = str(dc.get("true_value") or "是")
        false_v = str(dc.get("false_value") or "否")
        logic = str(dc.get("logic") or "and").lower()
        conds = [c for c in (dc.get("conditions") or []) if isinstance(c, dict)]
        yes = 0
        for ri, r in enumerate(rows):
            flags = []
            for cond in conds:
                col = _exact_col(cols, str(cond.get("column") or ""))
                if not col:
                    flags.append(False)
                    continue
                cell = str(r.get(col, "") or "")
                op = str(cond.get("op") or "eq").lower()
                vals = [str(v) for v in (cond.get("values") or []) if str(v).strip()]
                value = str(cond.get("value") or "")
                if op == "contains_any":
                    flags.append(any(v in cell for v in vals))
                elif op == "contains_all":
                    flags.append(all(v in cell for v in vals) if vals else False)
                elif op in ("contains", "contain"):
                    flags.append(bool(value) and value in cell)
                elif op in ("eq", "=="):
                    targets = vals or ([value] if value else [])
                    flags.append(any(cell.strip() == t for t in targets) if targets else False)
                elif op in ("neq", "!=", "not_eq"):
                    targets = vals or ([value] if value else [])
                    flags.append(all(cell.strip() != t for t in targets) if targets else bool(cell.strip()))
                elif op in ("not_empty", "nonempty"):
                    flags.append(bool(cell.strip()))
                else:
                    flags.append(False)
            if not flags:
                hit = False
            elif logic == "or":
                hit = any(flags)
            else:
                hit = all(flags)
            r[set_col] = true_v if hit else false_v
            if hit:
                yes += 1
            traces.append({
                "output_coord": "%d:%s" % (ri, set_col),
                "ai_binding_id": str(dc.get("ai_binding_id") or ""),
                "input_coords": [
                    "%d:%s" % (ri, str(c.get("column") or ""))
                    for c in conds if c.get("column")
                ],
                "op_id": "derive",
                "catalog_id": str(dc.get("catalog_id") or ""),
            })
        notes.append("derived[%d] %s: %d/%d true" % (i, set_col, yes, len(rows)))
    return traces


def apply_join(working: dict, tables: dict, plan: dict, cv: ContractVersion,
               notes: list, spec: dict) -> Tuple[List[dict], Optional[str]]:
    return apply_contract_joins(
        working, tables, [spec], notes, plan=plan, cv=cv)


def apply_project(working: dict, plan: dict, notes: list,
                  spec: dict = None) -> Tuple[dict, List[dict]]:
    cols = None
    if spec is not None:
        cols = list(spec.get("output_columns") or [])
    return project_output(working, plan, notes, output_columns=cols)


def project_output(working: dict, plan: dict, notes: list,
                   output_columns: list = None) -> Tuple[dict, List[dict]]:
    """Project to output_columns using bindings map; exact column names only."""
    traces = []
    raw_cols = output_columns if output_columns is not None else (plan.get("output_columns") or [])
    out_cols = [str(c).strip() for c in raw_cols if str(c).strip()]
    if not out_cols:
        notes.append("output_columns empty — refuse project")
        return {
            "name": working.get("name") or "合成表",
            "columns": [],
            "rows": [],
            "plan_notes": notes,
        }, traces
    cat_names = _catalog_name_map(plan)
    src_map = _binding_source_map(plan)
    # resolve each out col to a working column
    resolved = []
    for oc in out_cols:
        # catalog id as output?
        if oc in cat_names and cat_names[oc]:
            logical = cat_names[oc]
        else:
            logical = oc
        src = src_map.get(oc) or src_map.get(logical) or ""
        work_cols = working.get("columns") or []
        if src and _exact_col(work_cols, src):
            resolved.append((oc, src))
        elif _exact_col(work_cols, logical):
            resolved.append((oc, logical))
        elif _exact_col(work_cols, oc):
            resolved.append((oc, oc))
        else:
            notes.append("project: output column %r has no exact source — blank" % oc)
            resolved.append((oc, None))
    rows_out = []
    for ri, r in enumerate(working.get("rows") or []):
        nr = {}
        for oc, src in resolved:
            nr[oc] = r.get(src, "") if src else ""
            traces.append({
                "output_coord": "%d:%s" % (ri, oc),
                "ai_binding_id": "",
                "input_coords": ["%d:%s" % (ri, src)] if src else [],
                "op_id": "project",
                "catalog_id": oc if oc.startswith("#") else "",
            })
        rows_out.append(nr)
    result = {
        "name": working.get("name") or "合成表",
        "columns": out_cols,
        "rows": rows_out,
        "plan_notes": notes,
    }
    notes.append("project: %d cols × %d rows" % (len(out_cols), len(rows_out)))
    return result, traces


def validate_traces(traces: list, binding_ids: Set[str], known_coords: Set[str],
                    require_binding: bool = False) -> ValidationResult:
    """P0 A1: each trace must have op + input_coords; binding id if required."""
    r = ValidationResult(ok=True)
    for i, t in enumerate(traces or []):
        if not isinstance(t, dict):
            r.fail("trace_bad", "trace not object", "traces[%d]" % i)
            continue
        if not t.get("op_id"):
            r.fail("trace_op_missing", "op_id missing", "traces[%d]" % i)
        inputs = t.get("input_coords") or []
        if not isinstance(inputs, list):
            r.fail("trace_inputs_bad", "input_coords not list", "traces[%d]" % i)
        bid = str(t.get("ai_binding_id") or "")
        if require_binding and bid and bid not in binding_ids:
            r.fail("trace_binding_missing", "ai_binding_id not in catalog/bindings",
                   "traces[%d]" % i)
        if known_coords:
            for c in inputs:
                # join: and left:/right: synthetic coords are allowed without snapshot
                cs = str(c)
                if cs.startswith("left:") or cs.startswith("right:") or ":" in cs:
                    continue
                if cs not in known_coords:
                    r.fail("trace_coord_unknown", "input coord not in snapshot: %s" % cs,
                           "traces[%d]" % i)
    return r


def execute_mechanical(tables: dict, plan: dict, notes: list = None,
                       cv: Optional[ContractVersion] = None) -> dict:
    """
    v1.1.x ops[] only. Walk fixed_op_order → OPERATOR_DISPATCH → apply_*.
    No free-key plan path; no conversion to a legacy DSL.
    """
    notes = notes if notes is not None else []
    if cv is None:
        try:
            cv = load_contract(str(plan.get("contract_version") or "") or None)
        except Exception as e:
            notes.append("contract load failed in executor: %s" % e)
            return {
                "name": "合成表", "columns": [], "rows": [], "plan_notes": notes,
                "contract_rejected": True, "contract_status": "version_rejected",
            }

    from .accuracy import enforce_declared_accuracy
    from .ops_plan import (
        OPERATOR_DISPATCH,
        is_ops_contract,
        iter_ops_fixed_order,
        validate_ops_plan,
    )

    if not is_ops_contract(cv):
        notes.append("contract_version %s not ops[] contract — refuse" % cv.version)
        return {
            "name": "合成表", "columns": [], "rows": [], "plan_notes": notes,
            "contract_rejected": True,
            "contract_status": "version_rejected",
            "contract_issues": [{
                "code": "ops_version_required",
                "message": "executor accepts v1.1.x ops[] only",
                "path": "plan.contract_version",
                "state_id": "version_rejected",
            }],
            "synth_plan": plan, "traces": [], "trace_ok": False, "_mechanical": False,
        }

    vr = validate_ops_plan(plan, cv)
    if not vr.ok:
        notes.append("ops plan validation failed: %d" % len(vr.issues))
        return {
            "name": "合成表", "columns": [], "rows": [], "plan_notes": notes,
            "contract_rejected": True,
            "contract_status": vr.issues[0].state_id if vr.issues else "contract_incomplete",
            "contract_issues": [
                {"code": i.code, "message": i.message, "path": i.path, "state_id": i.state_id}
                for i in vr.issues
            ],
            "synth_plan": plan, "traces": [], "trace_ok": False, "_mechanical": False,
        }

    primary_name = str(plan.get("primary_table") or "").strip()
    picked = _exact_table(tables, primary_name)
    if picked is None:
        notes.append("primary_table %r not found (exact) — refuse execute" % primary_name)
        return {
            "name": "合成表", "columns": [], "rows": [], "plan_notes": notes,
            "contract_rejected": True, "contract_status": "contract_incomplete",
        }

    working = {
        "name": picked.get("name") or primary_name or "合成表",
        "columns": list(picked.get("columns") or []),
        "rows": [dict(r) for r in (picked.get("rows") or [])],
        "_fill_meta": {},
    }

    acc0 = enforce_declared_accuracy(plan, cv)
    if not acc0.ok:
        notes.append("accuracy checks failed: %d" % len(acc0.issues))
        return {
            "name": "合成表", "columns": [], "rows": [], "plan_notes": notes,
            "contract_rejected": True,
            "contract_status": acc0.issues[0].state_id if acc0.issues else "fail",
            "contract_issues": [
                {"code": i.code, "message": i.message, "path": i.path, "state_id": i.state_id}
                for i in acc0.issues
            ],
            "synth_plan": plan, "traces": [], "trace_ok": False, "_mechanical": False,
        }
    from .empty_roles import shared_map_reports
    rt = plan.get("runtime_notes")
    if not isinstance(rt, list):
        rt = []
        plan["runtime_notes"] = rt
    for line in shared_map_reports(plan):
        notes.append(line)
        if line not in rt:
            rt.append(line)

    dispatch_fns = {
        name: globals()[name]
        for name in OPERATOR_DISPATCH.values()
        if name in globals()
    }
    all_traces: List[dict] = []
    result: Optional[dict] = None

    for op, fn_name, spec in iter_ops_fixed_order(plan, cv):
        fn = dispatch_fns.get(fn_name)
        if fn is None:
            return _reject(notes, "op_no_dispatch", "ops.%s" % op, plan=plan)
        if op == "join":
            traces, err = fn(working, tables, plan, cv, notes, spec)
            if err:
                return _reject(notes, err, "ops.join", plan=plan)
            all_traces.extend(traces or [])
        elif op == "filter":
            cond = {
                "column": spec.get("column"),
                "op": spec.get("op"),
                "value": spec.get("value"),
            }
            if spec.get("values") is not None:
                cond["values"] = spec.get("values")
            err = fn(working, [cond], notes, cv=cv, plan=plan)
            if err:
                return _reject(notes, err, "ops.filter", plan=plan)
        elif op == "derive":
            body = dict(spec)
            body["set_column"] = spec.get("set_column") or spec.get("column")
            all_traces.extend(fn(working, [body], notes))
        elif op == "map_values":
            all_traces.extend(fn(working, [spec], notes))
        elif op in ("order", "dedupe", "aggregate"):
            err = fn(working, plan, cv, notes, spec=spec)
            if err:
                return _reject(notes, err, "ops.%s" % op, plan=plan)
        elif op == "project":
            result, proj_traces = fn(working, plan, notes, spec=spec)
            all_traces.extend(proj_traces)
        else:
            return _reject(notes, "op_no_dispatch", "ops.%s" % op, plan=plan)

    if result is None:
        notes.append("project op absent — refuse empty project")
        return {
            "name": "合成表", "columns": [], "rows": [], "plan_notes": notes,
            "contract_rejected": True, "contract_status": "contract_incomplete",
            "synth_plan": plan, "traces": all_traces, "trace_ok": False,
            "_mechanical": False,
        }

    if plan.get("row_cover") is not None:
        src_ids = set(str(x) for x in (plan.get("source_row_ids") or []))
        rc = validate_row_cover(plan.get("row_cover") or [], src_ids, cv)
        if not rc.ok:
            notes.append("row_cover validation failed")
            result["contract_rejected"] = True
            result["contract_status"] = "contract_incomplete"
            result["contract_issues"] = [
                {"code": i.code, "message": i.message, "path": i.path}
                for i in rc.issues
            ]
            result["columns"] = []
            result["rows"] = []
            result["plan_notes"] = notes
            return result

    binding_ids = set()
    for b in plan.get("bindings") or []:
        if isinstance(b, dict) and b.get("ai_binding_id"):
            binding_ids.add(str(b["ai_binding_id"]))
        if isinstance(b, dict) and b.get("target_field_id"):
            binding_ids.add(str(b["target_field_id"]))
    known_coords = set(str(c) for c in (plan.get("sheet_data_coords") or []))
    tr = validate_traces(all_traces, binding_ids, known_coords, require_binding=False)
    result["traces"] = all_traces
    result["trace_ok"] = tr.ok
    if not tr.ok:
        notes.append("trace validation issues: %d" % len(tr.issues))
        result["trace_issues"] = [
            {"code": i.code, "message": i.message, "path": i.path} for i in tr.issues
        ]
    result["plan_notes"] = notes
    result["synth_plan"] = plan
    result["contract_rejected"] = False
    result["contract_status"] = str(plan.get("status") or "pass")
    result["_mechanical"] = True
    return result
