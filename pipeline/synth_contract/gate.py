# -*- coding: utf-8 -*-
"""
Plan intake gate: load contract, validate envelope + present sections.
Incomplete → reject. No semantic repair.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from .registry import ContractVersion, load_contract
from .scope import Scope, parse_scope
from .normalize import validate_normalize_spec
from .validate import (
    ValidationResult,
    validate_binding,
    validate_dsl_continuity,
    validate_global_judgment,
    validate_join_spec,
    validate_plan_envelope,
    validate_region_cover,
    validate_row_cover,
    validate_verifiable_basis,
)


def _catalog_ids_from_plan(plan: dict) -> Set[str]:
    ids: Set[str] = set()
    for entry in plan.get("catalog") or []:
        if isinstance(entry, dict) and entry.get("catalog_id"):
            ids.add(str(entry["catalog_id"]))
    for bid in plan.get("catalog_ids") or []:
        ids.add(str(bid))
    # bindings may declare target ids
    for b in plan.get("bindings") or []:
        if isinstance(b, dict) and b.get("target_field_id"):
            ids.add(str(b["target_field_id"]))
    for j in plan.get("contract_joins") or []:
        if not isinstance(j, dict):
            continue
        for k in ("left_catalog_id", "right_catalog_id", "grain"):
            if j.get(k):
                ids.add(str(j[k]))
    return ids


def _catalog_scopes_from_plan(plan: dict) -> Dict[str, Scope]:
    out: Dict[str, Scope] = {}
    for entry in plan.get("catalog") or []:
        if not isinstance(entry, dict):
            continue
        cid = str(entry.get("catalog_id") or "")
        sc = parse_scope(entry.get("scope"), require_upto="region")
        if cid and sc is not None:
            out[cid] = sc
    return out


def _sheet_coords_from_tables(tables: dict, file_id: str, sheet: str) -> Set[str]:
    """
    Collect declared cell coords from table IR if present.
    Does not invent coords from row indices — only uses explicit fields.
    """
    coords: Set[str] = set()
    for name, t in (tables or {}).items():
        if not isinstance(t, dict):
            continue
        t_file = str(t.get("file_id") or t.get("source_file") or name)
        t_sheet = str(t.get("sheet") or t.get("sheet_name") or "")
        if file_id and t_file != file_id:
            continue
        if sheet and t_sheet and t_sheet != sheet:
            continue
        for c in t.get("data_coords") or []:
            coords.add(str(c))
        for c in t.get("cell_coords") or []:
            coords.add(str(c))
    return coords


def gate_plan(
    plan: dict,
    tables: Optional[dict] = None,
    contract_dir: Optional[str] = None,
    expected_hash: Optional[str] = None,
) -> Tuple[ValidationResult, Optional[ContractVersion]]:
    """
    Validate plan against the contract version it declares.
    Returns (result, loaded_contract_or_None).
    """
    tables = tables or {}
    if not isinstance(plan, dict):
        r = ValidationResult(ok=False)
        r.fail("plan_not_object", "plan not object", "plan")
        return r, None

    ver = str(plan.get("contract_version") or "").strip()
    if not ver:
        r = ValidationResult(ok=False)
        r.fail(
            "contract_version_missing",
            "plan missing contract_version — refuse execute",
            "plan.contract_version",
            state_id="contract_incomplete",
        )
        return r, None

    try:
        cv = load_contract(
            version=ver,
            contract_dir=contract_dir,
            expected_hash=expected_hash or plan.get("contract_hash"),
        )
    except (FileNotFoundError, ValueError) as e:
        r = ValidationResult(ok=False)
        r.fail(
            "contract_load_fail",
            str(e),
            "plan.contract_version",
            state_id="version_rejected",
        )
        return r, None

    r = validate_plan_envelope(cv, plan, expected_hash=expected_hash)

    # status await_user → stop before execute (mechanical)
    if str(plan.get("status") or "") == "await_user":
        r.fail(
            "await_user",
            "plan status is await_user — refuse execute",
            "plan.status",
            state_id="await_user",
        )
    if str(plan.get("status") or "") == "draft":
        r.fail(
            "status_not_executable",
            "draft is not executable",
            "plan.status",
            state_id="contract_incomplete",
        )
    from .triage import budget_incomparable_keys
    for path in budget_incomparable_keys(plan):
        r.fail(
            "budget_comparison_error",
            "budget limits/actual not comparable at %s" % path,
            path,
            state_id="contract_incomplete",
        )
        notes_rt = plan.get("runtime_notes")
        if not isinstance(notes_rt, list):
            notes_rt = []
            plan["runtime_notes"] = notes_rt
        line = "budget_comparison_error %s" % path
        if line not in notes_rt:
            notes_rt.append(line)

    catalog_ids = _catalog_ids_from_plan(plan)
    known_scopes = _catalog_scopes_from_plan(plan)

    for i, b in enumerate(plan.get("bindings") or []):
        if isinstance(b, dict):
            r = r.merge(validate_binding(
                cv, b, catalog_ids, known_scopes, path="bindings[%d]" % i))

    for i, j in enumerate(plan.get("contract_joins") or []):
        if isinstance(j, dict):
            r = r.merge(validate_join_spec(
                cv, j, catalog_ids, path="contract_joins[%d]" % i))

    regions = plan.get("regions")
    if regions is not None:
        # Prefer AI-declared sheet_data_coords on plan; else only explicit table coords
        sheet_coords = set(str(c) for c in (plan.get("sheet_data_coords") or []))
        if not sheet_coords and regions:
            sc0 = parse_scope((regions[0] or {}).get("scope"), "sheet") if regions else None
            if sc0:
                sheet_coords = _sheet_coords_from_tables(
                    tables, sc0.file_id, sc0.sheet)
        if not sheet_coords:
            r.fail(
                "sheet_data_coords_missing",
                "regions present but sheet_data_coords absent — cannot verify cover",
                "plan.sheet_data_coords",
            )
        else:
            r = r.merge(validate_region_cover(regions, sheet_coords))

    dsl = plan.get("dsl_steps")
    if dsl is not None:
        r = r.merge(validate_dsl_continuity(dsl))

    known_coords: Set[str] = set(str(c) for c in (plan.get("sheet_data_coords") or []))
    for t in tables.values():
        if isinstance(t, dict):
            for c in t.get("data_coords") or []:
                known_coords.add(str(c))
            for c in t.get("cell_coords") or []:
                known_coords.add(str(c))

    summary_hashes = dict(plan.get("summary_hashes") or {})
    sample_manifests = dict(plan.get("sample_manifests") or {})
    judgments = plan.get("judgments")
    if judgments is not None:
        seen_types: Set[str] = set()
        for i, j in enumerate(judgments):
            if isinstance(j, dict):
                r = r.merge(validate_global_judgment(
                    cv, j, known_coords, summary_hashes, sample_manifests,
                    path="judgments[%d]" % i))
                seen_types.add(str(j.get("global_judgment_type") or ""))
        req = plan.get("required_judgment_types") or []
        for t in req:
            if str(t) not in seen_types:
                r.fail(
                    "judgment_type_missing",
                    "required_judgment_types missing type %s" % t,
                    "plan.judgments",
                )

    row_cover = plan.get("row_cover")
    if row_cover is not None:
        src_ids = set(str(x) for x in (plan.get("source_row_ids") or []))
        if not src_ids:
            r.fail(
                "source_row_ids_missing",
                "row_cover present but source_row_ids absent",
                "plan.source_row_ids",
            )
        else:
            r = r.merge(validate_row_cover(row_cover, src_ids, cv))

    for i, spec in enumerate(plan.get("normalize_specs") or []):
        if isinstance(spec, dict):
            r = r.merge(validate_normalize_spec(
                cv, spec, path="normalize_specs[%d]" % i))

    objects = dict(plan.get("basis_objects") or {})
    for i, basis in enumerate(plan.get("verifiable_bases") or []):
        if isinstance(basis, dict):
            r = r.merge(validate_verifiable_basis(
                cv, basis, objects, path="verifiable_bases[%d]" % i))

    # Declared P1 accuracy / xref / budget / DAG — absence not invented
    from .accuracy import enforce_declared_accuracy
    from .ops_plan import is_ops_contract, validate_ops_plan

    if not is_ops_contract(cv):
        r.fail(
            "ops_version_required",
            "gate accepts v1.1.x ops[] only; got %s" % cv.version,
            "plan.contract_version",
            state_id="version_rejected",
        )
        return r, cv

    r = r.merge(validate_ops_plan(plan, cv))
    r = r.merge(enforce_declared_accuracy(plan, cv))

    return r, cv


def reject_payload(notes: List[str], result: ValidationResult,
                   plan: Optional[dict] = None) -> dict:
    """Empty table payload when gate rejects — shape matches execute_mechanical failures."""
    for issue in result.issues:
        notes.append(
            "[contract:%s] %s (%s)" % (issue.code, issue.message, issue.path)
        )
    states = [i.state_id for i in result.issues if i.state_id]
    status = states[0] if states else "contract_incomplete"
    return {
        "name": "合成表",
        "columns": [],
        "rows": [],
        "plan_notes": notes,
        "synth_plan": plan,
        "contract_rejected": True,
        "contract_status": status,
        "contract_issues": [
            {"code": i.code, "message": i.message, "path": i.path, "state_id": i.state_id}
            for i in result.issues
        ],
        "traces": [],
        "trace_ok": False,
        "_mechanical": False,
    }


def attach_contract_meta(plan: dict, cv: ContractVersion) -> None:
    """Record which contract file gated this plan (audit). Does not invent plan fields."""
    plan["_gated_contract_version"] = cv.version
    plan["_gated_contract_hash"] = cv.file_hash
