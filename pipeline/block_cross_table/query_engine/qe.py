# -*- coding: utf-8 -*-
"""B4 QE: AST re-check + mechanical step exec + budget gate + step traces."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from .errors import QueryEngineError
from .op_catalog import (
    FAIL_LOCUS_BUDGET,
    SORT_TRUNCATE_OP_ID,
    estimate_step_cost,
    gate_step_budget,
)
from .qp import validate_query_plan_ast
from .trace_query import QueryTraceSink

FAIL_LOCUS_GROUP_LATEST = "group_latest"
FAIL_LOCUS_PREDICATE = "predicate"
FAIL_LOCUS_JOIN_KEY = "join_key"
FAIL_LOCUS_TIME_WINDOW = "time_window"
FAIL_LOCUS_EXISTENCE = "existence"
FAIL_LOCUS_DERIVE = "derive"
FAIL_LOCUS_PROJECTION = "projection"
FAIL_LOCUS_OTHER = "other"


def _table(rows: list[dict[str, Any]], columns: list[str] | None = None) -> dict[str, Any]:
    cols = columns or (sorted({k for r in rows for k in r.keys()}) if rows else [])
    return {"columns": list(cols), "rows": list(rows)}


def _scale_of(handle: dict[str, Any]) -> dict[str, float]:
    rows = handle.get("rows") or []
    cols = handle.get("columns") or []
    n = float(len(rows))
    return {
        "nrows": n,
        "ncells": n * float(len(cols) or 1),
        "n_left": n,
        "n_right": n,
        "group_size_sum": n,
    }


def _cmp_cell(cell: Any, cmp: str, value: Any) -> bool:
    op = str(cmp or "eq").lower()
    if op in ("eq", "==", "equals"):
        return cell == value
    if op in ("ne", "!=", "neq"):
        return cell != value
    if op in ("in",):
        seq = value if isinstance(value, (list, tuple, set)) else [value]
        return cell in seq
    if op in ("not_in", "nin"):
        seq = value if isinstance(value, (list, tuple, set)) else [value]
        return cell not in seq
    if op in ("contains", "include"):
        hay = str(cell if cell is not None else "")
        if isinstance(value, (list, tuple, set)):
            return any(str(v) in hay for v in value if v is not None and str(v) != "")
        return value is not None and str(value) in hay
    if op in ("not_contains", "exclude"):
        hay = str(cell if cell is not None else "")
        if isinstance(value, (list, tuple, set)):
            return all(str(v) not in hay for v in value if v is not None and str(v) != "")
        return value is None or str(value) not in hay
    if op in ("gt", ">"):
        try:
            return cell is not None and cell > value
        except TypeError:
            return False
    if op in ("gte", ">="):
        try:
            return cell is not None and cell >= value
        except TypeError:
            return False
    if op in ("lt", "<"):
        try:
            return cell is not None and cell < value
        except TypeError:
            return False
    if op in ("lte", "<="):
        try:
            return cell is not None and cell <= value
        except TypeError:
            return False
    return cell == value


def _eval_atom(row: Mapping[str, Any], atom: Mapping[str, Any]) -> bool:
    col = str(
        atom.get("column_id")
        or atom.get("column")
        or atom.get("col")
        or ""
    )
    # Prefer explicit comparator; ignore tree-shape tokens mistakenly put in operator.
    raw_op = str(atom.get("operator") or atom.get("op") or "").lower()
    if raw_op in (
        "atom",
        "and",
        "or",
        "not",
        "exists",
        "any",
        "all",
        "",
    ):
        raw_op = ""
    cmp = str(
        atom.get("cmp")
        or atom.get("comparator")
        or atom.get("compare")
        or raw_op
        or "eq"
    )
    value = atom.get("value")
    if "values" in atom and value is None:
        value = atom.get("values")
        if cmp in ("eq", "==", "equals") and isinstance(value, (list, tuple)):
            cmp = "in"
    # Month window: values=[10] or ["10"] against datetime/string timestamps
    if cmp in ("in_month", "month_eq", "month_is"):
        cell = row.get(col)
        months = value if isinstance(value, (list, tuple, set)) else [value]
        want = {int(m) for m in months if str(m).strip().isdigit()}
        ts = _coerce_ts(cell)
        if isinstance(ts, datetime):
            return int(ts.month) in want
        s = str(cell or "")
        for m in want:
            token = "-%02d-" % m
            token2 = "/%02d/" % m
            if token in s or token2 in s:
                return True
        return False
    return _cmp_cell(row.get(col), cmp, value)


def _eval_condition_node(
    row: Mapping[str, Any],
    node: Any,
    *,
    condition_trees: Mapping[str, Any] | None = None,
) -> bool:
    if node is None:
        return True
    if isinstance(node, list):
        if not node:
            return True
        return all(
            _eval_condition_node(row, c, condition_trees=condition_trees) for c in node
        )
    if not isinstance(node, dict):
        return bool(node)
    # Ref to another tree
    ref = node.get("condition_tree_id") or node.get("tree_id")
    trees = condition_trees or {}
    if ref and str(ref) in trees and node.get("node_kind") is None and "atom" not in node:
        return _eval_condition_node(row, trees[str(ref)], condition_trees=trees)

    kind = str(node.get("node_kind") or node.get("kind") or "").upper()
    if not kind:
        # AI often uses operator=and|or|atom as the node kind
        op_as_kind = str(node.get("operator") or "").upper()
        if op_as_kind in ("AND", "OR", "NOT", "ATOM", "EXISTS", "ANY"):
            kind = op_as_kind
        elif "atom" in node and isinstance(node.get("atom"), dict):
            kind = "ATOM"
        elif node.get("conditions") is not None:
            logic = str(node.get("logic") or "AND").upper()
            kind = logic if logic in ("AND", "OR", "NOT") else "AND"
        elif any(k in node for k in ("column_id", "column", "col")):
            kind = "ATOM"

    if kind == "ATOM":
        atom = node.get("atom") if isinstance(node.get("atom"), dict) else node
        return _eval_atom(row, atom if isinstance(atom, dict) else {})
    if kind == "AND":
        children = list(node.get("children") or node.get("conditions") or [])
        if not children:
            return True
        return all(
            _eval_condition_node(row, c, condition_trees=trees) for c in children
        )
    if kind == "OR":
        children = list(node.get("children") or node.get("conditions") or [])
        if not children:
            return False
        return any(
            _eval_condition_node(row, c, condition_trees=trees) for c in children
        )
    if kind == "NOT":
        children = list(node.get("children") or node.get("conditions") or [])
        if not children:
            return True
        # NOT of first child (or AND of all then negate)
        if len(children) == 1:
            return not _eval_condition_node(
                row, children[0], condition_trees=trees
            )
        return not all(
            _eval_condition_node(row, c, condition_trees=trees) for c in children
        )
    if kind in ("EXISTS", "ANY"):
        # Row-local EXISTS: treat children as OR over atoms on this row
        children = list(node.get("children") or node.get("conditions") or [])
        if not children:
            return False
        return any(
            _eval_condition_node(row, c, condition_trees=trees) for c in children
        )
    # Fallback: flat atom fields
    if any(k in node for k in ("column_id", "column", "col")):
        return _eval_atom(row, node)
    return True


def _resolve_filter_predicate(
    args: Mapping[str, Any],
    condition_trees: Mapping[str, Any] | None,
) -> Any:
    ctid = args.get("condition_tree_id")
    trees = condition_trees or {}
    if ctid and str(ctid) in trees:
        return trees[str(ctid)]
    if args.get("conditions") is not None:
        return {
            "node_kind": str(args.get("logic") or "AND").upper(),
            "conditions": list(args.get("conditions") or []),
        }
    if args.get("column_id") or args.get("column"):
        return {
            "node_kind": "ATOM",
            "atom": {
                "column_id": args.get("column_id") or args.get("column"),
                "cmp": args.get("cmp") or args.get("operator") or "eq",
                "value": args.get("value"),
            },
        }
    return None


def _exec_filter(
    inp: dict[str, Any],
    args: Mapping[str, Any],
    *,
    condition_trees: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    pred = _resolve_filter_predicate(args, condition_trees)
    out_rows = []
    for row in inp.get("rows") or []:
        if pred is None:
            # Legacy single-column eq/ne when no tree/conditions
            col = str(args.get("column_id") or "")
            op = str(args.get("cmp") or "eq")
            value = args.get("value")
            ok = _cmp_cell(row.get(col), op, value) if col else True
        else:
            ok = _eval_condition_node(row, pred, condition_trees=condition_trees)
        if ok:
            out_rows.append(dict(row))
    return _table(out_rows, list(inp.get("columns") or []))


def _exec_join(
    left: dict[str, Any],
    right: dict[str, Any],
    args: Mapping[str, Any],
) -> dict[str, Any]:
    mode = str(args.get("mode") or args.get("set_op") or "inner").lower()
    # Set-union / concat: no equi keys required
    if mode in ("union", "union_all", "concat", "or"):
        cols = list(
            dict.fromkeys(
                list(left.get("columns") or []) + list(right.get("columns") or [])
            )
        )
        out = [dict(r) for r in (left.get("rows") or [])]
        seen = None
        if mode == "union":
            seen = set()
            for r in out:
                seen.add(tuple(sorted((k, str(v)) for k, v in r.items())))
        for rr in right.get("rows") or []:
            row = dict(rr)
            if seen is not None:
                key = tuple(sorted((k, str(v)) for k, v in row.items()))
                if key in seen:
                    continue
                seen.add(key)
            out.append(row)
        return _table(out, cols)

    lk = str(
        args.get("left_key")
        or args.get("left_on")
        or args.get("on")
        or ""
    )
    rk = str(
        args.get("right_key")
        or args.get("right_on")
        or args.get("on")
        or ""
    )
    # Fallback: single shared key name
    if (not lk or not rk) and args.get("key"):
        lk = rk = str(args.get("key"))
    if not lk or not rk:
        # Last resort for set_ops without keys: treat as union_all
        if str(args.get("op") or "").lower() in ("union", "set_union") or mode in (
            "set",
            "set_ops",
        ):
            cols = list(
                dict.fromkeys(
                    list(left.get("columns") or []) + list(right.get("columns") or [])
                )
            )
            out = [dict(r) for r in (left.get("rows") or [])] + [
                dict(r) for r in (right.get("rows") or [])
            ]
            return _table(out, cols)
        raise QueryEngineError("join keys missing", fail_locus=FAIL_LOCUS_JOIN_KEY)

    contains_mode = mode in (
        "contains",
        "left_contains_right",
        "substring",
        "left_has_right",
    )
    rcontains_mode = mode in ("right_contains_left", "right_has_left")

    def _keys_match(lv: Any, rv: Any) -> bool:
        if contains_mode:
            ls, rs = str(lv or ""), str(rv or "")
            if not ls or not rs:
                return False
            return rs in ls
        if rcontains_mode:
            ls, rs = str(lv or ""), str(rv or "")
            if not ls or not rs:
                return False
            return ls in rs
        return lv == rv

    out = []
    cols = list(dict.fromkeys(list(left.get("columns") or []) + list(right.get("columns") or [])))
    right_rows = list(right.get("rows") or [])
    for lr in left.get("rows") or []:
        if contains_mode or rcontains_mode:
            # One left row → at most one best right match (longest needle) to
            # avoid many-to-many blowups / budget bombs from short substring keys.
            best = None
            best_len = -1
            for rr in right_rows:
                if not _keys_match(lr.get(lk), rr.get(rk)):
                    continue
                needle = str(rr.get(rk) or "") if contains_mode else str(lr.get(lk) or "")
                nlen = len(needle)
                if nlen > best_len:
                    best, best_len = rr, nlen
            if best is not None:
                merged = dict(lr)
                for k, v in best.items():
                    if k not in merged:
                        merged[k] = v
                out.append(merged)
            continue
        for rr in right_rows:
            if _keys_match(lr.get(lk), rr.get(rk)):
                merged = dict(lr)
                for k, v in rr.items():
                    if k not in merged:
                        merged[k] = v
                out.append(merged)
    return _table(out, cols)


def _coerce_ts(v: Any) -> Any:
    """
    Coerce cell values to comparable timestamps.
    Supports datetime/date, ISO-like strings, and Excel serial day numbers.
    """
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    # date without time
    if hasattr(v, "year") and hasattr(v, "month") and hasattr(v, "day") and not hasattr(v, "hour"):
        try:
            return datetime(int(v.year), int(v.month), int(v.day))
        except Exception:
            pass
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        # Excel serial date (days since 1899-12-30); typical modern range ~30000–60000
        n = float(v)
        if 20000.0 <= n <= 80000.0:
            try:
                return datetime(1899, 12, 30) + timedelta(days=n)
            except Exception:
                return n
        return n
    s = str(v).strip()
    if not s:
        return None
    # Common "YYYY-MM-DD[ HH:MM:SS]"
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            width = 19 if "%H" in fmt else 10
            return datetime.strptime(s[:width], fmt)
        except ValueError:
            continue
    # Numeric string excel serial
    try:
        return _coerce_ts(float(s))
    except ValueError:
        return s


def _ts_key(v: Any) -> Any:
    """Normalize timestamps for range compare."""
    return _coerce_ts(v)


def _in_interval(ts: Any, start: Any, end: Any) -> bool:
    if ts is None or start is None or end is None:
        return False
    a, b, c = _coerce_ts(start), _coerce_ts(ts), _coerce_ts(end)
    if a is None or b is None or c is None:
        return False
    try:
        return a <= b <= c
    except TypeError:
        try:
            return str(a) <= str(b) <= str(c)
        except Exception:
            return False


def _exec_stamp_window(
    inp: dict[str, Any],
    args: Mapping[str, Any],
    *,
    interval_table: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ts_col = str(
        args.get("stamp_column_id")
        or args.get("timestamp_column_id")
        or args.get("time_column_id")
        or ""
    )
    # Range-join against a second table of intervals (duty roster etc.)
    if interval_table is not None:
        start_col = str(
            args.get("interval_start_column_id")
            or args.get("start_column_id")
            or "interval_start"
        )
        end_col = str(
            args.get("interval_end_column_id")
            or args.get("end_column_id")
            or "interval_end"
        )
        attach = args.get("attach_columns")
        attach_cols = (
            [str(c) for c in attach]
            if isinstance(attach, list)
            else None
        )
        out = []
        base_cols = list(inp.get("columns") or [])
        iv_cols = list(interval_table.get("columns") or [])
        def _pick_payload_col(candidates: list[str]) -> str | None:
            """Prefer string-heavy payload cols over numeric id/day-index cols."""
            if not candidates:
                return None
            best_c, best_s = candidates[0], -1
            sample = iv_rows[:80] if iv_rows else []
            for c in candidates:
                score = 0
                for r in sample:
                    v = r.get(c)
                    if isinstance(v, str) and len(v.strip()) >= 2:
                        score += 3
                    elif isinstance(v, (int, float)) and not isinstance(v, bool):
                        if 1.0 <= float(v) <= 31.0:
                            score -= 2
                        else:
                            score -= 1
                    elif v not in (None, ""):
                        score += 1
                if score > best_s:
                    best_c, best_s = c, score
            return best_c

        iv_rows = list(interval_table.get("rows") or [])
        if attach_cols is not None:
            # Keep requested names that exist; map missing aliases onto real iv cols
            extra = [c for c in attach_cols if c in iv_cols]
            if not extra:
                prefer = [
                    str(
                        args.get("value_column_id")
                        or args.get("on_duty_column_id")
                        or ""
                    )
                ]
                extra = [c for c in prefer if c and c in iv_cols]
            if not extra:
                candidates = [
                    c
                    for c in iv_cols
                    if c not in (start_col, end_col, "interval_start", "interval_end")
                ]
                picked = _pick_payload_col(candidates)
                extra = [picked] if picked else candidates
            # If AI asked for an alias, map onto the best payload column
            alias_map: dict[str, str] = {}
            if attach_cols and extra:
                payload = _pick_payload_col(extra) or extra[0]
                for wanted in attach_cols:
                    if wanted not in iv_cols:
                        alias_map[wanted] = payload
        else:
            extra = [c for c in iv_cols if c not in (start_col, end_col)]
            alias_map = {}
        alias_names = list(alias_map.keys())
        cols = list(
            dict.fromkeys(
                base_cols
                + [c for c in extra if c not in base_cols]
                + [c for c in alias_names if c not in base_cols]
            )
        )
        # Default keep rows when interval side empty/unusable
        keep_unmatched = bool(args.get("keep_unmatched", not iv_rows))
        for row in inp.get("rows") or []:
            ts = row.get(ts_col) if ts_col else None
            matched = False
            for iv in iv_rows:
                if not _in_interval(ts, iv.get(start_col), iv.get(end_col)):
                    continue
                # Prefer intervals whose attach payload is not a bare day-index int
                probe_cols = list(extra) + [alias_map[a] for a in alias_map]
                probe_vals = [iv.get(c) for c in probe_cols if c]
                if probe_vals and all(
                    isinstance(v, (int, float))
                    and not isinstance(v, bool)
                    and 1.0 <= float(v) <= 31.0
                    for v in probe_vals
                    if v is not None and v != ""
                ):
                    continue
                merged = dict(row)
                for c in extra:
                    if c not in merged:
                        merged[c] = iv.get(c)
                for alias, src_c in alias_map.items():
                    if alias not in merged:
                        merged[alias] = iv.get(src_c)
                out.append(merged)
                matched = True
                break
            if not matched and keep_unmatched:
                out.append(dict(row))
        return _table(out, cols)

    start = args.get("interval_start")
    end = args.get("interval_end")
    if start is None or end is None:
        # Allow interval object
        iv = args.get("interval") if isinstance(args.get("interval"), dict) else {}
        start = start if start is not None else iv.get("start")
        end = end if end is not None else iv.get("end")
    if start is None or end is None:
        # Soft: pass-through (no window filter) when interval unspecified
        return _table(
            [dict(r) for r in (inp.get("rows") or [])],
            list(inp.get("columns") or []),
        )
    out = []
    for row in inp.get("rows") or []:
        ts = row.get(ts_col)
        if _in_interval(ts, start, end):
            out.append(dict(row))
    return _table(out, list(inp.get("columns") or []))


def _exec_interval_build(
    args: Mapping[str, Any],
    *,
    source_table: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Multi intervals from explicit list
    intervals = args.get("intervals")
    if isinstance(intervals, list) and intervals:
        rows = []
        for item in intervals:
            if not isinstance(item, dict):
                continue
            if "start" not in item or "end" not in item:
                continue
            row = {"interval_start": item["start"], "interval_end": item["end"]}
            for k, v in item.items():
                if k in ("start", "end"):
                    continue
                row[str(k)] = v
            rows.append(row)
        if not rows:
            raise QueryEngineError(
                "intervals list empty/invalid",
                fail_locus=FAIL_LOCUS_TIME_WINDOW,
            )
        cols = list(dict.fromkeys(k for r in rows for k in r.keys()))
        return {"columns": cols, "rows": rows, "interval": rows[0]}

    # Build from a source table (start/end columns OR wide date-header layout)
    if source_table is not None:
        mode = str(args.get("mode") or args.get("layout") or "").lower()
        src_cols = list(source_table.get("columns") or [])
        src_rows = list(source_table.get("rows") or [])

        def _row_datetime_density(row: Mapping[str, Any]) -> int:
            n = 0
            for c in src_cols:
                if isinstance(_coerce_ts(row.get(c)), datetime):
                    n += 1
            return n

        # Auto-promote: many datetime serials in a single row ⇒ wide calendar matrix
        if mode not in (
            "wide_date_headers",
            "date_row_as_columns",
            "columns_are_day_points",
            "row_intervals",
            "start_end",
        ):
            best = max((_row_datetime_density(r) for r in src_rows), default=0)
            if best >= 3:
                mode = "wide_date_headers"
                args = dict(args)
                args["mode"] = mode

        # Wide layout: one row holds date serials per column; later rows hold cell values
        if mode in (
            "wide_date_headers",
            "date_row_as_columns",
            "columns_are_day_points",
        ):
            try:
                date_row_i = int(args.get("date_row_index") or 0)
            except (TypeError, ValueError):
                date_row_i = 0
            if date_row_i < 0 or date_row_i >= len(src_rows):
                raise QueryEngineError(
                    "date_row_index out of range for wide interval build",
                    fail_locus=FAIL_LOCUS_TIME_WINDOW,
                )
            value_key = str(args.get("value_column_id") or "cell_value")
            keep = args.get("keep_columns") or args.get("attach_columns")
            keep_cols = [str(c) for c in keep] if isinstance(keep, list) else [
                c for c in src_cols if c
            ]

            def _dates_in_row(row: Mapping[str, Any]) -> dict[str, Any]:
                found: dict[str, Any] = {}
                for c in src_cols:
                    dv = _coerce_ts(row.get(c))
                    if isinstance(dv, datetime):
                        found[c] = dv
                return found

            def _is_day_index_value(cell: Any) -> bool:
                if isinstance(cell, bool):
                    return False
                if isinstance(cell, (int, float)) and 1.0 <= float(cell) <= 31.0:
                    return True
                s = str(cell).strip()
                return s.isdigit() and 1 <= int(s) <= 31

            date_row = src_rows[date_row_i]
            date_by_col = _dates_in_row(date_row)
            # If the planner pointed at a title/day-index row, scan for the densest
            # coercible date row (generic; no business-column heuristics).
            if not date_by_col:
                best_i = -1
                best_map: dict[str, Any] = {}
                for ri, row in enumerate(src_rows):
                    m = _dates_in_row(row)
                    if len(m) > len(best_map):
                        best_i, best_map = ri, m
                if len(best_map) >= 2:
                    date_row_i = best_i
                    date_by_col = best_map
            if not date_by_col:
                raise QueryEngineError(
                    "wide interval build: no coercible dates in date_row",
                    fail_locus=FAIL_LOCUS_TIME_WINDOW,
                )
            date_cols = list(date_by_col.keys())
            # value_column_id naming a date/header col → emit under neutral field
            if value_key in date_by_col or value_key in ("", "None"):
                value_key = "on_duty_value"

            def _is_meta_calendar_row(row: Mapping[str, Any]) -> bool:
                """Skip day-index / extra date-header rows (generic shape, no jargon)."""
                vals = [row.get(c) for c in date_cols if row.get(c) not in (None, "")]
                if not vals:
                    return True
                dateish = sum(1 for v in vals if isinstance(_coerce_ts(v), datetime))
                if dateish >= max(2, (len(vals) + 1) // 2):
                    return True
                dayish = 0
                for v in vals:
                    if isinstance(v, bool):
                        continue
                    if isinstance(v, (int, float)) and 1.0 <= float(v) <= 31.0:
                        dayish += 1
                if dayish >= max(2, (len(vals) + 1) // 2):
                    return True
                return False

            out_rows = []
            for ri, row in enumerate(src_rows):
                if ri == date_row_i:
                    continue
                if _is_meta_calendar_row(row):
                    continue
                base = {c: row.get(c) for c in keep_cols if c not in date_by_col}
                for c, d0 in date_by_col.items():
                    cell = row.get(c)
                    if cell is None or cell == "":
                        continue
                    if _is_day_index_value(cell):
                        continue
                    d1 = d0 + timedelta(days=1) - timedelta(seconds=1)
                    nr = dict(base)
                    nr["interval_start"] = d0
                    nr["interval_end"] = d1
                    nr[value_key] = cell
                    out_rows.append(nr)
            cols = list(dict.fromkeys(k for r in out_rows for k in r.keys())) or [
                "interval_start",
                "interval_end",
                value_key,
            ]
            return {
                "columns": cols,
                "rows": out_rows,
                "interval": out_rows[0] if out_rows else {},
            }

        start_col = str(
            args.get("start_column_id")
            or args.get("interval_start_column_id")
            or ""
        )
        end_col = str(
            args.get("end_column_id")
            or args.get("interval_end_column_id")
            or ""
        )
        if not start_col or not end_col:
            raise QueryEngineError(
                "interval build needs start/end column ids",
                fail_locus=FAIL_LOCUS_TIME_WINDOW,
            )
        if start_col not in src_cols or end_col not in src_cols:
            raise QueryEngineError(
                "interval start/end columns not in source table: %s / %s"
                % (start_col, end_col),
                fail_locus=FAIL_LOCUS_TIME_WINDOW,
            )
        keep = args.get("keep_columns") or args.get("attach_columns")
        keep_cols = [str(c) for c in keep] if isinstance(keep, list) else None
        rows = []
        for row in src_rows:
            start = _coerce_ts(row.get(start_col))
            end = _coerce_ts(row.get(end_col))
            if start is None or end is None:
                continue
            nr = {"interval_start": start, "interval_end": end}
            extra = keep_cols if keep_cols is not None else [
                c for c in src_cols if c not in (start_col, end_col)
            ]
            for c in extra:
                nr[c] = row.get(c)
            rows.append(nr)
        if not rows:
            raise QueryEngineError(
                "interval build produced zero rows (start/end empty or unparsable)",
                fail_locus=FAIL_LOCUS_TIME_WINDOW,
            )
        cols = list(dict.fromkeys(k for r in rows for k in r.keys())) or [
            "interval_start",
            "interval_end",
        ]
        return {
            "columns": cols,
            "rows": rows,
            "interval": rows[0] if rows else {},
        }

    desc = args.get("interval_desc") if isinstance(args.get("interval_desc"), dict) else {}
    start = (
        desc.get("start")
        if "start" in desc
        else args.get("start") or args.get("interval_start") or desc.get("from")
    )
    end = (
        desc.get("end")
        if "end" in desc
        else args.get("end") or args.get("interval_end") or desc.get("to")
    )
    # year/month shorthand → calendar bounds (mechanical, not business-named)
    if (start is None or end is None) and (
        args.get("year") or desc.get("year") or args.get("month") or desc.get("month")
    ):
        try:
            year = int(args.get("year") or desc.get("year") or 2025)
            month = int(args.get("month") or desc.get("month") or 10)
            start = "%04d-%02d-01" % (year, month)
            # next month start as exclusive end surrogate; stamp uses inclusive end
            if month == 12:
                end = "%04d-12-31" % year
            else:
                end = "%04d-%02d-28" % (year, month)
        except (TypeError, ValueError):
            pass
    if start is not None and end is not None:
        return {
            "columns": ["interval_start", "interval_end"],
            "rows": [{"interval_start": start, "interval_end": end}],
            "interval": {"start": start, "end": end},
        }
    # Soft degrade: empty interval handle so stamp_window can keep_unmatched
    return {
        "columns": ["interval_start", "interval_end"],
        "rows": [],
        "interval": {},
        "degraded": "interval_desc_incomplete",
    }


def _normalize_derive_specs(args: Mapping[str, Any]) -> list[dict[str, Any]]:
    """
    Accept either classic {out_column_id, expr:{...}} or AI batch
    {expressions:[{out_column_id, kind, ...}, ...]}.
    """
    specs: list[dict[str, Any]] = []
    batch = args.get("expressions") or args.get("exprs") or args.get("derives")
    if isinstance(batch, list) and batch:
        for item in batch:
            if not isinstance(item, dict):
                continue
            out_col = str(item.get("out_column_id") or item.get("column_id") or "").strip()
            if not out_col:
                continue
            # item itself may be the expr (kind/when/source at top level)
            if isinstance(item.get("expr"), dict):
                expr = dict(item.get("expr") or {})
            else:
                expr = {
                    k: v
                    for k, v in item.items()
                    if k not in ("out_column_id", "column_id", "display_name")
                }
            specs.append({"out_column_id": out_col, "expr": expr})
        return specs
    out_col = str(args.get("out_column_id") or "").strip()
    expr = args.get("expr")
    if isinstance(expr, dict) and out_col:
        specs.append({"out_column_id": out_col, "expr": expr})
        return specs
    # Flat args: kind/when/source at top level with out_column_id
    if out_col and (
        args.get("kind")
        or args.get("when") is not None
        or args.get("source_column_id")
        or "const" in args
    ):
        expr = {
            k: v
            for k, v in args.items()
            if k not in ("out_column_id", "expressions", "exprs", "derives")
        }
        specs.append({"out_column_id": out_col, "expr": expr})
    return specs


def _derive_one_value(
    row: Mapping[str, Any],
    expr: Mapping[str, Any],
    *,
    condition_trees: Mapping[str, Any] | None = None,
) -> Any:
    kind = str(expr.get("kind") or expr.get("type") or "").lower().strip()
    src = str(
        expr.get("source_column_id")
        or expr.get("from_column_id")
        or expr.get("column_id")
        or ""
    )
    const = expr.get("const") if "const" in expr else expr.get("value")
    if not kind:
        if expr.get("cases") or expr.get("when") is not None or expr.get("when_then"):
            kind = "case"
        elif "const" in expr or (
            "value" in expr and not src and expr.get("when") is None
        ):
            kind = "const"
        elif src:
            kind = "copy"
        else:
            kind = "const"
            const = "" if const is None else const
    if kind in (
        "literal",
        "constant",
        "fixed",
        "empty",
        "placeholder",
        "null",
    ):
        kind = "const"
        if const is None and expr.get("value") is not None:
            const = expr.get("value")
        if const is None:
            const = ""
    if kind in ("map", "rename", "alias", "as", "column_ref", "col_ref", "field"):
        kind = "copy"
    if kind in (
        "case",
        "when",
        "flag_if",
        "cond",
        "condition",
        "if",
        "iff",
        "switch",
        "mark",
        "flag",
        "derive_flag",
        "conditional",
    ):
        kind = "case"

    if kind == "copy":
        return row.get(src)
    if kind == "const":
        return const
    if kind == "case":
        cases = expr.get("cases") or expr.get("when_then") or []
        else_v = expr.get("else", expr.get("default", ""))
        chosen = else_v
        matched = False
        if isinstance(cases, list):
            for case in cases:
                if not isinstance(case, dict):
                    continue
                when = case.get("when")
                if when is None and case.get("condition_tree_id"):
                    when = {"condition_tree_id": case.get("condition_tree_id")}
                if when is None and case.get("atom"):
                    when = {"node_kind": "ATOM", "atom": case.get("atom")}
                if when is None and (case.get("column_id") or case.get("column")):
                    when = {
                        "node_kind": "ATOM",
                        "atom": {
                            "column_id": case.get("column_id") or case.get("column"),
                            "cmp": case.get("cmp") or case.get("operator") or "eq",
                            "value": case.get("value"),
                        },
                    }
                ok = _eval_condition_node(row, when, condition_trees=condition_trees)
                if ok:
                    chosen = case.get("then", case.get("value", True))
                    matched = True
                    break
        if not matched and expr.get("when") is not None:
            if _eval_condition_node(
                row, expr.get("when"), condition_trees=condition_trees
            ):
                chosen = expr.get("then", True)
                matched = True
        if not matched and expr.get("condition_tree_id"):
            if _eval_condition_node(
                row,
                {"condition_tree_id": expr.get("condition_tree_id")},
                condition_trees=condition_trees,
            ):
                chosen = expr.get("then", expr.get("true_value", True))
                matched = True
        return chosen
    if "const" in expr or "value" in expr:
        return expr.get("const", expr.get("value"))
    if src:
        return row.get(src)
    raise QueryEngineError(
        "unknown derive expr kind: %s" % kind,
        fail_locus=FAIL_LOCUS_DERIVE,
    )


def _exec_derive(
    inp: dict[str, Any],
    args: Mapping[str, Any],
    *,
    condition_trees: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    specs = _normalize_derive_specs(args)
    if not specs:
        raise QueryEngineError("derive args invalid", fail_locus=FAIL_LOCUS_DERIVE)
    cols = list(inp.get("columns") or [])
    for spec in specs:
        out_col = str(spec["out_column_id"])
        if out_col not in cols:
            cols = cols + [out_col]
    rows = []
    for row in inp.get("rows") or []:
        nr = dict(row)
        for spec in specs:
            out_col = str(spec["out_column_id"])
            expr = spec.get("expr") if isinstance(spec.get("expr"), dict) else {}
            nr[out_col] = _derive_one_value(
                nr, expr, condition_trees=condition_trees
            )
        rows.append(nr)
    return _table(rows, cols)


def _exec_exists_any(inp: dict[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    col = str(args.get("column_id") or "")
    value = args.get("value")
    found = any(row.get(col) == value for row in (inp.get("rows") or []))
    mode = str(args.get("mode") or "EXISTS")
    if mode not in ("EXISTS", "ANY"):
        raise QueryEngineError("exists mode invalid", fail_locus=FAIL_LOCUS_EXISTENCE)
    return {
        "columns": ["exists_flag"],
        "rows": [{"exists_flag": found}],
        "scalar": found,
    }


def _sortable_cell(v: Any) -> tuple[bool, Any]:
    """None-safe sort key: None last under asc (first under reverse)."""
    return (True, "") if v is None else (False, v)


def _exec_group_top_k(inp: dict[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    gcol = str(args.get("group_column_id") or "")
    scol = str(args.get("sort_column_id") or "")
    direction = str(args.get("direction") or "desc")
    k = int(args.get("k") or 1)
    groups: dict[Any, list[dict[str, Any]]] = {}
    for row in inp.get("rows") or []:
        groups.setdefault(row.get(gcol), []).append(dict(row))
    out = []
    reverse = direction != "asc"
    for _, rows in groups.items():
        rows.sort(key=lambda r: _sortable_cell(r.get(scol)), reverse=reverse)
        out.extend(rows[:k])
    return _table(out, list(inp.get("columns") or []))


def _exec_arg_extremum(inp: dict[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    gcol = str(args.get("group_column_id") or "")
    scol = str(args.get("sort_column_id") or "")
    direction = str(args.get("direction") or "desc")
    groups: dict[Any, list[dict[str, Any]]] = {}
    for row in inp.get("rows") or []:
        groups.setdefault(row.get(gcol), []).append(dict(row))
    out = []
    reverse = direction != "asc"
    for _, rows in groups.items():
        rows.sort(key=lambda r: _sortable_cell(r.get(scol)), reverse=reverse)
        if rows:
            out.append(rows[0])
    return _table(out, list(inp.get("columns") or []))


def _normalize_sort_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """把 AI 常见的 sort / order_by 收成 sort_column_id + direction（机械，不猜业务列）。"""
    out = dict(args or {})
    scol = str(out.get("sort_column_id") or "").strip()
    if scol:
        return out
    raw = out.get("sort")
    if raw is None:
        raw = out.get("order_by")
    if raw is None:
        # 模型偶发用 Python 风格 / 别名字段；只认“列+方向”结构，不猜业务列
        for alias in ("sort_keys", "sort_by", "order_keys", "ranking"):
            raw = out.get(alias)
            if raw is not None:
                break
    if isinstance(raw, str) and raw.strip():
        parts = raw.strip().split()
        out["sort_column_id"] = parts[0]
        if len(parts) >= 2 and not str(out.get("direction") or "").strip():
            d = parts[1].lower().strip(",")
            if d in ("asc", "desc", "ascending", "descending"):
                out["direction"] = "asc" if d.startswith("asc") else "desc"
    elif isinstance(raw, dict):
        out["sort_column_id"] = str(
            raw.get("column_id") or raw.get("column") or raw.get("field") or ""
        ).strip()
        if raw.get("direction") and not str(out.get("direction") or "").strip():
            out["direction"] = str(raw.get("direction"))
    elif isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, str):
            out["sort_column_id"] = first.strip()
        elif isinstance(first, dict):
            out["sort_column_id"] = str(
                first.get("column_id") or first.get("column") or ""
            ).strip()
            if first.get("direction") and not str(out.get("direction") or "").strip():
                out["direction"] = str(first.get("direction"))
    return out


def _exec_sort_truncate(inp: dict[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    if args.get("claims_group_latest") or args.get("as_group_latest"):
        raise QueryEngineError(
            "sort_truncate cannot express group_latest",
            fail_locus=FAIL_LOCUS_GROUP_LATEST,
        )
    args = _normalize_sort_args(args)
    scol = str(args.get("sort_column_id") or "").strip()
    cols = list(inp.get("columns") or [])
    # fail closed：无有效排序列则报错（供上层重试），不静默按空键排序
    if not scol or (cols and scol not in cols):
        raise QueryEngineError(
            "sort_truncate needs valid sort_column_id "
            "(got %r; columns=%s)" % (scol or None, cols[:12]),
            fail_locus=FAIL_LOCUS_OTHER,
            subreason="sort_column_missing",
        )
    direction = str(args.get("direction") or "desc")
    limit = int(args.get("limit") or 0)
    rows = [dict(r) for r in (inp.get("rows") or [])]
    rows.sort(key=lambda r: _sortable_cell(r.get(scol)), reverse=(direction != "asc"))
    if limit > 0:
        rows = rows[:limit]
    return _table(rows, cols)


def _exec_aggregate(inp: dict[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    gcol = str(args.get("group_column_id") or "")
    out_col = str(args.get("out_column_id") or "agg_count")
    counts: dict[Any, int] = {}
    for row in inp.get("rows") or []:
        key = row.get(gcol)
        counts[key] = counts.get(key, 0) + 1
    rows = [{gcol: k, out_col: v} for k, v in counts.items()]
    return _table(rows, [gcol, out_col])


def _apply_projection(table: dict[str, Any], projection: Mapping[str, Any]) -> dict[str, Any]:
    cols_spec = projection.get("columns") or []
    ids = [str(c.get("column_id")) for c in cols_spec if isinstance(c, dict)]
    if not ids:
        raise QueryEngineError("projection columns empty", fail_locus=FAIL_LOCUS_PROJECTION)
    out_rows = []
    for row in table.get("rows") or []:
        out_rows.append({cid: row.get(cid) for cid in ids})
    return _table(out_rows, ids)


_EXECUTORS: dict[str, str] = {
    "op_filter": "filter",
    "op_join": "join",
    "op_stamp_window": "stamp_window",
    "op_interval_build": "interval_build",
    "op_derive": "derive",
    "op_exists_any": "exists_any",
    "op_group_top_k": "group_top_k",
    "op_arg_extremum": "arg_extremum",
    "op_sort_truncate": "sort_truncate",
    "op_aggregate": "aggregate",
    "op_set_ops": "join",  # equality-basis set intersect via join keys for B4 fixture
}


def execute_query_plan(
    plan: Mapping[str, Any],
    catalog: dict[str, Any],
    *,
    tables_by_id: Mapping[str, dict[str, Any]],
    coeff_slots: Mapping[str, float],
    bound_eval: Mapping[str, Any],
    query_exec_budget: Mapping[str, Any],
    trace_sink: QueryTraceSink | None = None,
    require_full_capability_cover: bool = True,
    on_fail_report: Callable[[dict[str, Any]], None] | None = None,
    slice_constraints: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Mechanical QE. Does not rewrite plan on 'unreasonable' results.
    Budget gate before each step; failure → QueryFailLocus + report_to_i.
    """
    if slice_constraints:
        from pipeline.block_cross_table.query_engine.i_slice_bridge import (
            assert_plan_respects_slice_constraints,
        )

        assert_plan_respects_slice_constraints(
            plan, slice_constraints, tables_by_id=tables_by_id
        )
    validate_query_plan_ast(
        plan,
        catalog,
        require_full_capability_cover=require_full_capability_cover,
    )

    handles: dict[str, dict[str, Any]] = {}
    for tid, table in tables_by_id.items():
        handles["src:%s" % tid] = copy.deepcopy(table)

    condition_trees = plan.get("condition_trees") if isinstance(
        plan.get("condition_trees"), dict
    ) else {}

    step_traces: list[dict[str, Any]] = []
    executed = 0

    for step in plan.get("steps") or []:
        step_id = str(step.get("step_id") or "")
        op_id = str(step.get("op") or "")
        args = dict(step.get("args") or {})
        inputs = list(step.get("inputs") or [])
        output = str(step.get("output") or step_id)

        try:
            if op_id == SORT_TRUNCATE_OP_ID and (
                step.get("claims_group_latest") or args.get("as_group_latest")
            ):
                raise QueryEngineError(
                    "global sort_truncate cannot masquerade as group_latest",
                    fail_locus=FAIL_LOCUS_GROUP_LATEST,
                    detail={"step_id": step_id},
                )

            # Absorb alternate input refs AI puts in args (structural aliases only).
            for alt_key in (
                "right_input",
                "right_source",
                "right_table",
                "right_table_id",
                "right_handle",
                "right",
                "interval_input",
                "interval_table",
                "intervals_from",
                "source_table",
                "source",
                "table",
            ):
                ref = args.get(alt_key)
                if isinstance(ref, str) and ref.strip() and ref not in inputs:
                    inputs.append(ref.strip())

            # Resolve input scales for budget.
            resolved = []
            if not inputs and op_id not in ("op_interval_build",):
                raise QueryEngineError(
                    "step inputs empty: %s" % step_id,
                    fail_locus=FAIL_LOCUS_OTHER,
                    detail={"step_id": step_id, "op": op_id},
                )
            for ref in inputs:
                if ref not in handles:
                    # Allow bare table_id without src: prefix
                    alt = ref if ref.startswith("src:") else ("src:%s" % ref)
                    if alt in handles:
                        ref = alt
                    else:
                        raise QueryEngineError(
                            "missing input handle: %s" % ref,
                            fail_locus=FAIL_LOCUS_OTHER,
                            detail={"step_id": step_id, "input": ref},
                        )
                resolved.append(handles[ref])

            if op_id == "op_interval_build":
                if resolved:
                    scale = _scale_of(resolved[0])
                else:
                    scale = {"nrows": 1.0, "ncells": 1.0}
            elif op_id == "op_join" or op_id == "op_set_ops":
                if len(resolved) < 2:
                    raise QueryEngineError(
                        "join needs two inputs",
                        fail_locus=FAIL_LOCUS_JOIN_KEY,
                        detail={"step_id": step_id},
                    )
                scale = {
                    "nrows": float(
                        len(resolved[0].get("rows") or [])
                        + len(resolved[1].get("rows") or [])
                    ),
                    "n_left": float(len(resolved[0].get("rows") or [])),
                    "n_right": float(len(resolved[1].get("rows") or [])),
                    "ncells": 0.0,
                    "group_size_sum": 0.0,
                }
            elif op_id == "op_stamp_window" and len(resolved) >= 2:
                scale = {
                    "nrows": float(
                        len(resolved[0].get("rows") or [])
                        + len(resolved[1].get("rows") or [])
                    ),
                    "n_left": float(len(resolved[0].get("rows") or [])),
                    "n_right": float(len(resolved[1].get("rows") or [])),
                    "ncells": 0.0,
                    "group_size_sum": 0.0,
                }
            else:
                scale = _scale_of(resolved[0]) if resolved else {"nrows": 0.0}

            # Budget gate BEFORE exec — on fail, do not execute.
            try:
                estimated = gate_step_budget(
                    catalog,
                    op_id,
                    scale,
                    query_exec_budget=query_exec_budget,
                    coeff_slots=coeff_slots,
                    bound_eval=bound_eval,
                )
            except Exception as exc:
                locus = getattr(exc, "fail_locus", FAIL_LOCUS_BUDGET)
                raise QueryEngineError(
                    str(exc),
                    fail_locus=locus if locus else FAIL_LOCUS_BUDGET,
                    detail={"step_id": step_id, "op": op_id, "executed": False},
                ) from exc

            # Fan-out: one_to_many edges may list same handle consumed by multiple steps;
            # handles stay immutable snapshots (copy on write below).
            if op_id == "op_filter":
                result = _exec_filter(
                    resolved[0], args, condition_trees=condition_trees
                )
            elif op_id in ("op_join", "op_set_ops"):
                join_args = dict(args) if isinstance(args, dict) else {}
                if op_id == "op_set_ops" and not (
                    join_args.get("left_key")
                    or join_args.get("right_key")
                    or join_args.get("key")
                    or join_args.get("on")
                ):
                    # AI often emits set_ops as row-union without equi keys
                    join_args.setdefault("mode", "union_all")
                result = _exec_join(resolved[0], resolved[1], join_args)
            elif op_id == "op_stamp_window":
                iv_tbl = resolved[1] if len(resolved) >= 2 else None
                # AI may put interval handle name in args instead of inputs[1]
                if iv_tbl is None:
                    ref = (
                        args.get("interval_table")
                        or args.get("interval_table_id")
                        or args.get("interval_handle")
                        or args.get("intervals_from")
                    )
                    if isinstance(ref, str) and ref.strip():
                        key = ref.strip()
                        if key in handles:
                            iv_tbl = handles[key]
                        elif ("src:%s" % key) in handles:
                            iv_tbl = handles["src:%s" % key]
                        else:
                            # fuzzy: match handle suffix / stem
                            for hk, hv in handles.items():
                                if hk.endswith(key) or key in hk:
                                    iv_tbl = hv
                                    break
                result = _exec_stamp_window(
                    resolved[0], args, interval_table=iv_tbl
                )
            elif op_id == "op_interval_build":
                src_tbl = resolved[0] if resolved else None
                result = _exec_interval_build(args, source_table=src_tbl)
            elif op_id == "op_derive":
                result = _exec_derive(
                    resolved[0], args, condition_trees=condition_trees
                )
            elif op_id == "op_exists_any":
                result = _exec_exists_any(resolved[0], args)
            elif op_id == "op_group_top_k":
                result = _exec_group_top_k(resolved[0], args)
            elif op_id == "op_arg_extremum":
                result = _exec_arg_extremum(resolved[0], args)
            elif op_id == SORT_TRUNCATE_OP_ID:
                result = _exec_sort_truncate(
                    resolved[0], _normalize_sort_args(args))
            elif op_id == "op_aggregate":
                result = _exec_aggregate(resolved[0], args)
            else:
                raise QueryEngineError(
                    "no mechanical executor for op: %s" % op_id,
                    fail_locus=FAIL_LOCUS_OTHER,
                    detail={"step_id": step_id},
                )

            handles[output] = result
            # Allow later steps to reference step_id when AI confuses it with output id
            if step_id and step_id != output and step_id not in handles:
                handles[step_id] = result
            # one_to_many: also register fan-out aliases from edges
            for edge in plan.get("edges") or []:
                if (
                    isinstance(edge, dict)
                    and edge.get("from") == output
                    and edge.get("cardinality") == "one_to_many"
                ):
                    alias = edge.get("alias")
                    if alias:
                        handles[str(alias)] = copy.deepcopy(result)

            executed += 1
            trace_body = {
                "step_id": step_id,
                "op": op_id,
                "status": "ok",
                "estimated_cost": estimated,
                "nrows": len(result.get("rows") or []),
                "output": output,
            }
            step_traces.append(trace_body)
            if trace_sink is not None:
                trace_sink.write("query_exec_step", trace_body)

        except QueryEngineError as exc:
            report = exc.as_report()
            report["step_id"] = step_id
            report["op"] = op_id
            report["executed"] = False if exc.fail_locus == FAIL_LOCUS_BUDGET else report.get(
                "detail", {}
            ).get("executed", True)
            if exc.fail_locus == FAIL_LOCUS_BUDGET:
                report["executed"] = False
            step_traces.append(
                {
                    "step_id": step_id,
                    "op": op_id,
                    "status": "failed",
                    "fail_locus": exc.fail_locus,
                    "message": str(exc),
                }
            )
            if trace_sink is not None:
                trace_sink.write(
                    "query_exec_step",
                    {
                        "step_id": step_id,
                        "op": op_id,
                        "status": "failed",
                        "fail_locus": exc.fail_locus,
                    },
                )
            if on_fail_report is not None:
                on_fail_report(report)
            return {
                "status": "failed",
                "fail_locus": exc.fail_locus,
                "subreason": exc.subreason,
                "report_to_i": True,
                "step_traces": step_traces,
                "steps_executed": executed,
                "report": report,
            }

    # Apply plan-level result_projection (not an op).
    final_ref = None
    for step in reversed(list(plan.get("steps") or [])):
        out = step.get("output")
        if out and out in handles and "scalar" not in handles[out]:
            final_ref = out
            break
    if final_ref is None:
        raise QueryEngineError("no table output for projection", fail_locus=FAIL_LOCUS_PROJECTION)

    projected = _apply_projection(handles[final_ref], plan.get("result_projection") or {})
    return {
        "status": "ok",
        "step_traces": step_traces,
        "steps_executed": executed,
        "handles": handles,
        "result_table": projected,
        "report_to_i": False,
    }
