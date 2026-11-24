# -*- coding: utf-8 -*-
"""
P0-3: mechanical plan schema coherence gate (no business-column semantics).
Input: m10_real_ast.json cases A/B/C → m10_schema_issues.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.block_cross_table.query_engine.ie_query import (  # noqa: E402
    CONTRACT_FAIL_LOCI,
    FIVE_PART_FIELD_KEYS,
)
from pipeline.block_cross_table.query_engine.plan_structural_markers import (  # noqa: E402
    is_placeholder_utterance_span,
)

DEFAULT_AST = (
    ROOT / "acceptance" / "cross_table" / "query_cases" / "m10_real_ast.json"
)
DEFAULT_OUT = (
    ROOT / "acceptance" / "cross_table" / "query_cases" / "m10_schema_issues.json"
)

_PROV_OK_KEYS = frozenset(
    {
        "utterance_span",
        "portrait_path",
        "memory_ref",
        "reason",
        "reasoning",
        "samples_note",
        "sample_basis",
        "weak",  # structural annotation from annotate_projection_provenance_weak
    }
)
_PLACEHOLDER_IDS = frozenset({"out_col", "out_col_0", "col", "column"})


def _walk(node: Any, visit) -> None:
    if isinstance(node, dict):
        visit(node)
        for v in node.values():
            _walk(v, visit)
    elif isinstance(node, list):
        for x in node:
            _walk(x, visit)


def _table_columns(art: dict[str, Any]) -> set[str]:
    tables = art.get("tables") or {}
    cols: set[str] = set()
    for key, val in tables.items():
        if isinstance(val, list):
            cols.update(str(x) for x in val)
        elif isinstance(val, dict) and isinstance(val.get("columns"), list):
            cols.update(str(x) for x in val["columns"])
    return cols


def _step_output_ids(plan: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if step.get("output"):
            out.add(str(step["output"]))
        if step.get("step_id"):
            out.add(str(step["step_id"]))
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        if args.get("out_column_id"):
            out.add(str(args["out_column_id"]))
    return out


def _five_part_complete(plan: dict[str, Any]) -> bool:
    for k in FIVE_PART_FIELD_KEYS:
        val = plan.get(k)
        if val is None or (isinstance(val, str) and not str(val).strip()):
            # also accept nested five_part
            nested = plan.get("five_part") if isinstance(plan.get("five_part"), dict) else {}
            nval = nested.get(k)
            if nval is None or (isinstance(nval, str) and not str(nval).strip()):
                return False
    return True


def check_case(
    label: str,
    case: dict[str, Any],
    *,
    table_cols: set[str],
) -> dict[str, Any]:
    plan = case.get("plan") if isinstance(case.get("plan"), dict) else {}
    status = str(case.get("status") or plan.get("status") or "")
    issues: list[dict[str, str]] = []
    step_outs = _step_output_ids(plan)
    trees = plan.get("condition_trees") if isinstance(plan.get("condition_trees"), dict) else {}

    # ③ fail_locus enum (when present or needs_user)
    fl = plan.get("fail_locus")
    if fl is None and status == "needs_user":
        fl = (case.get("analysis") or {}).get("fail_locus")
    if status == "needs_user" or fl is not None:
        if not isinstance(fl, str) or not fl.strip():
            issues.append(
                {
                    "code": "fail_locus_missing",
                    "detail": "needs_user or locus present but empty",
                }
            )
        elif fl.strip() not in CONTRACT_FAIL_LOCI:
            issues.append(
                {
                    "code": "fail_locus_not_enum",
                    "detail": "fail_locus not in contract enum (natural language rejected)",
                }
            )

    if status == "needs_user" and not _five_part_complete(plan):
        # structural note for P0-1 gate; not inventing prose
        issues.append(
            {
                "code": "five_part_incomplete",
                "detail": "needs_user plan missing non-empty five-part fields",
            }
        )

    # ② condition_tree_id
    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        ctid = args.get("condition_tree_id")
        if ctid:
            if str(ctid) not in trees:
                inline = args.get("conditions")
                if not (isinstance(inline, list) and inline):
                    issues.append(
                        {
                            "code": "missing_condition_tree",
                            "detail": "condition_tree_id=%s absent and no inline conditions"
                            % ctid,
                        }
                    )

    # ① projection columns
    rp = plan.get("result_projection") if isinstance(plan.get("result_projection"), dict) else {}
    cols = rp.get("columns") if isinstance(rp.get("columns"), list) else []
    allowed = set(table_cols) | step_outs
    for i, col in enumerate(cols):
        if not isinstance(col, dict):
            issues.append(
                {
                    "code": "projection_col_not_object",
                    "detail": "column[%s] not object" % i,
                }
            )
            continue
        cid = str(col.get("column_id") or "").strip()
        if not cid:
            issues.append(
                {
                    "code": "projection_missing_column_id",
                    "detail": "column[%s] missing column_id" % i,
                }
            )
            continue
        is_placeholder = cid in _PLACEHOLDER_IDS or cid.startswith("out_col")
        in_allowed = cid in allowed
        if in_allowed and not is_placeholder:
            continue
        if is_placeholder or not in_allowed:
            # default forbid placeholder; sole exception needs_user+five+projection
            if (
                status == "needs_user"
                and _five_part_complete(plan)
                and str(fl or "").strip() == "projection"
            ):
                issues.append(
                    {
                        "code": "projection_placeholder_needs_user",
                        "detail": "placeholder/unknown column_id=%s allowed only as needs_user; "
                        "case must not enter full-chain capability pass"
                        % cid,
                        "needs_user": "projection_placeholder",
                    }
                )
            else:
                issues.append(
                    {
                        "code": "projection_column_invalid",
                        "detail": "column_id=%s not in table cols or step outputs; "
                        "placeholders forbidden unless needs_user+fail_locus=projection"
                        % cid,
                    }
                )

    # ④ provenance structural keys / hardcode risk
    def visit(n: dict) -> None:
        prov = n.get("provenance")
        if not isinstance(prov, dict):
            return
        for key in prov.keys():
            ks = str(key)
            if ks in _PROV_OK_KEYS:
                continue
            # generic samples mention ok as value text; odd keys that equal table cols → risk
            if ks in table_cols or ks in ("column", "column_id", "hardcoded_column"):
                issues.append(
                    {
                        "code": "provenance_hardcode_risk",
                        "detail": "provenance key %s looks like column hardcoding"
                        % ks,
                    }
                )

    _walk(plan, visit)

    # projection provenance_weak (annotation-class; not a hard fail)
    case_weak = False
    for i, col in enumerate(cols):
        if not isinstance(col, dict):
            continue
        prov = col.get("provenance")
        if not isinstance(prov, dict):
            case_weak = True
            issues.append(
                {
                    "code": "provenance_weak",
                    "detail": "column[%s] provenance missing/non-object" % i,
                    "capability_pass": False,
                }
            )
            continue
        has_portrait = bool(str(prov.get("portrait_path") or "").strip())
        has_memory = bool(str(prov.get("memory_ref") or "").strip())
        marked = bool(prov.get("weak"))
        struct_weak = (
            not has_portrait
            and not has_memory
            and is_placeholder_utterance_span(prov.get("utterance_span"))
        )
        if marked or struct_weak:
            case_weak = True
            issues.append(
                {
                    "code": "provenance_weak",
                    "detail": "column[%s] projection provenance structurally weak"
                    % i,
                    "capability_pass": False,
                }
            )

    return {
        "case": label,
        "status": status,
        "issue_count": len(issues),
        "issues": issues,
        "provenance_weak": case_weak,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ast", type=Path, default=DEFAULT_AST)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    if not args.ast.is_file():
        print(
            "SKIP check_plan_schema_coherence :: missing %s "
            "(private/acceptance fixture; not a product regression)"
            % args.ast.as_posix()
        )
        return 0
    art = json.loads(args.ast.read_text(encoding="utf-8"))
    table_cols = _table_columns(art)
    cases = art.get("cases") or {}
    results = []
    for label in ("A", "B", "C"):
        case = cases.get(label) or {}
        results.append(check_case(label, case, table_cols=table_cols))
    payload = {
        "batch": art.get("batch"),
        "source": str(args.ast.as_posix()),
        "table_column_count": len(table_cols),
        "cases": results,
        "total_issues": sum(r["issue_count"] for r in results),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "out": str(args.out.as_posix()),
                "total_issues": payload["total_issues"],
                "per_case": {r["case"]: r["issue_count"] for r in results},
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
