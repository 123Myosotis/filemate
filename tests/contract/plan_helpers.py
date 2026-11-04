# -*- coding: utf-8 -*-
"""Shared plan fragments for accuracy hardfix fixtures."""
from __future__ import annotations


def identity_norm(norm_id: str = "kn_id") -> dict:
    return {
        "norm_id": norm_id,
        "idempotent": True,
        "steps": [{"op": "identity"}],
    }


def empty_map(column: str, source_table: str = "", side: str = "",
              map_id: str = "", shared_map_id: str = "",
              extra_raw_enums=None, extra_candidates=None) -> dict:
    mapping = [
        {"raw": "", "enum": "vacant"},
        {"raw": "未知", "enum": "unknown"},
        {"raw": "不适用", "enum": "na"},
    ]
    for row in extra_raw_enums or []:
        mapping.append(row)
    candidates = ["", "未知", "不适用", "N/A", "n/a"]
    for c in extra_candidates or []:
        if c not in candidates:
            candidates.append(c)
    mid = map_id or "em_%s_%s_%s" % (source_table or "na", side or "na", column)
    out = {
        "map_id": mid,
        "source_table": source_table,
        "column": column,
        "mapping": mapping,
        "candidates": candidates,
    }
    if side:
        out["side"] = side
    if shared_map_id:
        out["shared_map_id"] = shared_map_id
    return out


def agg_semantics(op: str = "count", unclassified: str = "fail") -> dict:
    """Five classes. unclassified_empty is fail unless a test is proving rejection."""
    return {
        op: {
            "vacant": "skip",
            "unknown": "skip",
            "na": "skip",
            "incomparable": "incomparable",
            "unclassified_empty": unclassified,
        }
    }


def attach_force_layer(plan: dict, columns=None, join_key_norm_id: str = "kn_id",
                       origins=None) -> dict:
    """Norm + one map per role key. `columns` is not a map list.

    Join left and join right are separate entries. When both sides use the same
    column name, the right entry sets shared_map_id to the left map. It does
    not omit the right entry.
    """
    from pipeline.synth_contract.empty_roles import collect_role_fields, find_map_entry

    plan = dict(plan)
    if origins:
        plan["column_origins"] = dict(origins)
    specs = list(plan.get("norm_specs") or [])
    if not any(s.get("norm_id") == join_key_norm_id for s in specs if isinstance(s, dict)):
        specs.append(identity_norm(join_key_norm_id))
    plan["norm_specs"] = specs
    maps = [m for m in (plan.get("empty_token_maps") or []) if isinstance(m, dict)]
    joins = []
    for j in list(plan.get("joins") or []) + list(plan.get("contract_joins") or []):
        if not isinstance(j, dict):
            continue
        jj = dict(j)
        if not jj.get("key_norm_id") and not jj.get("left_key_norm_id"):
            jj["key_norm_id"] = join_key_norm_id
        joins.append(jj)
    if joins:
        if plan.get("contract_joins"):
            plan["contract_joins"] = joins
            plan["joins"] = []
        else:
            plan["joins"] = joins

    def _remember(entry: dict) -> dict:
        maps.append(entry)
        plan["empty_token_maps"] = maps
        return entry

    for role in collect_role_fields(plan):
        if find_map_entry(plan, role):
            continue
        if role["role"] == "join_key" and role.get("side") == "right":
            left = None
            for prev in collect_role_fields(plan):
                if prev["role"] == "join_key" and prev.get("side") == "left" and prev["field"] == role["field"]:
                    left = find_map_entry(plan, prev)
                    break
            if left and left.get("map_id"):
                _remember(empty_map(
                    role["field"], source_table=role.get("source_table") or "",
                    side="right", shared_map_id=str(left["map_id"]),
                    map_id="em_%s_right_%s" % (role.get("source_table") or "na", role["field"]),
                ))
                continue
        _remember(empty_map(
            role["field"],
            source_table=role.get("source_table") or "",
            side=role.get("side") or "",
        ))
    plan["empty_token_maps"] = maps
    return plan


def to_ops_plan(plan: dict) -> dict:
    """Rewrite a v1.0-style mechanical plan into v1.1.0 ops[] shape."""
    import copy
    plan = copy.deepcopy(plan)
    ops = []
    joins_out = []
    for i, j in enumerate(list(plan.get("joins") or []) + list(plan.get("contract_joins") or [])):
        if not isinstance(j, dict):
            continue
        body = dict(j)
        jid = str(body.get("id") or ("j%d" % (i + 1)))
        body["id"] = jid
        joins_out.append(body)
        ops.append({"op": "join", "join_ref": jid})
    filters_out = []
    for i, f in enumerate(list(plan.get("filters") or []) + list(plan.get("keep_row_when") or [])):
        if not isinstance(f, dict):
            continue
        body = dict(f)
        fid = str(body.get("id") or ("f%d" % (i + 1)))
        body["id"] = fid
        filters_out.append(body)
        ops.append({"op": "filter", "filter_ref": fid})
    derives_out = []
    for i, d in enumerate(list(plan.get("derives") or []) + list(plan.get("derived_columns") or [])):
        if not isinstance(d, dict):
            continue
        body = dict(d)
        did = str(body.get("id") or ("derv%d" % (i + 1)))
        body["id"] = did
        derives_out.append(body)
        ops.append({"op": "derive", "derive_ref": did})
    maps_out = []
    for i, m in enumerate(list(plan.get("value_maps") or []) + list(plan.get("value_mappings") or [])):
        if not isinstance(m, dict):
            continue
        body = dict(m)
        mid = str(body.get("id") or ("m%d" % (i + 1)))
        body["id"] = mid
        maps_out.append(body)
        ops.append({"op": "map_values", "map_ref": mid})
    order_out = []
    for i, o in enumerate(plan.get("order_specs") or []):
        if not isinstance(o, dict):
            continue
        body = dict(o)
        oid = str(body.get("id") or body.get("order_spec_id") or ("o%d" % (i + 1)))
        body["id"] = oid
        body.pop("order_spec_id", None)
        order_out.append(body)
        ops.append({"op": "order", "order_ref": oid})
    dedupe_out = []
    for i, d in enumerate(plan.get("dedupe_specs") or []):
        if not isinstance(d, dict):
            continue
        body = dict(d)
        did = str(body.get("id") or ("d%d" % (i + 1)))
        body["id"] = did
        if body.get("order_spec_id") and not body.get("order_id"):
            body["order_id"] = body.get("order_spec_id")
        body.pop("order_spec_id", None)
        dedupe_out.append(body)
        ops.append({"op": "dedupe", "dedupe_ref": did})
    agg_out = []
    for i, a in enumerate(plan.get("agg_specs") or []):
        if not isinstance(a, dict):
            continue
        body = dict(a)
        aid = str(body.get("id") or ("a%d" % (i + 1)))
        body["id"] = aid
        if body.get("order_spec_id") and not body.get("order_id"):
            body["order_id"] = body.get("order_spec_id")
        body.pop("order_spec_id", None)
        agg_out.append(body)
        ops.append({"op": "aggregate", "agg_ref": aid})
    project_out = []
    cols = list(plan.get("output_columns") or [])
    if plan.get("project_specs"):
        for i, p in enumerate(plan.get("project_specs") or []):
            if not isinstance(p, dict):
                continue
            body = dict(p)
            pid = str(body.get("id") or ("p%d" % (i + 1)))
            body["id"] = pid
            project_out.append(body)
            ops.append({"op": "project", "project_ref": pid})
    elif cols:
        project_out = [{"id": "p1", "output_columns": cols}]
        ops.append({"op": "project", "project_ref": "p1"})

    plan["ops"] = ops
    plan["joins"] = joins_out
    plan["contract_joins"] = []
    plan["filters"] = filters_out
    plan["derives"] = derives_out
    plan["value_maps"] = maps_out
    plan["order_specs"] = order_out
    plan["dedupe_specs"] = dedupe_out
    plan["agg_specs"] = agg_out
    plan["project_specs"] = project_out
    for k in (
        "keep_row_when",
        "derived_columns",
        "value_mappings",
        "output_columns",
        "column_origins",
    ):
        plan.pop(k, None)
    # Option A: non-join roles resolve source_table = primary_table
    primary = str(plan.get("primary_table") or "")
    for m in plan.get("empty_token_maps") or []:
        if isinstance(m, dict) and not m.get("side"):
            m["source_table"] = primary
    return plan
