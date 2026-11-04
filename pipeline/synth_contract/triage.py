# -*- coding: utf-8 -*-
"""
Mechanical triage. Returns only simple|complex plus fired signal ids.
Does not map signals to suggested actions and does not call a model.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

_STATUS_SIGNALS = (
    ("status_await_user", "await_user"),
    ("status_contract_incomplete", "contract_incomplete"),
    ("status_version_rejected", "version_rejected"),
    ("status_over_budget", "over_budget"),
    ("status_unanchored", "unanchored"),
    ("status_fail", "fail"),
    ("status_rollback", "rollback"),
    ("status_non_idempotent_rerun", "non_idempotent_rerun"),
    ("status_review_reread", "review_reread"),
    ("status_stop", "stop"),
)


def triage_plan(plan: dict) -> dict:
    """Execute-before scan. Keys are only triage and fired_signals."""
    fired = _fired(plan if isinstance(plan, dict) else {}, None, include_execution=False)
    return _pack(fired)


def triage_after_execute(plan: dict, execution_result: Optional[dict] = None) -> dict:
    """Re-scan after execute, including execution_result.contract_issues."""
    fired = _fired(
        plan if isinstance(plan, dict) else {},
        execution_result if isinstance(execution_result, dict) else {},
        include_execution=True,
    )
    return _pack(fired)


def signal_locations(plan: dict, execution_result: Optional[dict] = None,
                     include_execution: bool = False) -> List[dict]:
    """Mechanical source_path and impact_scope for each fired signal. No actions."""
    plan = plan if isinstance(plan, dict) else {}
    locs = []
    for signal_id, source_path, impact in _locate(plan, execution_result, include_execution):
        locs.append({
            "signal_id": signal_id,
            "source_path": source_path,
            "impact_scope": impact,
        })
    return locs


def _pack(fired: List[str]) -> dict:
    return {
        "triage": "complex" if fired else "simple",
        "fired_signals": list(fired),
    }


def _fired(plan: dict, execution_result: Optional[dict], include_execution: bool) -> List[str]:
    seen = []
    for signal_id, _path, _impact in _locate(plan, execution_result, include_execution):
        if signal_id not in seen:
            seen.append(signal_id)
    return seen


def _locate(plan: dict, execution_result: Optional[dict], include_execution: bool):
    out = []
    for i, b in enumerate(plan.get("bindings") or []):
        if not isinstance(b, dict):
            continue
        reason = b.get("ambiguous_reason")
        if isinstance(reason, str) and reason.strip():
            out.append((
                "binding_ambiguous_reason",
                "bindings[%d].ambiguous_reason" % i,
                "bindings[%d]" % i,
            ))
        alts = b.get("alternatives")
        if isinstance(alts, list) and len(alts) > 0:
            out.append((
                "binding_alternatives",
                "bindings[%d].alternatives" % i,
                "bindings[%d]" % i,
            ))
    status = str(plan.get("status") or "")
    for signal_id, token in _STATUS_SIGNALS:
        if status == token:
            out.append((signal_id, "plan.status", "plan"))
    for k, bud in enumerate(plan.get("budgets") or []):
        if not isinstance(bud, dict):
            continue
        limits = bud.get("limits") if isinstance(bud.get("limits"), dict) else {}
        actual = bud.get("actual") if isinstance(bud.get("actual"), dict) else {}
        for key in sorted(set(limits) & set(actual)):
            if _finite_number(limits.get(key)) and _finite_number(actual.get(key)):
                if float(actual[key]) > float(limits[key]):
                    out.append((
                        "budget_actual_over",
                        "budgets[%d].actual.%s" % (k, key),
                        "budgets[%d]" % k,
                    ))
    if _row_cover_incomplete(plan):
        out.append(("row_cover_incomplete", "plan.row_cover", "plan"))
    notes = plan.get("runtime_notes") or []
    if isinstance(notes, list):
        for i, line in enumerate(notes):
            if isinstance(line, str) and line.startswith("shared_map_mismatch"):
                out.append((
                    "shared_map_mismatch_note",
                    "runtime_notes[%d]" % i,
                    "plan",
                ))
                break
    if include_execution:
        issues = (execution_result or {}).get("contract_issues") or []
        if isinstance(issues, list) and len(issues) > 0:
            out.append((
                "execution_contract_issues",
                "execution_result.contract_issues",
                "plan",
            ))
    return out


def _finite_number(v: Any) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return math.isfinite(float(v))


def _row_cover_incomplete(plan: dict) -> bool:
    cover = plan.get("row_cover")
    if cover is None:
        return False
    src = {str(x) for x in (plan.get("source_row_ids") or [])}
    if not src:
        return False
    seen = set()
    if isinstance(cover, list):
        for rec in cover:
            if isinstance(rec, dict) and rec.get("source_row_id") is not None:
                seen.add(str(rec.get("source_row_id")))
    return bool(src - seen)


def budget_incomparable_keys(plan: dict) -> List[str]:
    """Keys present on both limits and actual that are not finite numbers."""
    bad = []
    for k, bud in enumerate(plan.get("budgets") or []):
        if not isinstance(bud, dict):
            continue
        limits = bud.get("limits") if isinstance(bud.get("limits"), dict) else None
        actual = bud.get("actual") if isinstance(bud.get("actual"), dict) else None
        if limits is None or actual is None:
            if bud.get("limits") is not None or bud.get("actual") is not None:
                bad.append("budgets[%d]" % k)
            continue
        for key in sorted(set(limits) & set(actual)):
            if not (_finite_number(limits.get(key)) and _finite_number(actual.get(key))):
                bad.append("budgets[%d].%s" % (k, key))
    return bad
