"""matrix 字段宽表骨架 → 长表。ordered 拼接不走这里。"""

from __future__ import annotations


def _cell(grid, r, c):
    if grid is None or r is None or r < 0 or r >= len(grid):
        return ""
    row = grid[r] or []
    if c is None or c < 0 or c >= len(row) or row[c] is None:
        return ""
    return str(row[c]).strip()


def _span_scalar(grid, seg):
    rows = seg.get("value_rows") or []
    if not rows:
        return ""
    c0, c1 = seg["col_range"]
    vals = []
    for r in rows:
        for c in range(c0, c1):
            v = _cell(grid, r, c)
            if v:
                vals.append(v)
    if not vals:
        return ""
    if len(set(vals)) == 1:
        return vals[0]
    return None  # inconsistent


def melt_subtables(executable, block_plan, tables_by_id, dimensions_by_subtable):
    """dimensions_by_subtable: {subtable_id: [field_id, ...]}。"""
    fields = []
    for item in executable or []:
        for src in item.get("sources_resolved") or []:
            if src.get("kind") == "field" or src.get("field_id"):
                fields.append((item, src))
                break
    if not fields:
        return None

    by_sub = {}
    for item, src in fields:
        sid = int(src.get("subtable_id") or 0)
        by_sub.setdefault(sid, []).append((item, src))

    blocks = {b.get("block_id"): b for b in (block_plan or {}).get("blocks") or []}
    out = []
    for sid, pairs in sorted(by_sub.items()):
        dims = list((dimensions_by_subtable or {}).get(sid) or (dimensions_by_subtable or {}).get(str(sid)) or [])
        name_of = {src.get("field_id"): item.get("target_name") for item, src in pairs}
        src_of = {src.get("field_id"): src for _item, src in pairs}
        # grid from first block
        bid = pairs[0][1].get("block_id")
        block = blocks.get(bid) or {}
        tid = (block.get("parent_table_ids") or [None])[0]
        grid = ((tables_by_id or {}).get(tid) or {}).get("grid") or []

        list_src = next((s for s in src_of.values() if s.get("role") == "list"), None)
        skeleton = []
        if list_src:
            for seg in list_src.get("segments") or []:
                c0, c1 = seg["col_range"]
                gi = seg.get("group_index")
                for r in seg.get("value_rows") or []:
                    for c in range(c0, c1):
                        v = _cell(grid, r, c)
                        if not v:
                            continue
                        skeleton.append(
                            {
                                "group_index": gi,
                                "source_row": r,
                                "source_col": c,
                                "list_value": v,
                                "list_field": list_src.get("field_id"),
                            }
                        )
        else:
            # 退化：有 group_index 则按组；否则按侧栏 value_rows 展开
            gis = set()
            for src in src_of.values():
                for seg in src.get("segments") or []:
                    if seg.get("group_index") is not None:
                        gis.add(seg.get("group_index"))
            if gis:
                for gi in sorted(gis, key=lambda x: -1 if x is None else x):
                    skeleton.append(
                        {
                            "group_index": gi,
                            "source_row": None,
                            "source_col": None,
                            "list_value": "",
                            "list_field": None,
                        }
                    )
            else:
                rows_set = set()
                for src in src_of.values():
                    for seg in src.get("segments") or []:
                        rows_set.update(seg.get("value_rows") or [])
                for r in sorted(rows_set):
                    skeleton.append(
                        {
                            "group_index": None,
                            "source_row": r,
                            "source_col": None,
                            "list_value": "",
                            "list_field": None,
                        }
                    )
                if not skeleton:
                    skeleton.append(
                        {
                            "group_index": None,
                            "source_row": None,
                            "source_col": None,
                            "list_value": "",
                            "list_field": None,
                        }
                    )

        wide = []
        for sk in skeleton:
            row = {}
            gi = sk["group_index"]
            for fid, src in src_of.items():
                if src.get("role") == "list" and fid == sk["list_field"]:
                    row[fid] = sk["list_value"]
                    continue
                if src.get("role") in ("group", "attribute"):
                    seg = next((s for s in src.get("segments") or [] if s.get("group_index") == gi), None)
                    row[fid] = "" if seg is None else (_span_scalar(grid, seg) or "")
                    continue
                if src.get("role") == "column":
                    seg = (src.get("segments") or [{}])[0]
                    c = (seg.get("col_range") or [None])[0]
                    row[fid] = _cell(grid, sk["source_row"], c) if sk["source_row"] is not None else ""
            wide.append(row)

        dim_ids = [d for d in dims if d in src_of]
        other = [fid for fid in src_of if fid not in dim_ids]
        columns = [name_of[d] for d in dim_ids] + ["attribute_name", "attribute_value"]
        long_rows = []
        for w in wide:
            for fid in other:
                rec = {name_of[d]: w.get(d, "") for d in dim_ids}
                rec["attribute_name"] = name_of[fid]
                rec["attribute_value"] = w.get(fid, "")
                long_rows.append(rec)
        out.append(
            {
                "subtable_id": sid,
                "columns": columns,
                "rows": long_rows,
                "dimension_field_ids": dim_ids,
                "field_ids": list(src_of.keys()),
            }
        )
    return out
