# -*- coding: utf-8 -*-
"""Load real workbook sheets into QE {columns, rows} handles (mechanical; no business rules)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from openpyxl import load_workbook

from pipeline.stage1_parser.file_parser import _cell_comment_text, _cell_fill_rgb

_EMPTY_FILL_COMMENT = {
    "fill_summary": {"color_count": 0, "color_distribution": {}},
    "comments": [],
}


def _load_fill_comment_sidecar(path: Path, *, sheet_index: int = 0) -> dict[str, Any]:
    """Second pass (non-read_only): store fill tokens + comments; no semantic rules."""
    wb = load_workbook(path, read_only=False, data_only=False)
    try:
        names = wb.sheetnames
        if not names:
            return dict(_EMPTY_FILL_COMMENT)
        ws = wb[names[min(sheet_index, len(names) - 1)]]
        dist: dict[str, int] = {}
        comments: list[dict[str, Any]] = []
        for row in ws.iter_rows():
            for cell in row:
                tok = _cell_fill_rgb(cell)
                if tok:
                    dist[tok] = dist.get(tok, 0) + 1
                text = _cell_comment_text(cell)
                if text:
                    comments.append(
                        {
                            "row": int(cell.row) - 1,
                            "col": int(cell.column) - 1,
                            "text": text,
                            "len": len(text),
                        }
                    )
        return {
            "fill_summary": {
                "color_count": len(dist),
                "color_distribution": dict(sorted(dist.items())),
            },
            "comments": comments,
        }
    finally:
        wb.close()


def load_flat_xlsx(
    path: str | Path,
    *,
    table_id: str,
    header_row: int = 0,
    max_rows: int | None = None,
    row_offset: int = 0,
    window_mode: bool = False,
) -> dict[str, Any]:
    """
    First non-empty header row → column ids; subsequent rows → dict rows.
    Duplicate / empty headers get stable synthetic ids (c{i}).

    row_offset / window_mode: I-layer window load only (not silent truncate policy).
    When window_mode=True and max_rows set, stop after the window without remainder→truncated.
    """
    p = Path(path)
    wb = load_workbook(p, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        rows_iter = ws.iter_rows(values_only=True)
        header = None
        for i, row in enumerate(rows_iter):
            if i < header_row:
                continue
            cells = list(row or ())
            if any(c is not None and str(c).strip() != "" for c in cells):
                header = cells
                break
        if header is None:
            out = {
                "table_id": table_id,
                "columns": [],
                "rows": [],
                "source_path": str(p),
                "truncated": False,
                "original_rows": 0,
                "loaded_rows": 0,
            }
            out.update(_load_fill_comment_sidecar(p))
            return out

        columns: list[str] = []
        seen: dict[str, int] = {}
        for i, h in enumerate(header):
            raw = str(h).strip() if h is not None else ""
            if not raw:
                name = "c%d" % i
            else:
                name = raw
                if name in seen:
                    seen[name] += 1
                    name = "%s__%d" % (name, seen[name])
                else:
                    seen[name] = 0
            columns.append(name)

        out_rows: list[dict[str, Any]] = []
        remainder = 0
        hit_cap = False
        skipped = 0
        offset = max(0, int(row_offset or 0))
        for row in rows_iter:
            cells = list(row or ())
            if all(c is None or str(c).strip() == "" for c in cells):
                continue
            item: dict[str, Any] = {}
            empty = True
            for i, col in enumerate(columns):
                val = cells[i] if i < len(cells) else None
                if val is not None and str(val).strip() != "":
                    empty = False
                item[col] = val
            if empty:
                continue
            if skipped < offset:
                skipped += 1
                continue
            if max_rows is not None and len(out_rows) >= max_rows:
                if window_mode:
                    break
                hit_cap = True
                remainder += 1
                continue
            out_rows.append(item)
        loaded = len(out_rows)
        if window_mode:
            out = {
                "table_id": table_id,
                "columns": columns,
                "rows": out_rows,
                "source_path": str(p),
                "truncated": False,
                "original_rows": loaded,
                "loaded_rows": loaded,
                "row_offset": offset,
            }
        else:
            out = {
                "table_id": table_id,
                "columns": columns,
                "rows": out_rows,
                "source_path": str(p),
                "truncated": bool(hit_cap),
                "original_rows": loaded + remainder,
                "loaded_rows": loaded,
            }
        out.update(_load_fill_comment_sidecar(p))
        return out
    finally:
        wb.close()


def load_section_matrix_xlsx(
    path: str | Path,
    *,
    table_id: str,
    max_cells: int | None = None,
) -> dict[str, Any]:
    """
    Matrix with section headers on row 0 spanning column groups.
    Emits flat rows: section_header, cell_col, cell_row, cell_value.
    """
    p = Path(path)
    wb = load_workbook(p, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        grid: list[list[Any]] = []
        for row in ws.iter_rows(values_only=True):
            grid.append(list(row or ()))
        if not grid:
            empty = {
                "table_id": table_id,
                "columns": ["section_header", "cell_col", "cell_row", "cell_value"],
                "rows": [],
                "source_path": str(p),
            }
            empty.update(_load_fill_comment_sidecar(p))
            return empty

        header = grid[0]
        section_at: list[str] = []
        last = ""
        for h in header:
            if h is not None and str(h).strip() != "":
                last = str(h).strip()
            section_at.append(last)

        columns = ["section_header", "cell_col", "cell_row", "cell_value"]
        out: list[dict[str, Any]] = []
        for r_i, row in enumerate(grid):
            if r_i == 0:
                continue
            for c_i, val in enumerate(row):
                if val is None or str(val).strip() == "":
                    continue
                out.append(
                    {
                        "section_header": section_at[c_i] if c_i < len(section_at) else "",
                        "cell_col": c_i,
                        "cell_row": r_i,
                        "cell_value": val,
                    }
                )
                if max_cells is not None and len(out) >= max_cells:
                    break
            if max_cells is not None and len(out) >= max_cells:
                break
        result = {
            "table_id": table_id,
            "columns": columns,
            "rows": out,
            "source_path": str(p),
            "load_mode": "section_matrix",
        }
        result.update(_load_fill_comment_sidecar(p))
        return result
    finally:
        wb.close()


def _layout_facts_from_path(path: str | Path | None) -> dict[str, Any]:
    """1B disclose-only facts via 1a classify_layout + merged_cells (no expand)."""
    from pipeline.block_plan.layout import classify_layout

    out: dict[str, Any] = {
        "layout": "unknown",
        "merge_anchors": [],
        "synthetic_column_count": 0,
        "header_rows_candidate": 0,
    }
    if not path:
        return out
    p = Path(path)
    if not p.is_file():
        return out
    wb = load_workbook(p, read_only=False, data_only=False)
    try:
        if not wb.sheetnames:
            return out
        ws = wb[wb.sheetnames[0]]
        grid: list[list[str]] = []
        for row in ws.iter_rows(values_only=True):
            grid.append([("" if c is None else str(c).strip()) for c in (row or ())])
        merges_half: list[tuple[int, int, int, int]] = []
        anchors: list[dict[str, Any]] = []
        for mr in ws.merged_cells.ranges:
            r0, c0 = int(mr.min_row) - 1, int(mr.min_col) - 1
            r1, c1 = int(mr.max_row), int(mr.max_col)  # half-open end
            merges_half.append((r0, c0, r1, c1))
            anchors.append(
                {
                    "row": r0,
                    "col": c0,
                    "span_rows": r1 - r0,
                    "span_cols": c1 - c0,
                    "is_header_merge": False,
                }
            )
        layout, metrics = classify_layout(grid, merges_half)
        top_band = int(metrics.get("top_sparse_band") or 0)
        top_end = max(1, top_band)
        for a, rect in zip(anchors, merges_half):
            a["is_header_merge"] = bool(rect[0] < top_end and rect[2] > 0)
        # layout from 1a is ordered|matrix only; keep unknown only when empty/unloadable
        out["layout"] = str(layout) if layout in ("ordered", "matrix") else "unknown"
        out["merge_anchors"] = anchors
        out["header_rows_candidate"] = top_band
        return out
    except Exception:  # noqa: BLE001 — disclose path must not crash portrait
        return out
    finally:
        wb.close()


def build_minimal_portrait(
    table: Mapping[str, Any],
    *,
    structure_status: str = "layers_recognized",
) -> dict[str, Any]:
    """Build portrait_query_extensions from loaded columns (mechanical inventory)."""
    columns = list(table.get("columns") or [])
    rows = list(table.get("rows") or [])
    queryable = []
    profiles = []
    join_keys = []
    for col in columns:
        qid = "col:%s" % col
        queryable.append(
            {
                "column_id": qid,
                "column_name": col,
                "type_class": "string_or_mixed",
            }
        )
        vals = []
        for r in rows[:500]:
            if isinstance(r, dict) and r.get(col) is not None:
                vals.append(r.get(col))
        uniq = {str(v) for v in vals}
        card = "low" if len(uniq) <= 32 else "high"
        profiles.append(
            {
                "column_id": qid,
                "cardinality_class": card,
                "term_expand_domain": list(sorted(uniq))[:32] if card == "low" else [],
                "domain_cap_status": "ok" if card == "low" else "unavailable",
                "null_rate": 0.0,
            }
        )
        lower = str(col).lower()
        if any(tok in lower for tok in ("id", "key", "ref", "code")) or col in (
            "cell_value",
            "section_header",
        ):
            join_keys.append({"column_id": qid, "signal": "name_token"})

    portrait = {
        "table_id": table.get("table_id"),
        "structure_status": structure_status,
        "portrait_query_extensions": {
            "queryable_columns": queryable,
            "column_value_profiles": profiles,
            "time_axis_candidates": [],
            "join_key_candidates": join_keys,
        },
        "source_path": table.get("source_path"),
    }
    if "fill_summary" in table:
        portrait["fill_summary"] = table.get("fill_summary")
    if "comments" in table:
        portrait["comments"] = table.get("comments")
    # 1A truncation facts (mechanical; no silent truncate)
    for k in ("truncated", "original_rows", "loaded_rows"):
        if k in table:
            portrait[k] = table.get(k)
    if "truncated" in portrait and "loaded_rows" in portrait and "original_rows" in portrait:
        portrait["truncation_fact"] = "truncated=%s (loaded %s/%s)" % (
            portrait.get("truncated"),
            portrait.get("loaded_rows"),
            portrait.get("original_rows"),
        )
    # 1B disclose-only layout facts (orthogonal to structure_status)
    facts = _layout_facts_from_path(table.get("source_path"))
    facts["synthetic_column_count"] = sum(
        1 for c in columns if re.fullmatch(r"c\d+", str(c) or "")
    )
    portrait.update(facts)
    return portrait
