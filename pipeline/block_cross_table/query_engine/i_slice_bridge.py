# -*- coding: utf-8 -*-
"""I-layer slice bridge: oversized/truncated portraits → row windows for QP (not StagePlanSchema).

Reuses capacity-driven windowing pattern from layer_recognize_slice.plan_propose_slices
and StagePlanSchema's row_range field name — does not emit or validate StagePlanSchema.
"""
from __future__ import annotations

import os
from typing import Any, Mapping, Sequence


def default_rows_per_slice() -> int:
    """Capacity knob (auditor: ~10k rows/slice); override via QUERY_SLICE_ROWS."""
    raw = (os.environ.get("QUERY_SLICE_ROWS") or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return 10_000


def propose_row_slices(
    original_rows: int,
    *,
    rows_per_slice: int | None = None,
) -> list[dict[str, Any]]:
    """Equal row windows [start, end) — same capacity-driven open-window idea as propose_slices."""
    cap = int(rows_per_slice if rows_per_slice is not None else default_rows_per_slice())
    if cap <= 0:
        cap = 10_000
    n = max(0, int(original_rows or 0))
    if n <= 0:
        return []
    out: list[dict[str, Any]] = []
    start = 0
    idx = 0
    while start < n:
        end = min(start + cap, n)
        out.append(
            {
                "slice_id": "s%d" % idx,
                "row_range": [start, end],
                "nrows": end - start,
            }
        )
        start = end
        idx += 1
    return out


def needs_slice_bridge(portrait: Mapping[str, Any], *, rows_per_slice: int | None = None) -> bool:
    """True when 1A truncation disclosed or file rows exceed one-slice capacity."""
    cap = int(rows_per_slice if rows_per_slice is not None else default_rows_per_slice())
    if portrait.get("truncated") is True:
        return True
    orig = portrait.get("original_rows")
    if isinstance(orig, int) and orig > cap:
        return True
    return False


def build_i_slice_route(
    portraits: Sequence[Mapping[str, Any]],
    *,
    active_index: int = 0,
    rows_per_slice: int | None = None,
) -> dict[str, Any]:
    """
    I routing decision before qp_call_ai.
    - Single oversized table → auto-activate one slice (default first).
    - Two+ tables each needing slices → cross_slice_bind_required (needs_user / conflict_e).
    """
    cap = int(rows_per_slice if rows_per_slice is not None else default_rows_per_slice())
    per_table: dict[str, Any] = {}
    needing: list[str] = []
    for p in portraits:
        tid = str(p.get("table_id") or "")
        if not tid:
            continue
        if not needs_slice_bridge(p, rows_per_slice=cap):
            continue
        orig = int(p.get("original_rows") or p.get("loaded_rows") or 0)
        slices = propose_row_slices(orig, rows_per_slice=cap)
        if not slices:
            continue
        needing.append(tid)
        ai = max(0, min(int(active_index), len(slices) - 1))
        active = slices[ai]
        per_table[tid] = {
            "file_original_rows": orig,
            "slices": slices,
            "slices_n": len(slices),
            "active_slice_id": active["slice_id"],
            "active_index": ai,
            "row_range": list(active["row_range"]),
            "rows_per_slice": cap,
        }

    cross = len(needing) >= 2
    return {
        "needed": bool(per_table),
        "cross_slice_bind_required": cross,
        "needing_table_ids": needing,
        "slice_constraints": per_table,
        "route": "conflict_or_needs_user" if cross else ("slice_then_qp" if per_table else "passthrough"),
    }


def apply_slice_route_to_portraits(
    portraits: list[dict[str, Any]],
    route: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Attach slice_constraint; strip silent-truncation facts so QP sees I windows, not truncate."""
    cons = route.get("slice_constraints") or {}
    if not cons:
        return [dict(p) for p in portraits]
    out: list[dict[str, Any]] = []
    for p in portraits:
        row = dict(p)
        tid = str(row.get("table_id") or "")
        sc = cons.get(tid)
        if sc is None:
            out.append(row)
            continue
        for k in ("truncated", "original_rows", "loaded_rows", "truncation_fact"):
            row.pop(k, None)
        rr = list(sc.get("row_range") or [0, 0])
        row["slice_constraint"] = {
            "slice_id": sc.get("active_slice_id"),
            "row_range": rr,
            "slices_n": sc.get("slices_n"),
            "active_index": sc.get("active_index"),
            "file_original_rows": sc.get("file_original_rows"),
            "note": "I-layer window; not silent truncate",
        }
        out.append(row)
    return out


def assert_plan_respects_slice_constraints(
    plan: Mapping[str, Any],
    slice_constraints: Mapping[str, Any] | None,
    tables_by_id: Mapping[str, Any] | None = None,
) -> None:
    """QE/I gate: reject out-of-window row_range claims; else ack / windowed tables / match."""
    from pipeline.block_cross_table.query_engine.errors import QueryEngineError

    if not slice_constraints:
        return
    allowed = set()
    for sc in slice_constraints.values():
        rr = list(sc.get("row_range") or [])
        if len(rr) == 2:
            allowed.add((int(rr[0]), int(rr[1])))
    # Hard reject: plan explicitly claims a row_range outside active windows.
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        args = step.get("args") if isinstance(step.get("args"), Mapping) else {}
        rr = args.get("row_range")
        if isinstance(rr, (list, tuple)) and len(rr) == 2:
            claimed = (int(rr[0]), int(rr[1]))
            if allowed and claimed not in allowed:
                raise QueryEngineError(
                    "plan row_range outside I-layer slice window",
                    fail_locus="other",
                    subreason="slice_constraint_out_of_window",
                    detail={
                        "claimed": list(claimed),
                        "allowed": [list(x) for x in sorted(allowed)],
                    },
                )
    if plan.get("slice_constraint_ack") is True:
        return
    # I already bound data: each constrained table's nrows == active window size.
    if tables_by_id is not None:
        bound_ok = True
        for tid, sc in slice_constraints.items():
            rr = list(sc.get("row_range") or [])
            if len(rr) != 2:
                bound_ok = False
                break
            expect = int(rr[1]) - int(rr[0])
            t = tables_by_id.get(tid) if isinstance(tables_by_id, Mapping) else None
            nrows = len((t or {}).get("rows") or [])
            if nrows != expect:
                bound_ok = False
                break
        if bound_ok and slice_constraints:
            return
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        args = step.get("args") if isinstance(step.get("args"), Mapping) else {}
        rr = args.get("row_range")
        if isinstance(rr, (list, tuple)) and len(rr) == 2:
            if (int(rr[0]), int(rr[1])) in allowed:
                return
    raise QueryEngineError(
        "plan ignores I-layer slice_constraint",
        fail_locus="other",
        subreason="slice_constraint_ignored",
        detail={"slice_constraints": dict(slice_constraints)},
    )


def merge_slice_qe_nrows(per_slice_nrows: Sequence[int | None]) -> int:
    """Sum QE nrows from completed slices; None (needs_user/pending) skipped."""
    total = 0
    for n in per_slice_nrows:
        if n is None:
            continue
        total += int(n)
    return total
