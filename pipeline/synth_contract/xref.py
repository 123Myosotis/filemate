# -*- coding: utf-8 -*-
"""
P1-3/P1-5: entity_reg, alias_map, xref, batch_id, caliber / caliber_map.
Mechanical ID and set checks only — no semantic repair.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set

from .validate import ValidationResult


def validate_entity_reg(entries: Sequence[dict],
                        known_norm_ids: Set[str] = None) -> ValidationResult:
    """
    Same entity_id → all members share one norm_value.
    One norm_value → at most one entity_id unless alias_map covers it (checked separately).
    """
    r = ValidationResult(ok=True)
    known_norm_ids = known_norm_ids or set()
    by_id: Dict[str, Set[str]] = {}
    value_owners: Dict[str, Set[str]] = {}
    for i, e in enumerate(entries or []):
        if not isinstance(e, dict):
            r.fail("entity_reg_bad", "not object", "entity_reg[%d]" % i)
            continue
        eid = str(e.get("entity_id") or "")
        if not eid:
            r.fail("entity_id_missing", "entity_id missing", "entity_reg[%d]" % i)
            continue
        if not e.get("scope"):
            r.fail("entity_scope", "scope missing", "entity_reg[%d].scope" % i)
        kid = str(e.get("key_norm_id") or "")
        if known_norm_ids and kid and kid not in known_norm_ids:
            r.fail("entity_key_norm", "key_norm_id unknown", "entity_reg[%d].key_norm_id" % i)
        members = e.get("member_coords") or []
        if not isinstance(members, list) or not members:
            r.fail("entity_members", "member_coords empty", "entity_reg[%d].member_coords" % i)
        nv = str(e.get("norm_value") if e.get("norm_value") is not None else "")
        if "norm_value" not in e:
            r.fail("entity_norm_value", "norm_value missing", "entity_reg[%d].norm_value" % i)
        by_id.setdefault(eid, set()).add(nv)
        value_owners.setdefault(nv, set()).add(eid)
    for eid, vals in by_id.items():
        if len(vals) > 1:
            r.fail(
                "entity_norm_conflict",
                "entity_id %s has multiple norm_values" % eid,
                "entity_reg",
                state_id="fail",
            )
    # multi-owner values recorded for alias check
    r._value_owners = value_owners  # type: ignore[attr-defined]
    return r


def validate_alias_map(aliases: Sequence[dict], entity_ids: Set[str],
                       multi_owners: Dict[str, Set[str]] = None) -> ValidationResult:
    r = ValidationResult(ok=True)
    covered_pairs: Set[tuple] = set()
    for i, a in enumerate(aliases or []):
        if not isinstance(a, dict):
            r.fail("alias_bad", "not object", "alias_map[%d]" % i)
            continue
        frm = str(a.get("from_entity_id") or "")
        to = str(a.get("to_entity_id") or "")
        if not frm or not to:
            r.fail("alias_ends", "from/to missing", "alias_map[%d]" % i)
            continue
        if frm not in entity_ids or to not in entity_ids:
            r.fail("alias_unknown_entity", "entity_id not in entity_reg", "alias_map[%d]" % i)
        if not a.get("evidence_coords"):
            r.fail("alias_evidence", "evidence_coords missing", "alias_map[%d]" % i)
        covered_pairs.add(tuple(sorted((frm, to))))
    if multi_owners:
        for nv, owners in multi_owners.items():
            if len(owners) <= 1:
                continue
            # every pair among owners must be covered by alias_map
            owners_l = sorted(owners)
            for i in range(len(owners_l)):
                for j in range(i + 1, len(owners_l)):
                    pair = tuple(sorted((owners_l[i], owners_l[j])))
                    if pair not in covered_pairs:
                        r.fail(
                            "alias_required",
                            "norm_value shared by %s without alias_map" % (owners_l,),
                            "alias_map",
                            state_id="await_user",
                        )
                        return r
    return r


def validate_xref(xrefs: Sequence[dict], entity_ids_by_txn: Dict[str, Set[str]],
                  catalog_ids_by_txn: Dict[str, Set[str]] = None) -> ValidationResult:
    r = ValidationResult(ok=True)
    catalog_ids_by_txn = catalog_ids_by_txn or {}
    for i, x in enumerate(xrefs or []):
        if not isinstance(x, dict):
            r.fail("xref_bad", "not object", "xref[%d]" % i)
            continue
        for k in ("from_txn", "to_txn", "from_catalog_id", "to_catalog_id", "entity_id"):
            if not x.get(k):
                r.fail("xref_field", "missing %s" % k, "xref[%d].%s" % (i, k))
        ft, tt = str(x.get("from_txn") or ""), str(x.get("to_txn") or "")
        eid = str(x.get("entity_id") or "")
        from_ents = entity_ids_by_txn.get(ft) or set()
        to_ents = entity_ids_by_txn.get(tt) or set()
        if eid and eid not in from_ents:
            r.fail("xref_entity_from", "entity_id not in from_txn reg", "xref[%d]" % i,
                   state_id="await_user")
        if eid and eid not in to_ents:
            r.fail("xref_entity_to", "entity_id not in to_txn reg", "xref[%d]" % i,
                   state_id="await_user")
        fc = catalog_ids_by_txn.get(ft)
        tc = catalog_ids_by_txn.get(tt)
        if fc is not None and str(x.get("from_catalog_id") or "") not in fc:
            r.fail("xref_catalog_from", "from_catalog_id unknown", "xref[%d]" % i)
        if tc is not None and str(x.get("to_catalog_id") or "") not in tc:
            r.fail("xref_catalog_to", "to_catalog_id unknown", "xref[%d]" % i)
    return r


def validate_batch_id(plan: dict) -> ValidationResult:
    r = ValidationResult(ok=True)
    bid = plan.get("batch_id")
    if bid is None or str(bid).strip() == "":
        # absence is ok unless plan declares cross-txn objects
        needs = bool(plan.get("xref") or plan.get("entity_reg") or plan.get("caliber_map"))
        if needs:
            return r.fail("batch_id_missing", "cross-txn objects require batch_id", "batch_id")
        return r
    return r


def validate_caliber(calibers: Sequence[dict], calc_ids: Set[str],
                     norm_ids: Set[str]) -> ValidationResult:
    r = ValidationResult(ok=True)
    seen: Set[str] = set()
    for i, c in enumerate(calibers or []):
        if not isinstance(c, dict):
            r.fail("caliber_bad", "not object", "caliber[%d]" % i)
            continue
        cid = str(c.get("caliber_id") or "")
        if not cid:
            r.fail("caliber_id_missing", "caliber_id missing", "caliber[%d]" % i)
            continue
        if cid in seen:
            r.fail("caliber_dup", "duplicate caliber_id", "caliber[%d]" % i)
        seen.add(cid)
        calc = str(c.get("calc_id") or "")
        if calc and calc not in calc_ids:
            r.fail("caliber_calc", "calc_id unknown", "caliber[%d].calc_id" % i)
        nid = str(c.get("norm_id") or "")
        if nid and nid not in norm_ids:
            r.fail("caliber_norm", "norm_id unknown", "caliber[%d].norm_id" % i)
        if not c.get("text_id"):
            r.fail("caliber_text", "text_id missing", "caliber[%d].text_id" % i)
    return r


def validate_caliber_usage(plan: dict, caliber_ids: Set[str]) -> ValidationResult:
    """Same catalog_id → one caliber_id within txn; all referenced ids registered."""
    r = ValidationResult(ok=True)
    by_catalog: Dict[str, Set[str]] = {}
    for i, row in enumerate(plan.get("metric_calibers") or []):
        if not isinstance(row, dict):
            continue
        cat = str(row.get("catalog_id") or "")
        cal = str(row.get("caliber_id") or "")
        if not cat or not cal:
            r.fail("metric_caliber_field", "catalog_id/caliber_id missing",
                   "metric_calibers[%d]" % i)
            continue
        if cal not in caliber_ids:
            r.fail("caliber_unref", "caliber_id not registered", "metric_calibers[%d]" % i)
        by_catalog.setdefault(cat, set()).add(cal)
    for cat, cals in by_catalog.items():
        if len(cals) > 1:
            r.fail(
                "caliber_conflict",
                "catalog_id %s maps to multiple caliber_ids" % cat,
                "metric_calibers",
                state_id="contract_incomplete",
            )
    for i, m in enumerate(plan.get("caliber_map") or []):
        if not isinstance(m, dict):
            continue
        frm = str(m.get("from_id") or "")
        to = str(m.get("to_id") or "")
        if frm not in caliber_ids or to not in caliber_ids:
            r.fail("caliber_map_unknown", "from_id/to_id not registered", "caliber_map[%d]" % i)
        # steps equality after transform: both sides must declare matching steps lists
        left = m.get("from_steps")
        right = m.get("to_steps")
        if left is not None or right is not None:
            def norm(steps):
                if not isinstance(steps, list):
                    return None
                return [(str(s.get("op")), s.get("param")) for s in steps if isinstance(s, dict)]
            if norm(left) != norm(right):
                r.fail(
                    "caliber_map_steps",
                    "mapped calc steps differ",
                    "caliber_map[%d]" % i,
                    state_id="contract_incomplete",
                )
    # delivery doc refs
    for i, doc in enumerate(plan.get("delivery_blocks") or []):
        if not isinstance(doc, dict):
            continue
        for ref in doc.get("caliber_ids") or []:
            if str(ref) not in caliber_ids:
                r.fail("delivery_caliber_unref", "caliber_id not registered: %s" % ref,
                       "delivery_blocks[%d]" % i)
    return r


def enforce_xref_and_caliber(plan: dict) -> ValidationResult:
    r = ValidationResult(ok=True)
    r = r.merge(validate_batch_id(plan))
    ents = [e for e in (plan.get("entity_reg") or []) if isinstance(e, dict)]
    norm_ids = set()
    for s in list(plan.get("norm_specs") or []) + list(plan.get("normalize_specs") or []):
        if isinstance(s, dict):
            if s.get("norm_id"):
                norm_ids.add(str(s["norm_id"]))
            if s.get("spec_id"):
                norm_ids.add(str(s["spec_id"]))
    er = validate_entity_reg(ents, norm_ids)
    multi = getattr(er, "_value_owners", {}) or {}
    r = r.merge(er)
    entity_ids = {str(e.get("entity_id")) for e in ents if e.get("entity_id")}
    r = r.merge(validate_alias_map(plan.get("alias_map") or [], entity_ids, multi))
    # group entity regs by txn if declared
    by_txn: Dict[str, Set[str]] = {}
    for e in ents:
        txn = str((e.get("scope") or {}).get("txn") or (e.get("scope") or {}).get("file_id")
                  or plan.get("batch_id") or "default")
        by_txn.setdefault(txn, set()).add(str(e.get("entity_id") or ""))
    cat_by_txn: Dict[str, Set[str]] = {}
    for entry in plan.get("catalog") or []:
        if not isinstance(entry, dict):
            continue
        txn = str((entry.get("scope") or {}).get("txn")
                  or (entry.get("scope") or {}).get("file_id")
                  or plan.get("batch_id") or "default")
        if entry.get("catalog_id"):
            cat_by_txn.setdefault(txn, set()).add(str(entry["catalog_id"]))
    if plan.get("xref") is not None:
        r = r.merge(validate_xref(plan.get("xref") or [], by_txn, cat_by_txn))
    calc_ids = {str(s.get("calc_id")) for s in (plan.get("calc_specs") or [])
                if isinstance(s, dict) and s.get("calc_id")}
    calibers = [c for c in (plan.get("calibers") or []) if isinstance(c, dict)]
    cal_ids = {str(c.get("caliber_id")) for c in calibers if c.get("caliber_id")}
    if calibers or plan.get("metric_calibers") or plan.get("caliber_map"):
        r = r.merge(validate_caliber(calibers, calc_ids, norm_ids))
        r = r.merge(validate_caliber_usage(plan, cal_ids))
    return r
