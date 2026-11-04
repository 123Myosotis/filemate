# -*- coding: utf-8 -*-
"""
Mechanical validators only: membership, equality, set ops, numeric compare.
No semantic repair, no fuzzy match, no default invention.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from .registry import ContractVersion
from .scope import Scope, parse_scope


@dataclass
class ValidationIssue:
    code: str
    message: str
    path: str = ""
    state_id: str = "contract_incomplete"


@dataclass
class ValidationResult:
    ok: bool
    issues: List[ValidationIssue] = field(default_factory=list)

    def merge(self, other: "ValidationResult") -> "ValidationResult":
        return ValidationResult(
            ok=self.ok and other.ok,
            issues=list(self.issues) + list(other.issues),
        )

    def fail(self, code: str, message: str, path: str = "",
             state_id: str = "contract_incomplete") -> "ValidationResult":
        self.ok = False
        self.issues.append(
            ValidationIssue(code=code, message=message, path=path, state_id=state_id)
        )
        return self


def validate_enum(cv: ContractVersion, enum_name: str, value: Any,
                  path: str = "") -> ValidationResult:
    r = ValidationResult(ok=True)
    if value is None or str(value) == "":
        return r.fail("enum_missing", "enum %s missing" % enum_name, path)
    if not cv.accepts_enum(enum_name, str(value)):
        return r.fail(
            "enum_unknown",
            "value %r not in contract enum %s@%s" % (value, enum_name, cv.version),
            path,
        )
    return r


def _require_keys(obj: dict, keys: Sequence[str], path: str) -> ValidationResult:
    r = ValidationResult(ok=True)
    if not isinstance(obj, dict):
        return r.fail("not_object", "expected object", path)
    for k in keys:
        if k not in obj:
            r.fail("field_missing", "missing field %s" % k, "%s.%s" % (path, k))
        else:
            v = obj[k]
            if v is None or v == "" or v == [] or v == {}:
                r.fail("field_empty", "empty field %s" % k, "%s.%s" % (path, k))
    return r


def validate_scope_field(obj: Any, path: str, require_upto: str = "sheet"
                         ) -> ValidationResult:
    r = ValidationResult(ok=True)
    scope = parse_scope(obj, require_upto=require_upto)
    if scope is None:
        return r.fail(
            "scope_incomplete",
            "scope incomplete up to %s" % require_upto,
            path,
            state_id="contract_incomplete",
        )
    return r


def validate_verifiable_basis(
    cv: ContractVersion,
    basis: Any,
    objects_by_id: Dict[str, dict],
    path: str = "basis",
) -> ValidationResult:
    """source_type/source_ref/value/compare_field/compare_op — mechanical only."""
    r = ValidationResult(ok=True)
    r = r.merge(_require_keys(
        basis if isinstance(basis, dict) else {},
        ["source_type", "source_ref", "value", "compare_field", "compare_op"],
        path,
    ))
    if not r.ok:
        return r
    assert isinstance(basis, dict)
    r = r.merge(validate_enum(cv, "source_type", basis["source_type"], path + ".source_type"))
    r = r.merge(validate_enum(cv, "compare_op", basis["compare_op"], path + ".compare_op"))
    ref = str(basis["source_ref"])
    obj = objects_by_id.get(ref)
    if obj is None:
        return r.fail("source_ref_missing", "source_ref not found: %s" % ref, path)
    expected_type = cv.source_object_type(str(basis["source_type"]))
    actual_type = str(obj.get("object_type") or "")
    if expected_type and actual_type != expected_type:
        return r.fail(
            "source_type_mismatch",
            "object_type %r != expected %r for source_type %r"
            % (actual_type, expected_type, basis["source_type"]),
            path,
        )
    cf = str(basis["compare_field"])
    if cf not in obj:
        return r.fail("compare_field_missing", "field %s absent on object" % cf, path)
    left = basis["value"]
    right = obj[cf]
    op = str(basis["compare_op"])
    if not _mech_compare(left, right, op):
        return r.fail(
            "compare_fail",
            "mechanical compare failed: %r %s %r" % (left, op, right),
            path,
            state_id="fail",
        )
    return r


def _mech_compare(left: Any, right: Any, op: str) -> bool:
    if op == "eq":
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return float(left) == float(right)
        if isinstance(left, str) and isinstance(right, str):
            return left == right
        return type(left) == type(right) and left == right
    if op in ("le", "ge"):
        try:
            lf, rf = float(left), float(right)
        except (TypeError, ValueError):
            return False
        return lf <= rf if op == "le" else lf >= rf
    return False


def validate_binding(
    cv: ContractVersion,
    binding: dict,
    catalog_ids: Set[str],
    known_scopes: Dict[str, Scope],
    path: str = "binding",
) -> ValidationResult:
    r = ValidationResult(ok=True)
    required = [
        "scope", "source_file", "source_sheet", "orig_col_name",
        "cell_coord_or_range", "target_field_id", "target_field_name", "confidence",
    ]
    r = r.merge(_require_keys(binding, required, path))
    if not r.ok:
        return r
    r = r.merge(validate_scope_field(binding.get("scope"), path + ".scope", "region"))
    scope = parse_scope(binding.get("scope"), require_upto="region")
    tid = str(binding.get("target_field_id") or "")
    if tid not in catalog_ids:
        r.fail("catalog_id_missing", "target_field_id not in catalog: %s" % tid, path)
    # ID must belong to declared scope: catalog key format or known_scopes map
    if tid in known_scopes and scope is not None:
        cat_scope = known_scopes[tid]
        if not scopes_equal_sheet_region(cat_scope, scope):
            r.fail(
                "catalog_scope_mismatch",
                "catalog_id %s not in binding scope" % tid,
                path,
            )
    try:
        conf = float(binding["confidence"])
    except (TypeError, ValueError):
        return r.fail("confidence_bad", "confidence not numeric", path + ".confidence")
    if cv.forbid_degenerate_confidence() and conf in cv.degenerate_confidence_values():
        r.fail(
            "confidence_degenerate",
            "confidence %s forbidden as degenerate" % conf,
            path + ".confidence",
        )
    # Direction 3: confidence stays for display and audit. No threshold comparison.
    return r


def scopes_equal_sheet_region(a: Scope, b: Scope) -> bool:
    return (
        a.file_id == b.file_id
        and a.sheet == b.sheet
        and a.region == b.region
    )


def validate_join_spec(cv: ContractVersion, join: dict, catalog_ids: Set[str],
                       path: str = "join") -> ValidationResult:
    r = ValidationResult(ok=True)
    required = [
        "scope", "left_scope", "right_scope", "left_catalog_id", "right_catalog_id",
        "join_type", "expected_cardinality", "grain",
    ]
    r = r.merge(_require_keys(join, required, path))
    if not r.ok:
        return r
    for key, lvl in (
        ("scope", "region"),
        ("left_scope", "region"),
        ("right_scope", "region"),
    ):
        r = r.merge(validate_scope_field(join.get(key), "%s.%s" % (path, key), lvl))
    r = r.merge(validate_enum(cv, "join_type", join.get("join_type"), path + ".join_type"))
    for side in ("left_catalog_id", "right_catalog_id"):
        cid = str(join.get(side) or "")
        if cid not in catalog_ids:
            r.fail("catalog_id_missing", "%s not in catalog" % side, "%s.%s" % (path, side))
    # grain must be a catalog id in scope — existence only
    grain = str(join.get("grain") or "")
    if grain and grain not in catalog_ids:
        r.fail("grain_missing", "grain not in catalog: %s" % grain, path + ".grain")
    return r


def validate_region_cover(
    regions: Sequence[dict],
    sheet_data_coords: Set[str],
    path: str = "regions",
) -> ValidationResult:
    """
    Set checks only: union == sheet data area, pairwise disjoint.
    Continuity: each region declares boundary_continuous bool that must be True.
    Local does NOT re-infer regions.
    """
    r = ValidationResult(ok=True)
    if not regions:
        return r.fail("regions_empty", "region set empty", path)
    covered: Set[str] = set()
    seen_ids: Set[str] = set()
    for i, reg in enumerate(regions):
        p = "%s[%d]" % (path, i)
        if not isinstance(reg, dict):
            r.fail("region_not_object", "region not object", p)
            continue
        rid = str(reg.get("region_id") or "")
        if not rid:
            r.fail("region_id_missing", "region_id missing", p)
        elif rid in seen_ids:
            r.fail("region_id_dup", "duplicate region_id %s" % rid, p)
        else:
            seen_ids.add(rid)
        coords = reg.get("coords")
        if not isinstance(coords, list) or not coords:
            r.fail("region_coords_missing", "coords missing", p)
            continue
        cset = {str(c) for c in coords}
        if covered & cset:
            r.fail(
                "region_overlap",
                "regions overlap at %s" % sorted(list(covered & cset))[:5],
                p,
            )
        covered |= cset
        if reg.get("boundary_continuous") is not True:
            r.fail(
                "region_not_continuous",
                "boundary_continuous must be true (AI-declared)",
                p,
            )
        r = r.merge(validate_scope_field(reg.get("scope"), p + ".scope", "region"))
    if sheet_data_coords and covered != set(str(c) for c in sheet_data_coords):
        missing = set(str(c) for c in sheet_data_coords) - covered
        extra = covered - set(str(c) for c in sheet_data_coords)
        r.fail(
            "region_cover_mismatch",
            "union!=sheet_data missing=%d extra=%d" % (len(missing), len(extra)),
            path,
        )
    return r


def validate_dsl_continuity(steps: Sequence[dict], path: str = "dsl") -> ValidationResult:
    """Next in_scope must be covered by previous out_scope (same file/sheet/region)."""
    r = ValidationResult(ok=True)
    if not steps:
        return r.fail("dsl_empty", "dsl steps empty", path)
    prev_out: Optional[Scope] = None
    for i, step in enumerate(steps):
        p = "%s[%d]" % (path, i)
        if not isinstance(step, dict):
            r.fail("dsl_step_bad", "step not object", p)
            continue
        for key in ("step_id", "op", "in_scope", "out_scope"):
            if key not in step or step[key] in (None, "", {}, []):
                r.fail("dsl_field_missing", "missing %s" % key, "%s.%s" % (p, key))
        in_s = parse_scope(step.get("in_scope"), require_upto="region")
        out_s = parse_scope(step.get("out_scope"), require_upto="region")
        if in_s is None or out_s is None:
            r.fail("dsl_scope_incomplete", "in/out scope incomplete", p)
            continue
        if prev_out is not None:
            if not (
                prev_out.file_id == in_s.file_id
                and prev_out.sheet == in_s.sheet
                and prev_out.region == in_s.region
            ):
                r.fail(
                    "dsl_scope_break",
                    "step %s in_scope not covered by previous out_scope" % step.get("step_id"),
                    p,
                )
        prev_out = out_s
    return r


def validate_global_judgment(
    cv: ContractVersion,
    judgment: dict,
    known_coords: Set[str],
    summary_hashes: Dict[str, str],
    sample_manifests: Dict[str, dict],
    path: str = "judgment",
) -> ValidationResult:
    r = ValidationResult(ok=True)
    required = [
        "judgment_id", "global_judgment_type", "based_on", "confidence",
        "on_low_confidence", "declared_scope", "anchor",
    ]
    r = r.merge(_require_keys(judgment, required, path))
    if not r.ok:
        return r
    r = r.merge(validate_enum(
        cv, "global_judgment_type", judgment.get("global_judgment_type"),
        path + ".global_judgment_type"))
    r = r.merge(validate_enum(cv, "based_on", judgment.get("based_on"), path + ".based_on"))
    r = r.merge(validate_enum(
        cv, "plan_status", judgment.get("on_low_confidence"),
        path + ".on_low_confidence"))
    r = r.merge(validate_scope_field(
        judgment.get("declared_scope"), path + ".declared_scope", "region"))
    based = str(judgment.get("based_on"))
    anchor = judgment.get("anchor") if isinstance(judgment.get("anchor"), dict) else {}
    # based_on ↔ anchor field correspondence
    if based == "full":
        coords = anchor.get("evidence_coords")
        if not isinstance(coords, list) or not coords:
            r.fail("anchor_missing", "full requires evidence_coords", path + ".anchor",
                   state_id="unanchored")
        else:
            cset = {str(c) for c in coords}
            if not cset.issubset(known_coords):
                r.fail(
                    "anchor_coord_unknown",
                    "evidence_coords not in snapshot",
                    path + ".anchor",
                    state_id="unanchored",
                )
            declared = parse_scope(judgment.get("declared_scope"), "region")
            # mechanical: coords must be subset of declared region coords if provided
            region_coords = set(str(c) for c in (anchor.get("declared_scope_coords") or []))
            if region_coords and cset != region_coords:
                r.fail(
                    "anchor_scope_mismatch",
                    "evidence_coords != declared_scope_coords",
                    path + ".anchor",
                    state_id="unanchored",
                )
            _ = declared
    elif based == "summary":
        sh = str(anchor.get("summary_hash") or "")
        scope_key = str(anchor.get("scope_key") or "")
        if not sh or not scope_key:
            r.fail("anchor_missing", "summary requires summary_hash+scope_key",
                   path + ".anchor", state_id="unanchored")
        elif summary_hashes.get(scope_key) != sh:
            r.fail("anchor_hash_mismatch", "summary_hash mismatch",
                   path + ".anchor", state_id="unanchored")
    elif based == "sample":
        mid = str(anchor.get("sample_manifest_id") or "")
        man = sample_manifests.get(mid)
        if not mid or man is None:
            r.fail("anchor_missing", "sample requires sample_manifest_id",
                   path + ".anchor", state_id="unanchored")
        else:
            mcoords = {str(c) for c in (man.get("coords") or [])}
            if not mcoords.issubset(known_coords):
                r.fail("anchor_coord_unknown", "manifest coords not in snapshot",
                       path + ".anchor", state_id="unanchored")
            declared_n = man.get("declared_count")
            if declared_n is not None and int(declared_n) != len(mcoords):
                r.fail("anchor_count_mismatch", "manifest count != len(coords)",
                       path + ".anchor", state_id="unanchored")
    try:
        conf = float(judgment["confidence"])
    except (TypeError, ValueError):
        return r.fail("confidence_bad", "confidence not numeric", path + ".confidence")
    if cv.forbid_degenerate_confidence() and conf in cv.degenerate_confidence_values():
        r.fail("confidence_degenerate", "degenerate confidence", path + ".confidence")
    # Direction 3: same as validate_binding. on_low_confidence is not applied here.
    return r


def validate_row_cover(
    records: Sequence[dict],
    source_row_ids: Set[str],
    cv: ContractVersion,
    path: str = "row_cover",
) -> ValidationResult:
    """P0 A6: every source row appears exactly once with a valid disposition."""
    r = ValidationResult(ok=True)
    seen: Set[str] = set()
    for i, rec in enumerate(records):
        p = "%s[%d]" % (path, i)
        if not isinstance(rec, dict):
            r.fail("row_cover_bad", "record not object", p)
            continue
        rid = str(rec.get("source_row_id") or "")
        disp = str(rec.get("disposition") or "")
        if not rid:
            r.fail("row_id_missing", "source_row_id missing", p)
            continue
        if rid in seen:
            r.fail("row_id_dup", "duplicate source_row_id %s" % rid, p)
        seen.add(rid)
        r = r.merge(validate_enum(cv, "row_disposition", disp, p + ".disposition"))
        r = r.merge(validate_scope_field(rec.get("scope"), p + ".scope", "region"))
    missing = source_row_ids - seen
    extra = seen - source_row_ids
    if missing:
        r.fail("row_cover_missing", "uncovered source rows: %d" % len(missing), path,
               state_id="contract_incomplete")
    if extra:
        r.fail("row_cover_extra", "unknown source rows: %d" % len(extra), path)
    return r


def validate_plan_envelope(
    cv: ContractVersion,
    plan: dict,
    expected_hash: Optional[str] = None,
) -> ValidationResult:
    """Top-level plan must declare contract_version (+ optional hash) and status."""
    r = ValidationResult(ok=True)
    if not isinstance(plan, dict):
        return r.fail("plan_not_object", "plan not object", "plan")
    ver = str(plan.get("contract_version") or "")
    if not ver:
        return r.fail("contract_version_missing", "contract_version missing", "plan")
    if ver != cv.version:
        r.fail(
            "contract_version_mismatch",
            "plan %s != loaded %s" % (ver, cv.version),
            "plan.contract_version",
            state_id="version_rejected",
        )
    ch = plan.get("contract_hash")
    if ch is not None:
        if str(ch) != cv.file_hash:
            r.fail(
                "contract_hash_mismatch",
                "plan contract_hash != file hash",
                "plan.contract_hash",
                state_id="version_rejected",
            )
    if expected_hash and cv.file_hash != expected_hash:
        r.fail(
            "contract_hash_mismatch",
            "expected_hash mismatch",
            "plan",
            state_id="version_rejected",
        )
    status = plan.get("status")
    if status is not None:
        r = r.merge(validate_enum(cv, "plan_status", status, "plan.status"))
    # required judgment types: AI-declared list, values must be in enum
    req = plan.get("required_judgment_types")
    if req is not None:
        if not isinstance(req, list) or not req:
            r.fail("required_judgments_bad", "required_judgment_types must be non-empty list",
                   "plan.required_judgment_types")
        else:
            for i, t in enumerate(req):
                r = r.merge(validate_enum(
                    cv, "global_judgment_type", t,
                    "plan.required_judgment_types[%d]" % i))
    return r


def pick_state(cv: ContractVersion, state_ids: Iterable[str]) -> ValidationResult:
    """Apply exclusive_with / priority from contract state table. No invented merges."""
    r = ValidationResult(ok=True)
    ids = [str(s) for s in state_ids if s]
    if not ids:
        return r
    rows = []
    for sid in ids:
        row = cv.state_by_id(sid)
        if row is None:
            r.fail("state_unknown", "state_id not in contract: %s" % sid, "state")
        else:
            rows.append(row)
    if not r.ok or not rows:
        return r
    # exclusivity
    for i, a in enumerate(rows):
        excl = set(str(x) for x in (a.get("exclusive_with") or []))
        for b in rows[i + 1:]:
            bid = str(b.get("state_id"))
            aid = str(a.get("state_id"))
            bexcl = set(str(x) for x in (b.get("exclusive_with") or []))
            if bid in excl or aid in bexcl:
                # merge_into same?
                ma = a.get("merge_into")
                mb = b.get("merge_into")
                if ma and mb and str(ma) == str(mb):
                    continue
                return r.fail(
                    "state_exclusive",
                    "exclusive states %s and %s" % (aid, bid),
                    "state",
                )
    higher = bool(cv.data.get("priority_higher_wins", True))
    best = rows[0]
    for row in rows[1:]:
        pa = int(best.get("priority") or 0)
        pb = int(row.get("priority") or 0)
        if (pb > pa) if higher else (pb < pa):
            best = row
    r.issues.append(ValidationIssue(
        code="state_selected",
        message=str(best.get("state_id")),
        path="state",
        state_id=str(best.get("state_id")),
    ))
    return r
