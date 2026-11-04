"""matrix 字段草案：几何分段、C1 跨组合并、D1 子表、候选维度。不调用模型。"""

from __future__ import annotations

from pipeline.block_plan.policy import LayoutBounds


def _cell(grid, r, c):
    if r < 0 or r >= len(grid):
        return ""
    row = grid[r] or []
    if c < 0 or c >= len(row) or row[c] is None:
        return ""
    return str(row[c]).strip()


def _ncols(grid):
    return max((len(r or []) for r in grid), default=0)


def _density(grid, r, ncols):
    if ncols <= 0:
        return 0.0
    n = sum(1 for c in range(ncols) if _cell(grid, r, c))
    return n / ncols


def _merges(block):
    out = []
    for m in (block.get("context") or {}).get("merges") or []:
        if not isinstance(m, dict):
            continue
        out.append(
            (
                int(m.get("r0", -1)),
                int(m.get("c0", -1)),
                int(m.get("r1", -1)),
                int(m.get("c1", -1)),
            )
        )
    return out


def _top_band(grid, merges, bounds):
    nrows = len(grid)
    ncols = _ncols(grid)
    band = []
    for r in range(nrows):
        horiz = any(r0 <= r < r1 and (c1 - c0) >= 2 for r0, c0, r1, c1 in merges)
        if _density(grid, r, ncols) < bounds.sparse_row_density or horiz:
            band.append(r)
            continue
        break
    return band


def _segment_rows(grid, merges, top_band):
    nrows = len(grid)
    ncols = _ncols(grid)
    used = 0
    for r in range(nrows):
        for c in range(ncols):
            if _cell(grid, r, c):
                used = max(used, c + 1)
    titles = []
    for r0, c0, r1, c1 in merges:
        if r0 in top_band:
            continue
        if r1 - r0 != 1:
            continue
        width = c1 - c0
        if width < 3 or (used and width < used / 2):
            continue
        others_empty = all(
            not _cell(grid, r0, c) for c in range(ncols) if not (c0 <= c < c1)
        )
        if not others_empty:
            continue
        above = next((rr for rr in range(r0 - 1, -1, -1) if any(_cell(grid, rr, c) for c in range(ncols))), None)
        below = next((rr for rr in range(r0 + 1, nrows) if any(_cell(grid, rr, c) for c in range(ncols))), None)
        if above is None or below is None:
            continue

        def nempty(rr):
            return sum(1 for c in range(ncols) if _cell(grid, rr, c))

        if nempty(above) > nempty(r0) and nempty(below) > nempty(r0):
            titles.append(r0)
    return sorted(set(titles))


def _span_values(grid, row, c0, c1):
    return [_cell(grid, row, c) for c in range(c0, c1) if _cell(grid, row, c)]


def _row_role(vals):
    if not vals:
        return "empty"
    if len(set(vals)) == 1:
        return "equal"
    return "differ"


def draft_matrix_block(block, tables_by_id, bounds=None):
    """返回 {subtables, fields, segment_title_rows, isomorphic}。非 matrix 返回 None。"""
    if (block or {}).get("layout") != "matrix":
        return None
    bounds = bounds or LayoutBounds()
    tid = (block.get("parent_table_ids") or [None])[0]
    table = (tables_by_id or {}).get(tid) or {}
    grid = table.get("grid") or table.get("rows") or []
    merges = _merges(block)
    nrows = len(grid)
    ncols = _ncols(grid)
    band = _top_band(grid, merges, bounds)
    titles = _segment_rows(grid, merges, band)
    band_set = set(band)

    multi = []
    covered = set()
    for r0, c0, r1, c1 in merges:
        if r0 not in band_set or (c1 - c0) < 2 or (r1 - r0) != 1:
            continue
        multi.append({"label_row": r0, "col_range": [c0, c1], "label": _cell(grid, r0, c0)})
        covered.update(range(c0, c1))
    multi.sort(key=lambda g: g["col_range"][0])

    side_cols = []
    for c in range(ncols):
        if c in covered:
            continue
        if any(_cell(grid, r, c) for r in band):
            side_cols.append(c)

    def classify_group(g):
        c0, c1 = g["col_range"]
        label_row = g["label_row"]
        end = nrows
        for t in titles:
            if t > label_row:
                end = t
                break
        attrs = []
        list_rows = []
        started = False
        for r in range(label_row + 1, end):
            if r in titles:
                break
            vals = _span_values(grid, r, c0, c1)
            kind = _row_role(vals)
            if not started:
                if kind == "equal":
                    attrs.append(r)
                elif kind == "differ":
                    started = True
                    list_rows.append(r)
            else:
                if kind != "empty":
                    list_rows.append(r)
        return attrs, list_rows

    group_parts = []
    for i, g in enumerate(multi):
        attrs, list_rows = classify_group(g)
        group_parts.append({**g, "group_index": i, "attr_rows": attrs, "list_rows": list_rows})

    n_attrs = [len(g["attr_rows"]) for g in group_parts]
    isomorphic = len(set(n_attrs)) <= 1 if group_parts else True

    # raw segments before field ids
    raw = []  # dicts with role, slot, group_index, col_range, value_rows, samples

    def samples_for(c0, c1, rows, limit=3):
        out = []
        for r in rows:
            for c in range(c0, c1):
                v = _cell(grid, r, c)
                if v and v not in out:
                    out.append(v)
                if len(out) >= limit:
                    return out
        return out

    if isomorphic and group_parts:
        # one field per role across groups
        raw.append(
            {
                "role": "group",
                "attribute_slot": None,
                "segments": [
                    {
                        "group_index": g["group_index"],
                        "col_range": g["col_range"],
                        "value_rows": [g["label_row"]],
                    }
                    for g in group_parts
                ],
            }
        )
        slots = n_attrs[0] if n_attrs else 0
        for slot in range(slots):
            raw.append(
                {
                    "role": "attribute",
                    "attribute_slot": slot,
                    "segments": [
                        {
                            "group_index": g["group_index"],
                            "col_range": g["col_range"],
                            "value_rows": [g["attr_rows"][slot]],
                        }
                        for g in group_parts
                        if slot < len(g["attr_rows"])
                    ],
                }
            )
        raw.append(
            {
                "role": "list",
                "attribute_slot": None,
                "segments": [
                    {
                        "group_index": g["group_index"],
                        "col_range": g["col_range"],
                        "value_rows": list(g["list_rows"]),
                    }
                    for g in group_parts
                ],
            }
        )
    else:
        for g in group_parts:
            raw.append(
                {
                    "role": "group",
                    "attribute_slot": None,
                    "segments": [
                        {
                            "group_index": g["group_index"],
                            "col_range": g["col_range"],
                            "value_rows": [g["label_row"]],
                        }
                    ],
                }
            )
            for slot, ar in enumerate(g["attr_rows"]):
                raw.append(
                    {
                        "role": "attribute",
                        "attribute_slot": slot,
                        "segments": [
                            {
                                "group_index": g["group_index"],
                                "col_range": g["col_range"],
                                "value_rows": [ar],
                            }
                        ],
                    }
                )
            raw.append(
                {
                    "role": "list",
                    "attribute_slot": None,
                    "segments": [
                        {
                            "group_index": g["group_index"],
                            "col_range": g["col_range"],
                            "value_rows": list(g["list_rows"]),
                        }
                    ],
                }
            )

    side_fields = []
    for c in side_cols:
        label_row = next((r for r in band if _cell(grid, r, c)), None)
        value_rows = [
            r
            for r in range(nrows)
            if r not in titles and r != label_row and _cell(grid, r, c)
        ]
        side_fields.append(
            {
                "role": "column",
                "attribute_slot": None,
                "segments": [
                    {
                        "group_index": None,
                        "col_range": [c, c + 1],
                        "value_rows": value_rows,
                    }
                ],
            }
        )

    def union_rows(fields, role):
        rows = set()
        for f in fields:
            if role and f["role"] != role:
                continue
            for seg in f["segments"]:
                rows.update(seg["value_rows"])
        return rows

    main_fields = raw
    r_main = union_rows(main_fields, "list")
    r_side = set()
    for f in side_fields:
        for seg in f["segments"]:
            r_side.update(seg["value_rows"])
    # D1：双方都有独占行才拆（门店侧栏行 3 与店名重叠，仍应拆；全重叠则不拆）
    split = bool(r_main - r_side) and bool(r_side - r_main)

    def assign(fields, subtable_id, start):
        out = []
        n = start
        for f in fields:
            segs = f["segments"]
            c0 = segs[0]["col_range"][0]
            vrows = segs[0]["value_rows"]
            samples = []
            for seg in segs:
                samples.extend(
                    samples_for(seg["col_range"][0], seg["col_range"][1], seg["value_rows"])
                )
                if len(samples) >= 3:
                    samples = samples[:3]
                    break
            out.append(
                {
                    "field_id": "f%s" % n,
                    "role": f["role"],
                    "attribute_slot": f["attribute_slot"],
                    "subtable_id": subtable_id,
                    "block_id": block.get("block_id"),
                    "segments": segs,
                    "col": c0,
                    "row": vrows[0] if vrows else None,
                    "value_samples": samples[:3],
                }
            )
            n += 1
        return out, n

    if split:
        fields, n = assign(main_fields, 0, 0)
        side, _n = assign(side_fields, 1, n)
        fields.extend(side)
        subtables = [0, 1]
    else:
        fields, _n = assign(main_fields + side_fields, 0, 0)
        subtables = [0]

    return {
        "block_id": block.get("block_id"),
        "isomorphic": isomorphic,
        "segment_title_rows": titles,
        "subtable_ids": subtables,
        "fields": fields,
        "grid_rows": nrows,
        "grid_cols": ncols,
    }


def dimension_candidates(draft, grid_by_table, block):
    """能区分行的 field_id 列表。"""
    tid = (block.get("parent_table_ids") or [None])[0]
    grid = (grid_by_table or {}).get(tid, {}).get("grid") or []
    cands = []
    by_sub = {}
    for f in draft.get("fields") or []:
        by_sub.setdefault(f["subtable_id"], []).append(f)
    chosen = {sid: [] for sid in by_sub}
    for sid, fields in by_sub.items():
        for f in fields:
            role = f["role"]
            ok = False
            if role == "list":
                ok = True
            elif role == "group":
                scalars = []
                for seg in f["segments"]:
                    vals = _span_values(grid, seg["value_rows"][0], seg["col_range"][0], seg["col_range"][1]) if seg["value_rows"] else []
                    scalars.append(vals[0] if vals else "")
                ok = True  # 组标签可区分组
            elif role == "attribute":
                scalars = []
                for seg in f["segments"]:
                    r = seg["value_rows"][0] if seg["value_rows"] else None
                    vals = _span_values(grid, r, seg["col_range"][0], seg["col_range"][1]) if r is not None else []
                    scalars.append(vals[0] if vals else "")
                ok = len(set(scalars)) >= 2
            elif role == "column":
                seg = f["segments"][0]
                vals = []
                c = seg["col_range"][0]
                for r in seg["value_rows"]:
                    v = _cell(grid, r, c)
                    if v:
                        vals.append(v)
                ok = len(set(vals)) >= 2
            if ok:
                chosen[sid].append(f["field_id"])
                cands.append(f["field_id"])
    return chosen
