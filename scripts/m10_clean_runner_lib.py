# -*- coding: utf-8 -*-
"""
P0-5 clean M10 runner helpers.
Business words live only in acceptance/.../query_cases/cases/*.json.
Assertions use structural fields only.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = ROOT / "acceptance" / "cross_table" / "query_cases" / "cases"
OUT = ROOT / "acceptance" / "cross_table" / "query_cases"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_case(name: str) -> dict[str, Any]:
    path = CASES_DIR / name
    if not path.suffix:
        path = CASES_DIR / (name + ".json")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_table_path(rel: str) -> Path:
    p = ROOT / rel
    if not p.exists():
        raise FileNotFoundError(rel)
    return p


def load_seed_binding(case: dict[str, Any]) -> dict[str, Any]:
    ref = case.get("seed_binding_ref") or "m10_1_seed_binding.json"
    seed = load_case(ref)
    return dict(seed.get("binding_payload") or {})


def qp_forbidden_literals() -> tuple[str, ...]:
    data = load_case("qp_system_forbidden_literals.json")
    return tuple(str(x) for x in (data.get("literals") or []))


def _walk(node: Any, visit) -> None:
    if isinstance(node, dict):
        visit(node)
        for v in node.values():
            _walk(v, visit)
    elif isinstance(node, list):
        for x in node:
            _walk(x, visit)


def analyze_plan_structural(plan: dict, *, seed_values: list[str] | None = None) -> dict:
    """Structural analysis only; seed_values come from case binding, not runner literals."""
    blob = json.dumps(plan or {}, ensure_ascii=False)
    cols: list[str] = []
    refs: list[str] = []
    or_hit = False
    not_hit = False
    sample_prov = False

    def visit(n: dict) -> None:
        nonlocal or_hit, not_hit, sample_prov
        if str(n.get("node_kind") or "").upper() == "OR":
            or_hit = True
        if str(n.get("node_kind") or "").upper() == "NOT":
            not_hit = True
        args = n.get("args") if isinstance(n.get("args"), dict) else {}
        if str(args.get("logic") or "").upper() == "OR":
            or_hit = True
        for cond in args.get("conditions") or []:
            if isinstance(cond, dict):
                for k in ("column", "column_id", "col"):
                    if cond.get(k):
                        cols.append(str(cond[k]))
                op = str(cond.get("operator") or cond.get("cmp") or "").lower()
                if op in ("not_contains", "ne", "not_in", "neq"):
                    not_hit = True
        for k in ("column", "column_id"):
            if args.get(k):
                cols.append(str(args[k]))
        prov = n.get("provenance") if isinstance(n.get("provenance"), dict) else {}
        if prov.get("memory_ref"):
            refs.append(str(prov["memory_ref"]))
        why = json.dumps(prov, ensure_ascii=False)
        if "sample" in why.lower():
            sample_prov = True

    _walk(plan or {}, visit)
    if re.search(r'"logic"\s*:\s*"OR"', blob, re.I):
        or_hit = True
    if "not_contains" in blob.lower():
        not_hit = True
    vals = [str(v) for v in (seed_values or [])]
    include_hits = [v for v in vals if v in blob]
    return {
        "has_or_or_in": or_hit or bool(include_hits),
        "has_not_logic": not_hit,
        "mapped_columns": sorted(set(cols)),
        "include_value_hits": include_hits,
        "explicit_memory_ref": bool(refs),
        "memory_refs_found": refs,
        "provenance_mentions_samples": sample_prov,
        "fail_locus": (plan or {}).get("fail_locus"),
        "ops": [
            str(s.get("op"))
            for s in (plan.get("steps") or [])
            if isinstance(s, dict) and s.get("op")
        ],
        "five_part_keys_present": all(
            str((plan or {}).get(k) or "").strip()
            or str(((plan or {}).get("five_part") or {}).get(k) or "").strip()
            for k in (
                "speculation",
                "evidence",
                "what_might_be_wrong",
                "next_step",
                "please_confirm_or_correct",
            )
        ),
    }


def structural_checks(
    *,
    status: str | None,
    expected_status: str | None,
    plan: dict,
    ana: dict,
    ai_calls: int | None = None,
) -> dict[str, Any]:
    """Allowed assertion surface for P0-5 runners."""
    return {
        "status": status,
        "status_matches_expected": (
            None
            if expected_status is None
            else status == expected_status
        ),
        "ops": ana.get("ops") or [],
        "fail_locus": ana.get("fail_locus"),
        "five_part_keys_present": bool(ana.get("five_part_keys_present")),
        "rollback_has_query_planner_start": "query_planner_start"
        in list((plan or {}).get("rollback_anchors") or []),
        "ai_calls": ai_calls,
    }


def run_qp_case(
    *,
    case: dict[str, Any],
    portraits: list,
    catalog: dict,
    tables: dict,
    api_key: str,
    memories: list,
    qp_call_ai,
    max_retries: int = 2,
    max_tokens: int = 8192,
    timeout: int = 300,
) -> dict[str, Any]:
    from pipeline.block_cross_table.query_engine.errors import QueryEngineError
    from pipeline.block_cross_table.query_engine.ie_query import (
        CONTRACT_FAIL_LOCI,
        map_fail_locus,
    )

    t0 = time.time()
    err = None
    audit: dict[str, Any] | None = None
    try:
        out = qp_call_ai(
            utterance=str(case.get("utterance") or ""),
            portraits=portraits,
            catalog=catalog,
            tables_by_id=tables,
            api_key=api_key,
            recalled_memories=memories,
            require_full_capability_cover=False,
            apply_column_repair=False,
            max_retries=max_retries,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    except QueryEngineError as exc:
        # P1 step 8.5: catch, map locus, no replan, no binding persist, write audit.
        mapped = map_fail_locus(getattr(exc, "fail_locus", None))
        detail = dict(getattr(exc, "detail", None) or {})
        err = "%s: %s" % (type(exc).__name__, exc)
        audit = {
            "event": "qp_call_ai_failed",
            "fail_locus": mapped,
            "raw_fail_locus": getattr(exc, "fail_locus", None),
            "subreason": getattr(exc, "subreason", None),
            "trace_run_id": detail.get("trace_run_id"),
            "replan": False,
            "persist_binding": False,
        }
        out = {
            "status": "failed",
            "plan": detail.get("last_plan")
            if isinstance(detail.get("last_plan"), dict)
            else {},
            "error": err,
            "fail_locus": mapped,
            "ai_calls": detail.get("ai_calls"),
            "audit": audit,
            "trace_run_id": detail.get("trace_run_id"),
        }
    except Exception as exc:  # noqa: BLE001
        mapped = map_fail_locus(getattr(exc, "fail_locus", None))
        err = "%s: %s" % (type(exc).__name__, exc)
        audit = {
            "event": "qp_call_ai_failed",
            "fail_locus": mapped,
            "replan": False,
            "persist_binding": False,
        }
        out = {
            "status": "failed",
            "plan": {},
            "error": err,
            "fail_locus": mapped,
            "ai_calls": None,
            "audit": audit,
        }
    plan = out.get("plan") or {}
    elapsed_s = round(time.time() - t0, 2)
    # D2 test-only budget gate (mechanical; not semantic).
    if elapsed_s > 45 * 60 or int(out.get("ai_calls") or 0) > 20:
        out = {
            "status": "needs_user",
            "plan": {
                **(plan if isinstance(plan, dict) else {}),
                "status": "needs_user",
                "fail_locus": "budget",
            },
            "ai_calls": out.get("ai_calls"),
            "fail_locus": "budget",
        }
        plan = out["plan"]
        err = err or "D2_budget_gate"
    seed = load_seed_binding(case) if case.get("seed_binding_ref") else {}
    ana = analyze_plan_structural(
        plan, seed_values=list(seed.get("values") or [])
    )
    fl = ana.get("fail_locus")
    if fl is not None and fl not in CONTRACT_FAIL_LOCI and out.get("status") == "needs_user":
        ana["fail_locus_enum_ok"] = False
    else:
        ana["fail_locus_enum_ok"] = fl is None or fl in CONTRACT_FAIL_LOCI
    checks = structural_checks(
        status=out.get("status"),
        expected_status=case.get("expected_status"),
        plan=plan,
        ana=ana,
        ai_calls=out.get("ai_calls"),
    )
    return {
        "case_id": case.get("case_id"),
        "utterance": case.get("utterance"),
        "status": out.get("status"),
        "error": err,
        "ai_calls": out.get("ai_calls"),
        "elapsed_s": elapsed_s,
        "plan": plan,
        "analysis": ana,
        "checks": checks,
        "recalled_memories": memories,
        "label": case.get("case_id"),
        "five_part": out.get("five_part"),
        "e_route": out.get("e_route"),
        "fail_locus": out.get("fail_locus") or ana.get("fail_locus"),
        "provenance_weak": bool(out.get("provenance_weak")),
        "audit": out.get("audit") or audit,
        "trace_run_id": out.get("trace_run_id"),
    }
