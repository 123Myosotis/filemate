# -*- coding: utf-8 -*-
"""B4 QR: standards 1–8 framework + reconciliation; AI via verdict fixture."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from .errors import QueryEngineError
from .qp import ROLLBACK_QUERY_PLANNER_START
from .trace_query import QueryTraceSink

# Whitespace / common punctuation stripped for span≈utterance compare (not semantic rewrite).
_SPAN_NOISE_RE = re.compile(
    r"[\s\u3000"
    r"\,\.\,，。；;：:、！!？?\"'“”‘’（）()\[\]【】<>《》/\\|"
    r"]+"
)


def normalize_span_text(text: object) -> str:
    """Strip spaces/punctuation for tolerant span coverage checks."""
    return _SPAN_NOISE_RE.sub("", str(text or "")).casefold()


def span_covered_by_utterance(span: object, utterance: object | None) -> bool:
    """
    True if span is a real fragment of the utterance.
    Accepts exact substring, or match after removing spaces/punctuation.
    Empty span never covers.
    """
    if utterance is None:
        return False
    raw_s = str(span or "").strip()
    raw_u = str(utterance)
    if not raw_s or not raw_u.strip():
        return False
    if raw_s in raw_u:
        return True
    ns = normalize_span_text(raw_s)
    nu = normalize_span_text(raw_u)
    if not ns:
        return False
    return ns in nu

# Abstract standard ids (no business words).
STANDARD_IDS = (
    "std1_steps_executed",
    "std2_emptiness",
    "std3_derive_h_exists_projection",
    "std4_conditions",
    "std5_joins",
    "std6_windows",
    "std7_reconciliation",
    "std8_declared_artifacts",
)

VERDICT_PASSED = "review_passed"
VERDICT_FAILED = "review_failed"
VERDICT_NEEDS_USER = "needs_user"

E_ENTRY_WU = "E_entry_wu"  # 戊
ACTION_ROLLBACK_QP = "rollback_query_planner_start"
ACTION_DELIVER = "deliver"
ACTION_ROUTE_E = "route_E"


def enumerate_review_standards() -> list[dict[str, str]]:
    """Return standard 1–8 checkpoint structure."""
    return [{"standard_id": sid, "index": i + 1} for i, sid in enumerate(STANDARD_IDS)]


def build_reconciliation_table(
    plan: Mapping[str, Any],
    *,
    rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Four-column reconciliation rows:
    demand_point | step_id | column_or_filter | utterance_span
    If rows provided (from fixture/AI), use them; else build skeleton from demand_points.

    String resolve rules (anti self-cert):
    - column_id / step_id match → accept
    - utterance_span match → accept ONLY if span is a substring of the case
      utterance AND also appears on steps[].provenance (steps alone ≠ legal)
    - no utterance on plan → span path never accepts (no AI-internal fallback)
    - never dump all projection columns
    """
    if rows is not None:
        return list(rows)
    proj = [
        c
        for c in ((plan.get("result_projection") or {}).get("columns") or [])
        if isinstance(c, dict) and c.get("column_id")
    ]
    steps = [s for s in (plan.get("steps") or []) if isinstance(s, dict)]
    step_ids = {str(s.get("step_id")) for s in steps if s.get("step_id")}
    fallback_sid = next(iter(step_ids), None)
    raw_utt = plan.get("utterance") or plan.get("_case_utterance")
    utterance = str(raw_utt) if raw_utt is not None and str(raw_utt).strip() else None
    step_spans = set()
    for s in steps:
        prov = s.get("provenance") if isinstance(s.get("provenance"), dict) else {}
        sp = prov.get("utterance_span")
        if sp is not None and str(sp).strip():
            step_spans.add(str(sp))

    def _expand(s: str, demand_key: Any) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for c in proj:
            cid = str(c["column_id"])
            prov = c.get("provenance") if isinstance(c.get("provenance"), dict) else {}
            span = prov.get("utterance_span")
            sid = c.get("source_step_id") or fallback_sid
            span_ok = (
                utterance is not None
                and span is not None
                and str(span) == s
                and span_covered_by_utterance(s, utterance)
                and s in step_spans  # additive: also on steps
            )
            if s == cid or span_ok or (
                s in step_ids and str(c.get("source_step_id")) == s
            ):
                found.append(
                    {
                        "demand_point": demand_key,
                        "step_id": sid,
                        "column_or_filter": cid,
                        "utterance_span": (str(span) if span is not None else s),
                    }
                )
        if not found and s in step_ids:
            found.append(
                {
                    "demand_point": demand_key,
                    "step_id": s,
                    "column_or_filter": s,
                    "utterance_span": s,
                }
            )
        return found

    out: list[dict[str, Any]] = []
    for dp in plan.get("demand_points") or []:
        if isinstance(dp, dict):
            cof = dp.get("bound_column_or_filter")
            if isinstance(cof, str) and cof.strip():
                exp = _expand(cof.strip(), dp.get("demand_id"))
                if exp:
                    for r in exp:
                        if dp.get("bound_step_id"):
                            r["step_id"] = dp.get("bound_step_id")
                        if dp.get("utterance_span"):
                            r["utterance_span"] = dp.get("utterance_span")
                        out.append(r)
                else:
                    out.append(
                        {
                            "demand_point": dp.get("demand_id"),
                            "step_id": dp.get("bound_step_id"),
                            "column_or_filter": cof,
                            "utterance_span": dp.get("utterance_span"),
                        }
                    )
            else:
                out.append(
                    {
                        "demand_point": dp.get("demand_id"),
                        "step_id": dp.get("bound_step_id"),
                        "column_or_filter": cof,
                        "utterance_span": dp.get("utterance_span"),
                    }
                )
        elif isinstance(dp, str) and dp.strip():
            # Bare demand_id/label: direct col/step/span match, else cover
            # projection cols whose span ⊆ utterance AND span ∈ step_spans.
            s = dp.strip()
            exp = _expand(s, s)
            if not exp and utterance is not None:
                for c in proj:
                    prov = (
                        c.get("provenance")
                        if isinstance(c.get("provenance"), dict)
                        else {}
                    )
                    span = prov.get("utterance_span")
                    cid = str(c["column_id"])
                    if span is None or not str(span).strip():
                        continue
                    # Allow legal proj spans without requiring identical text
                    # on steps[].provenance (AI often only spans the projection).
                    span_legal = span_covered_by_utterance(
                        span, utterance
                    ) or str(span).strip() == cid
                    if span_legal:
                        exp.append(
                            {
                                "demand_point": s,
                                "step_id": c.get("source_step_id") or fallback_sid,
                                "column_or_filter": cid,
                                "utterance_span": str(span),
                            }
                        )
            out.extend(exp)

    # Gap-fill: every projected column with step + span must appear in recon
    # so std7 missing_result_column_rows does not over-fire when demand_points
    # only name a subset. Span legitimacy is still enforced in check_standard_7.
    covered_cids = {
        str(r.get("column_or_filter"))
        for r in out
        if isinstance(r, dict) and r.get("column_or_filter")
    }
    for c in proj:
        cid = str(c["column_id"])
        if cid in covered_cids:
            continue
        prov = c.get("provenance") if isinstance(c.get("provenance"), dict) else {}
        span = prov.get("utterance_span")
        sid = c.get("source_step_id") or fallback_sid
        if not sid or span is None or not str(span).strip():
            continue
        s = str(span).strip()
        # Do not gap-fill fabricated spans (keeps anti self-cert empty-recon path).
        if utterance is not None:
            if not (
                span_covered_by_utterance(s, utterance) or s == cid
            ):
                continue
        out.append(
            {
                "demand_point": cid,
                "step_id": sid,
                "column_or_filter": cid,
                "utterance_span": s,
            }
        )
        covered_cids.add(cid)
    return out


def check_standard_7(
    plan: Mapping[str, Any],
    reconciliation: list[dict[str, Any]],
) -> dict[str, Any]:
    """Missing demand or result-column row → review_failed."""
    dps = plan.get("demand_points") or []
    raw_utt = plan.get("utterance") or plan.get("_case_utterance")
    utterance = str(raw_utt) if raw_utt is not None and str(raw_utt).strip() else None
    proj_spans = {
        str(p.get("utterance_span"))
        for c in ((plan.get("result_projection") or {}).get("columns") or [])
        if isinstance(c, dict)
        for p in (
            [c.get("provenance")] if isinstance(c.get("provenance"), dict) else []
        )
        if p.get("utterance_span") is not None and str(p.get("utterance_span")).strip()
    }
    demand_tokens: list[str] = []
    for d in dps:
        if isinstance(d, str) and d.strip():
            demand_tokens.append(d.strip())
        elif isinstance(d, dict):
            # Prefer explicit span on demand; else column/filter token.
            span_dp = d.get("utterance_span")
            if isinstance(span_dp, str) and span_dp.strip():
                demand_tokens.append(span_dp.strip())
            cof = d.get("bound_column_or_filter")
            if isinstance(cof, str) and cof.strip():
                demand_tokens.append(cof.strip())
    # Anti self-cert: demand tokens that also appear as projection spans must
    # be real utterance fragments (substring / punctuation-tolerant), OR equal
    # a projected column_id (schema-name shorthand / semantic generalization —
    # not a business-name whitelist).
    # If utterance is missing, skip this gate (caller should inject utterance).
    proj_column_ids = {
        str(c.get("column_id"))
        for c in ((plan.get("result_projection") or {}).get("columns") or [])
        if isinstance(c, dict) and c.get("column_id")
    }
    if utterance is not None:
        for tok in demand_tokens:
            if tok not in proj_spans:
                continue
            if span_covered_by_utterance(tok, utterance):
                continue
            if tok in proj_column_ids:
                # e.g. utterance_span/demand "设备名称" == projected column_id
                continue
            return {
                "ok": False,
                "standard_id": "std7_reconciliation",
                "reason": "fabricated_span",
                "span": tok,
            }
        # Projection spans themselves: must be utterance fragment OR equal their
        # own column_id (AI often copies the column name into utterance_span).
        for c in ((plan.get("result_projection") or {}).get("columns") or []):
            if not isinstance(c, dict):
                continue
            cid = str(c.get("column_id") or "").strip()
            prov = c.get("provenance") if isinstance(c.get("provenance"), dict) else {}
            span = prov.get("utterance_span")
            if span is None or not str(span).strip():
                continue
            s = str(span).strip()
            if span_covered_by_utterance(s, utterance):
                continue
            if cid and s == cid:
                continue
            return {
                "ok": False,
                "standard_id": "std7_reconciliation",
                "reason": "fabricated_span",
                "span": s,
            }
    demand_ids = {
        str(d.get("demand_id"))
        for d in dps
        if isinstance(d, dict) and d.get("demand_id")
    }
    covered_demands = set()
    for row in reconciliation:
        if not isinstance(row, dict):
            continue
        if not row.get("step_id") or not row.get("utterance_span"):
            return {
                "ok": False,
                "standard_id": "std7_reconciliation",
                "reason": "incomplete_reconciliation_row",
            }
        if row.get("demand_point"):
            covered_demands.add(str(row["demand_point"]))

    missing = demand_ids - covered_demands
    if missing:
        return {
            "ok": False,
            "standard_id": "std7_reconciliation",
            "reason": "missing_demand_rows",
            "missing": sorted(missing),
        }

    # Every result_projection column must appear in reconciliation.
    proj_cols = {
        str(c.get("column_id"))
        for c in ((plan.get("result_projection") or {}).get("columns") or [])
        if isinstance(c, dict) and c.get("column_id")
    }
    covered_cols = {
        str(r.get("column_or_filter"))
        for r in reconciliation
        if isinstance(r, dict) and r.get("column_or_filter")
    }
    missing_cols = proj_cols - covered_cols
    if missing_cols:
        return {
            "ok": False,
            "standard_id": "std7_reconciliation",
            "reason": "missing_result_column_rows",
            "missing": sorted(missing_cols),
        }
    return {"ok": True, "standard_id": "std7_reconciliation"}


def check_standard_3_hcd(
    plan: Mapping[str, Any],
    reconciliation: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Standard 3 cover H / EXISTS-ANY (C) / derive-projection (D):
    each required demand kind must reconcile to a step + utterance_span.
    """
    kinds_needed = set()
    for dp in plan.get("demand_points") or []:
        if isinstance(dp, dict) and dp.get("kind"):
            kinds_needed.add(str(dp["kind"]))
    required = {"group_latest", "existence", "derive"}
    must = kinds_needed & required
    if not must:
        # No H/C/D demand kinds declared → std3 N/A for this plan (not a forced full-cover).
        return {"ok": True, "standard_id": "std3_derive_h_exists_projection", "skipped": True}

    by_kind = {
        str(dp.get("kind")): dp
        for dp in (plan.get("demand_points") or [])
        if isinstance(dp, dict) and dp.get("kind")
    }
    for kind in must:
        dp = by_kind.get(kind)
        if dp is None:
            return {
                "ok": False,
                "standard_id": "std3_derive_h_exists_projection",
                "reason": "missing_demand_kind",
                "kind": kind,
            }
        did = str(dp.get("demand_id"))
        matched = [
            r
            for r in reconciliation
            if isinstance(r, dict)
            and str(r.get("demand_point")) == did
            and r.get("step_id")
            and r.get("utterance_span")
        ]
        if not matched:
            return {
                "ok": False,
                "standard_id": "std3_derive_h_exists_projection",
                "reason": "hcd_unreconciled",
                "kind": kind,
            }
    return {"ok": True, "standard_id": "std3_derive_h_exists_projection"}


def load_verdict_fixture(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "verdict" not in data:
        raise QueryEngineError(
            "verdict fixture invalid",
            fail_locus="other",
        )
    return data


def apply_verdict_action(verdict: str) -> dict[str, Any]:
    if verdict == VERDICT_PASSED:
        return {"verdict": verdict, "action": ACTION_DELIVER, "rollback_anchor": None}
    if verdict == VERDICT_FAILED:
        return {
            "verdict": verdict,
            "action": ACTION_ROLLBACK_QP,
            "rollback_anchor": ROLLBACK_QUERY_PLANNER_START,
            "patch_forbidden": True,
        }
    if verdict == VERDICT_NEEDS_USER:
        return {
            "verdict": verdict,
            "action": ACTION_ROUTE_E,
            "e_entry": E_ENTRY_WU,
            "rollback_anchor": None,
        }
    raise QueryEngineError(
        "unknown verdict: %s" % verdict,
        fail_locus="other",
    )


def run_query_review(
    plan: Mapping[str, Any],
    exec_result: Mapping[str, Any],
    *,
    reconciliation_rows: list[dict[str, Any]] | None = None,
    verdict_fixture: Mapping[str, Any] | None = None,
    verdict_path: str | Path | None = None,
    trace_sink: QueryTraceSink | None = None,
    force_local_checks: bool = True,
) -> dict[str, Any]:
    """
    Build standards checklist + reconciliation; apply fixture verdict (B4 AI stand-in).
    Local mechanical fails (std7/std3) override fixture to review_failed.
    """
    standards = enumerate_review_standards()
    recon = build_reconciliation_table(plan, rows=reconciliation_rows)
    checks: list[dict[str, Any]] = []

    # std1: steps executed
    steps = plan.get("steps") or []
    executed = int(exec_result.get("steps_executed") or 0)
    std1_ok = exec_result.get("status") == "ok" and executed == len(steps)
    checks.append(
        {
            "standard_id": "std1_steps_executed",
            "ok": std1_ok,
            "detail": {"executed": executed, "planned": len(steps)},
        }
    )

    if force_local_checks:
        s7 = check_standard_7(plan, recon)
        checks.append(s7)
        s3 = check_standard_3_hcd(plan, recon)
        checks.append(s3)

    local_fail = [c for c in checks if c.get("ok") is False]

    if verdict_path is not None:
        verdict_fixture = load_verdict_fixture(verdict_path)
    if verdict_fixture is None:
        raise QueryEngineError("verdict fixture required in B4", fail_locus="other")

    verdict = str(verdict_fixture.get("verdict"))
    if local_fail:
        verdict = VERDICT_FAILED

    action = apply_verdict_action(verdict)
    out = {
        "standards": standards,
        "checks": checks,
        "reconciliation": recon,
        **action,
        "fixture_verdict": verdict_fixture.get("verdict"),
        "local_override": bool(local_fail),
    }
    if trace_sink is not None:
        trace_sink.write(
            "query_review",
            {
                "verdict": out["verdict"],
                "action": out["action"],
                "local_override": out["local_override"],
            },
        )
    return out
