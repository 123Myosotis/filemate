# -*- coding: utf-8 -*-
"""
Budget meter: elapsed_ms / call_count only; meter_version; inherit/cover checks.
No silent continue past over_budget.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set

from .registry import ContractVersion
from .scope import Scope, parse_scope, scope_covers
from .validate import ValidationResult


def validate_meter(cv: ContractVersion, meter: dict) -> ValidationResult:
    r = ValidationResult(ok=True)
    if not isinstance(meter, dict):
        return r.fail("meter_bad", "not object", "meter")
    ver = str(meter.get("meter_version") or "")
    expected = str(cv.data.get("meter_version") or "")
    if not ver:
        return r.fail("meter_version_missing", "meter_version missing", "meter.meter_version")
    if expected and ver != expected:
        return r.fail(
            "meter_mismatch",
            "meter_version %s != contract %s" % (ver, expected),
            "meter.meter_version",
            state_id="version_rejected",
        )
    includes = meter.get("meter_includes")
    if not isinstance(includes, list) or not includes:
        return r.fail("meter_includes_missing", "meter_includes empty", "meter.meter_includes")
    for i, subj in enumerate(includes):
        if not cv.accepts_enum("meter_subject", str(subj)):
            r.fail("meter_subject_unknown", "subject not registered: %s" % subj,
                   "meter.meter_includes[%d]" % i)
    return r


def validate_budget_object(cv: ContractVersion, budget: dict,
                           known_budget_ids: Set[str],
                           scopes_by_id: Dict[str, Scope]) -> ValidationResult:
    r = ValidationResult(ok=True)
    if not isinstance(budget, dict):
        return r.fail("budget_bad", "not object", "budget")
    bid = str(budget.get("budget_id") or "")
    if not bid:
        r.fail("budget_id_missing", "budget_id missing", "budget.budget_id")
    sc = parse_scope(budget.get("budget_scope") or budget.get("scope"), require_upto="region")
    if sc is None:
        # allow sheet-level budgets
        sc = parse_scope(budget.get("budget_scope") or budget.get("scope"), require_upto="sheet")
    if sc is None:
        r.fail("budget_scope", "budget_scope incomplete", "budget.budget_scope")
    else:
        scopes_by_id[bid] = sc
    for subj in ("elapsed_ms", "call_count"):
        if subj in (budget.get("limits") or {}):
            try:
                float((budget.get("limits") or {})[subj])
            except (TypeError, ValueError):
                r.fail("budget_limit_bad", "limit not numeric", "budget.limits.%s" % subj)
    parent = str(budget.get("inherits_from") or "")
    if parent:
        if parent not in known_budget_ids and parent not in scopes_by_id:
            r.fail("budget_inherit_unknown", "inherits_from unknown", "budget.inherits_from")
        else:
            parent_scope = scopes_by_id.get(parent)
            if parent_scope is not None and sc is not None:
                if not scope_covers(parent_scope, sc):
                    r.fail(
                        "budget_inherit_cover",
                        "parent scope does not cover child",
                        "budget.inherits_from",
                    )
    strat = str(budget.get("on_over_budget_strategy") or budget.get("on_over_budget") or "")
    if strat and not cv.accepts_enum("on_over_budget_strategy", strat):
        r.fail("budget_strategy_unknown", "strategy not registered", "budget.on_over_budget_strategy")
    reset = str(budget.get("reset_rule") or "")
    if reset and not cv.accepts_enum("reset_rule", reset):
        r.fail("reset_rule_unknown", "reset_rule not registered", "budget.reset_rule")
    return r


def check_over_budget(budget: dict, actual: dict) -> ValidationResult:
    """actual > limit on any declared subject → over_budget."""
    r = ValidationResult(ok=True)
    limits = budget.get("limits") or {}
    for subj, lim in limits.items():
        try:
            lim_f = float(lim)
            act_f = float((actual or {}).get(subj, 0) or 0)
        except (TypeError, ValueError):
            continue
        if act_f > lim_f:
            return r.fail(
                "over_budget",
                "%s actual %s > limit %s" % (subj, act_f, lim_f),
                "budget.%s" % subj,
                state_id="over_budget",
            )
    return r


def validate_defer_rule(budgets: Sequence[dict]) -> ValidationResult:
    """defer_allowed=false → at most one budget object per budget_scope key."""
    r = ValidationResult(ok=True)
    scope_counts: Dict[str, List[str]] = {}
    for b in budgets or []:
        if not isinstance(b, dict):
            continue
        if b.get("defer_allowed") is False:
            key = str(b.get("budget_scope") or b.get("scope") or "")
            scope_counts.setdefault(key, []).append(str(b.get("budget_id") or ""))
    for key, ids in scope_counts.items():
        # if multiple budgets claim same scope and any has defer_allowed false
        same = [b for b in budgets if isinstance(b, dict)
                and str(b.get("budget_scope") or b.get("scope") or "") == key]
        if len(same) > 1:
            r.fail(
                "budget_defer_conflict",
                "defer_allowed=false but multiple budgets on scope",
                "budgets",
            )
    return r


def enforce_budgets(plan: dict, cv: ContractVersion,
                    actuals: Dict[str, dict] = None) -> ValidationResult:
    r = ValidationResult(ok=True)
    meter = plan.get("meter")
    if meter is not None:
        r = r.merge(validate_meter(cv, meter if isinstance(meter, dict) else {}))
    budgets = [b for b in (plan.get("budgets") or []) if isinstance(b, dict)]
    if not budgets:
        return r
    known = {str(b.get("budget_id")) for b in budgets if b.get("budget_id")}
    scopes: Dict[str, Scope] = {}
    # first pass collect scopes
    for b in budgets:
        bid = str(b.get("budget_id") or "")
        sc = parse_scope(b.get("budget_scope") or b.get("scope"), "region")
        if sc is None:
            sc = parse_scope(b.get("budget_scope") or b.get("scope"), "sheet")
        if bid and sc is not None:
            scopes[bid] = sc
    for i, b in enumerate(budgets):
        r = r.merge(validate_budget_object(cv, b, known, scopes))
    r = r.merge(validate_defer_rule(budgets))
    actuals = actuals or dict(plan.get("budget_actuals") or {})
    for b in budgets:
        bid = str(b.get("budget_id") or "")
        act = actuals.get(bid) or b.get("actual") or {}
        if act:
            r = r.merge(check_over_budget(b, act if isinstance(act, dict) else {}))
    return r
