# -*- coding: utf-8 -*-
"""Role → empty_token_map requirement (handbook §7.2). No guessing.

A map is keyed by (source_table, column) plus join side. The same column name
on two tables is two requirements. A second entry may name shared_map_id; it
does not inherit another table's map unless that id is written on the entry.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .validate import ValidationResult

EMPTY_MAP_REQUIRED: Dict[str, bool] = {
    "join_key": True,
    "dedupe_key": True,
    "agg_group": True,
    "agg_input": True,
    "order_key": True,
    "filter_eq": True,
    "filter_in": True,
    "order_tie_break": True,
}


def collect_role_fields(plan: dict) -> List[dict]:
    """Each item: role, field, source_table, side (join only)."""
    out: List[dict] = []
    primary = str(plan.get("primary_table") or "")
    origins = plan.get("column_origins") if isinstance(plan.get("column_origins"), dict) else {}

    def _src(column: str) -> str:
        if column in origins and origins.get(column):
            return str(origins[column])
        return primary

    joins = list(plan.get("contract_joins") or []) + list(plan.get("joins") or [])
    for j in joins:
        if not isinstance(j, dict):
            continue
        right_table = str(j.get("right_table") or (j.get("right_scope") or {}).get("file_id") or "")
        for key, side, table in (
            (("left_column", "left_field_name", "left_catalog_id"), "left", primary),
            (("right_column", "right_field_name", "right_catalog_id"), "right", right_table),
        ):
            field = ""
            for k in key:
                if j.get(k):
                    field = str(j[k])
                    break
            if field:
                out.append({
                    "role": "join_key",
                    "field": field,
                    "side": side,
                    "source_table": table,
                })
    for cond in list(plan.get("keep_row_when") or []) + list(plan.get("filters") or []):
        if not isinstance(cond, dict):
            continue
        op = str(cond.get("op") or "eq").lower()
        col = str(cond.get("column") or "")
        if not col:
            continue
        if op in ("eq", "neq", "==", "!=", "not_eq"):
            role = "filter_eq"
        elif op in ("contains_any", "contains_all", "in"):
            role = "filter_in"
        else:
            continue
        out.append({"role": role, "field": col, "side": "", "source_table": _src(col)})
    for spec in plan.get("order_specs") or []:
        if not isinstance(spec, dict):
            continue
        for k in spec.get("keys") or []:
            if not isinstance(k, dict):
                continue
            field = str(k.get("column") or k.get("catalog_id") or "")
            if field:
                out.append({
                    "role": "order_key", "field": field, "side": "",
                    "source_table": _src(field),
                })
        tb = spec.get("tie_break")
        if isinstance(tb, dict):
            field = str(tb.get("column") or tb.get("catalog_id") or "")
            if field:
                out.append({
                    "role": "order_tie_break", "field": field, "side": "",
                    "source_table": _src(field),
                })
    for d in plan.get("dedupe_specs") or []:
        if not isinstance(d, dict):
            continue
        field = str(d.get("key_column") or d.get("key_catalog_id") or "")
        if field:
            out.append({
                "role": "dedupe_key", "field": field, "side": "",
                "source_table": _src(field),
            })
    for a in plan.get("agg_specs") or []:
        if not isinstance(a, dict):
            continue
        for g in list(a.get("group_catalog_ids") or []) + list(a.get("group_columns") or []):
            field = str(g)
            out.append({
                "role": "agg_group", "field": field, "side": "",
                "source_table": _src(field),
            })
        for key in ("input_column", "input_catalog_id"):
            if a.get(key):
                field = str(a[key])
                out.append({
                    "role": "agg_input", "field": field, "side": "",
                    "source_table": _src(field),
                })
    for c in plan.get("calc_specs") or []:
        if not isinstance(c, dict):
            continue
        for step in c.get("steps") or []:
            if not isinstance(step, dict):
                continue
            for key in ("column", "source_catalog_id", "catalog_id"):
                if step.get(key):
                    field = str(step[key])
                    out.append({
                        "role": "agg_input", "field": field, "side": "",
                        "source_table": _src(field),
                    })
    return out


def _maps(plan: dict) -> List[dict]:
    return [m for m in (plan.get("empty_token_maps") or []) if isinstance(m, dict)]


def find_map_entry(plan: dict, role: dict) -> Optional[dict]:
    """Entry that satisfies this role. Join sides do not satisfy each other."""
    field = str(role.get("field") or "")
    source = str(role.get("source_table") or "")
    side = str(role.get("side") or "")
    hits = []
    for m in _maps(plan):
        if str(m.get("column") or "") != field:
            continue
        m_source = str(m.get("source_table") or "")
        m_side = str(m.get("side") or "")
        if role.get("role") == "join_key":
            if m_side != side:
                continue
            if source and m_source and m_source != source:
                continue
            hits.append(m)
            continue
        if source and m_source and m_source != source:
            continue
        if side and m_side and m_side != side:
            continue
        hits.append(m)
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]
    exact = [m for m in hits if str(m.get("source_table") or "") == source and not m.get("side")]
    if len(exact) == 1:
        return exact[0]
    return hits[0]


def shared_target(plan: dict, entry: dict) -> Optional[dict]:
    shared = str(entry.get("shared_map_id") or "")
    if not shared:
        return None
    for m in _maps(plan):
        if str(m.get("map_id") or "") == shared:
            return m
    return None


def resolve_map_body(plan: dict, entry: Optional[dict]) -> Optional[dict]:
    """The entry's own mapping. shared_map_id does not replace it."""
    return entry


def shared_map_reports(plan: dict) -> List[str]:
    """Provenance check only. Mismatch is reported; each side keeps its mapping."""
    import json
    reports: List[str] = []
    for entry in _maps(plan):
        if not entry.get("shared_map_id"):
            continue
        target = shared_target(plan, entry)
        if target is None:
            continue
        left = json.dumps(entry.get("mapping"), ensure_ascii=False, sort_keys=True)
        right = json.dumps(target.get("mapping"), ensure_ascii=False, sort_keys=True)
        if left != right:
            reports.append(
                "shared_map_mismatch map_id=%s shared_map_id=%s" % (
                    entry.get("map_id"), entry.get("shared_map_id"))
            )
    return reports


def resolve_empty_map(plan: dict, column: str, source_table: str = None,
                      side: str = None) -> Tuple[list, list]:
    role = {
        "role": "join_key" if side in ("left", "right") else "",
        "field": column,
        "source_table": source_table or "",
        "side": side or "",
    }
    if not side:
        # non-join lookup: any entry for this source+column, preferring a body
        field = str(column)
        source = str(source_table or plan.get("primary_table") or "")
        hits = []
        for m in _maps(plan):
            if str(m.get("column") or "") != field:
                continue
            m_source = str(m.get("source_table") or "")
            if source and m_source and m_source != source:
                continue
            hits.append(m)
        body_hits = [m for m in hits if not m.get("shared_map_id")]
        entry = body_hits[0] if body_hits else (hits[0] if hits else None)
    else:
        entry = find_map_entry(plan, role)
    body = resolve_map_body(plan, entry)
    if not body:
        return [], []
    return list(body.get("mapping") or []), list(body.get("candidates") or [])


def enforce_empty_token_maps(plan: dict) -> ValidationResult:
    r = ValidationResult(ok=True)
    for role in collect_role_fields(plan):
        need = EMPTY_MAP_REQUIRED.get(role["role"])
        if need is None:
            r.fail("empty_role_unknown", "role not in handbook table: %s" % role["role"], "empty_roles")
            continue
        if not need:
            continue
        entry = find_map_entry(plan, role)
        if entry is None:
            r.fail(
                "empty_token_map_missing",
                "role %s side %s field %s source %s missing empty_token_map" % (
                    role["role"], role.get("side") or "-", role["field"], role.get("source_table") or "-"),
                "empty_token_maps",
                state_id="contract_incomplete",
            )
            continue
        if entry.get("shared_map_id") and shared_target(plan, entry) is None:
            r.fail(
                "shared_map_unresolved",
                "shared_map_id %s not found" % entry.get("shared_map_id"),
                "empty_token_maps",
                state_id="contract_incomplete",
            )
    return r
