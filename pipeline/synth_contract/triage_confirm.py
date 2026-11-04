# -*- coding: utf-8 -*-
"""
Independent confirmation step. Not part of triage.py.
AI supplies suggested_action and user_prompt. Illegal output or failure → abort.
"""
from __future__ import annotations

from typing import Callable, Dict, Optional, Set

from .triage import signal_locations

ACTIONS_BEFORE = frozenset({"confirm_execute", "edit_plan", "abort"})
ACTIONS_LATE = frozenset({"accept_current_result", "abort"})


def build_complex_confirmation(
    plan: dict,
    triage_result: dict,
    ai_complete: Optional[Callable[[dict], dict]] = None,
    action_set: Optional[Set[str]] = None,
    lock_status: str = "pending",
    execution_result: Optional[dict] = None,
) -> dict:
    allowed = set(action_set or ACTIONS_BEFORE)
    fired = list((triage_result or {}).get("fired_signals") or [])
    locs = signal_locations(
        plan,
        execution_result,
        include_execution=execution_result is not None,
    )
    by_id = {}
    for loc in locs:
        by_id.setdefault(loc["signal_id"], loc)
    payload = {
        "fired_signals": fired,
        "locations": [
            {
                "signal_id": s,
                "source_path": (by_id.get(s) or {}).get("source_path") or "plan",
                "impact_scope": (by_id.get(s) or {}).get("impact_scope") or "plan",
            }
            for s in fired
        ],
        "plan": plan,
        "allowed_actions": sorted(allowed),
    }
    parsed = None
    if ai_complete is not None:
        try:
            parsed = ai_complete(payload)
        except Exception:
            parsed = None
    if not _legal(parsed, fired, allowed):
        return _abort(plan, fired, by_id, lock_status)
    conflicts = []
    for s in fired:
        loc = by_id.get(s) or {}
        action = _action_for(parsed, s)
        conflicts.append({
            "signal_id": s,
            "source_path": loc.get("source_path") or "plan",
            "impact_scope": loc.get("impact_scope") or "plan",
            "suggested_action": action,
        })
    return {
        "schema_version": "d3-complex-1",
        "contract_version": plan.get("contract_version"),
        "contract_hash": plan.get("contract_hash"),
        "triage": "complex",
        "fired_signals": fired,
        "conflicts": conflicts,
        "user_prompt": str(parsed.get("user_prompt")),
        "lock_record": {
            "status": lock_status,
            "decided_at": None,
            "decision": None,
        },
    }


def _action_for(parsed: dict, signal_id: str) -> str:
    for row in parsed.get("conflicts") or []:
        if isinstance(row, dict) and row.get("signal_id") == signal_id:
            return str(row.get("suggested_action"))
    return ""


def _legal(parsed, fired, allowed: Set[str]) -> bool:
    if not isinstance(parsed, dict):
        return False
    prompt = parsed.get("user_prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return False
    rows = parsed.get("conflicts")
    if not isinstance(rows, list):
        return False
    got = {}
    for row in rows:
        if not isinstance(row, dict):
            return False
        sid = row.get("signal_id")
        act = row.get("suggested_action")
        if sid not in fired or act not in allowed:
            return False
        got[sid] = act
    return set(got) == set(fired)


def _abort(plan: dict, fired: list, by_id: Dict[str, dict], lock_status: str) -> dict:
    conflicts = []
    for s in fired:
        loc = by_id.get(s) or {}
        conflicts.append({
            "signal_id": s,
            "source_path": loc.get("source_path") or "plan",
            "impact_scope": loc.get("impact_scope") or "plan",
            "suggested_action": "abort",
        })
    return {
        "schema_version": "d3-complex-1",
        "contract_version": plan.get("contract_version"),
        "contract_hash": plan.get("contract_hash"),
        "triage": "complex",
        "fired_signals": fired,
        "conflicts": conflicts,
        "user_prompt": "abort",
        "lock_record": {
            "status": lock_status,
            "decided_at": None,
            "decision": None,
        },
        "confirmation_fallback": "abort",
    }
