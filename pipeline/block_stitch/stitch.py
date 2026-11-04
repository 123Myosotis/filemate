"""stitch：按阶段 4a 把桥的 executable 拼成动态列宽表。

实现约束（4b 写码前确认）：
1. gate 的 sources[1..n] 各自调用 binding()，不继承主源绑定。
2. out_row 内部是 {block_id: source_row} 映射；keys_equal 从这里取 b 的源行。
3. unresolved_conflicts 只深拷贝桥的 conflicts；拼接自己发现的冲突写入
   stitch_implied_conflicts[]（结构同 unresolved_conflicts）。
"""

import copy


class StitchError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _spans(block):
    spans = block.get("parent_spans")
    if spans:
        return spans
    tids = block.get("parent_table_ids") or [None]
    return [
        {
            "table_id": tids[0],
            "row_span": list(block.get("row_span") or [0, 0]),
            "col_span": list(block.get("col_span") or [0, 0]),
            "repeat_header": False,
        }
    ]


def _grid(tables_by_id, table_id):
    table = (tables_by_id or {}).get(table_id) or {}
    return table.get("grid") or table.get("rows") or []


def _abs_cell(grid, r, c):
    if r < 0 or c < 0 or r >= len(grid):
        return ""
    row = grid[r] if isinstance(grid[r], (list, tuple)) else []
    if c >= len(row):
        return ""
    val = row[c]
    return "" if val is None else str(val)


def _header_abs_row(block, span):
    if block.get("layout") != "ordered":
        return None
    if span.get("repeat_header"):
        return 0
    r0, _r1 = span["row_span"]
    return r0


def source_rows(block, tables_by_id=None):
    """§1.1：块内数据源行。ordered 跳过字段名行。"""
    rows = []
    for span in _spans(block):
        r0, r1 = span["row_span"]
        header = _header_abs_row(block, span)
        for r in range(r0, r1):
            if header is not None and r == header:
                continue
            rows.append(
                {
                    "table_id": span.get("table_id"),
                    "row": r,
                    "col_span": list(span.get("col_span") or [0, 0]),
                }
            )
    return rows


def _effective_slots(align_item):
    slots = []
    for slot in align_item.get("slots_resolved") or []:
        if not isinstance(slot, dict):
            continue
        if slot.get("key_column") is not True:
            continue
        if slot.get("block_id") is None or slot.get("col") is None:
            continue
        slots.append(slot)
    return slots


def _alignments_usable(alignments_resolved):
    usable = []
    for item in alignments_resolved or []:
        if not isinstance(item, dict):
            continue
        slots = _effective_slots(item)
        if len(slots) < 2:
            continue
        usable.append(
            {
                "align_id": item.get("align_id"),
                "slots_resolved": slots,
                "_by_block": {s.get("block_id"): s for s in slots},
            }
        )
    return usable


def shared_align_ids(block_a, block_b, alignments_resolved):
    """§3.1：两块共同的有效 align_id 列表（保序）。"""
    a_id = block_a.get("block_id") if isinstance(block_a, dict) else block_a
    b_id = block_b.get("block_id") if isinstance(block_b, dict) else block_b
    out = []
    for item in _alignments_usable(alignments_resolved):
        by_block = item["_by_block"]
        if a_id in by_block and b_id in by_block:
            out.append(item["align_id"])
    return out


def _key_text(align_item, block_id, source_row, tables_by_id):
    slot = align_item["_by_block"].get(block_id)
    if slot is None:
        return None
    grid = _grid(tables_by_id, source_row.get("table_id"))
    return _abs_cell(grid, source_row["row"], slot["col"])


def keys_equal(block_a, row_a, block_b, row_b, alignments_resolved, tables_by_id):
    """§3.1：每个共同 align_id 的键都精确相等才 True。"""
    a_id = block_a.get("block_id")
    b_id = block_b.get("block_id")
    ids = shared_align_ids(block_a, block_b, alignments_resolved)
    if not ids:
        return False
    usable = {item["align_id"]: item for item in _alignments_usable(alignments_resolved)}
    for align_id in ids:
        item = usable.get(align_id)
        if item is None:
            return False
        if _key_text(item, a_id, row_a, tables_by_id) != _key_text(
            item, b_id, row_b, tables_by_id
        ):
            return False
    return True


def _connected_components(blocks, alignments_resolved):
    """§3.2：对齐连通分量。返回按块计划顺序的段列表，每段是 block 列表。"""
    n = len(blocks)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    usable = _alignments_usable(alignments_resolved)
    index = {bl.get("block_id"): i for i, bl in enumerate(blocks)}
    for item in usable:
        members = [index[bid] for bid in item["_by_block"] if bid in index]
        for k in range(1, len(members)):
            union(members[0], members[k])

    groups = {}
    order = []
    for i, bl in enumerate(blocks):
        root = find(i)
        if root not in groups:
            groups[root] = []
            order.append(root)
        groups[root].append(bl)
    return [groups[r] for r in order]


def match_component(blocks_in_plan_order, alignments_resolved, tables_by_id):
    """§3.3：段内匹配。

    约束 2：每个 out_row 是 {block_id: source_row} 映射，不是单元格值。
    keys_equal 查已有块 b 的源行时从 out_row[b.block_id] 取。
    """
    out_rows = []
    for block in blocks_in_plan_order:
        bid = block.get("block_id")
        for s in source_rows(block, tables_by_id):
            host = None
            for partial in out_rows:
                # 约束 2：partial 是 {block_id: source_row}
                if bid in partial:
                    continue
                ok = True
                for other_id, other_row in partial.items():
                    other_block = next(
                        (b for b in blocks_in_plan_order if b.get("block_id") == other_id),
                        None,
                    )
                    if other_block is None:
                        ok = False
                        break
                    if not keys_equal(
                        block,
                        s,
                        other_block,
                        other_row,
                        alignments_resolved,
                        tables_by_id,
                    ):
                        ok = False
                        break
                if ok:
                    host = partial
                    break
            if host is not None:
                host[bid] = s
            else:
                out_rows.append({bid: s})
    return out_rows


def binding(block, source, tables_by_id):
    """§3.5：column = 字段名行上的列标题命中；否则 cell。"""
    if not isinstance(source, dict):
        return "cell"
    if block.get("layout") != "ordered":
        return "cell"
    row = source.get("row")
    col = source.get("col")
    if row is None or col is None:
        return "cell"
    header_hit = False
    for span in _spans(block):
        header = _header_abs_row(block, span)
        if header is None or row != header:
            continue
        c0, c1 = span.get("col_span") or [0, 0]
        if not (c0 <= col < c1):
            continue
        grid = _grid(tables_by_id, span.get("table_id"))
        cell = _abs_cell(grid, row, col)
        text = source.get("cell_text")
        text = "" if text is None else str(text)
        if cell == text:
            header_hit = True
            break
    return "column" if header_hit else "cell"


def _primary_source(mapping):
    sources = mapping.get("sources_resolved") or []
    if not sources:
        return None
    return sources[0]


def _text_for_source(block, source, attached_source_row, tables_by_id):
    """按该 source 自己的 binding 取文本（约束 1：不继承主源）。"""
    kind = binding(block, source, tables_by_id)
    if kind == "column":
        if attached_source_row is None:
            return None
        grid = _grid(tables_by_id, attached_source_row.get("table_id"))
        return _abs_cell(grid, attached_source_row["row"], source.get("col"))
    # cell：只有贴上的源行号等于 source.row 时才有文本
    if attached_source_row is None:
        return None
    if attached_source_row.get("row") != source.get("row"):
        return None
    text = source.get("cell_text")
    return "" if text is None else str(text)


def write_value(mapping, text, out_row, blocks_by_id, tables_by_id):
    """§5：用 bridge 的 primitive 写值。

    约束 1：gate 组内 sources[1..n] 各自 binding()，不继承主源。
    out_row：{block_id: source_row}（约束 2）。
    """
    prim = mapping.get("primitive") or "copy"
    rule = mapping.get("rule") if isinstance(mapping.get("rule"), dict) else {}
    if prim == "copy":
        if str(text or "").strip() == "":
            return ""
        return str(text)
    if prim == "replace":
        for row in rule.get("table") or []:
            if not isinstance(row, dict):
                continue
            if str(text) == str(row.get("from")):
                return "" if row.get("to") is None else str(row.get("to"))
        return ""
    if prim == "gate":
        literals = [str(x) for x in (rule.get("literals") or [])]
        texts = [str(text)]
        # 并排行：out_row 含多块时才带上 sources[1..]
        if len(out_row) >= 2:
            sources = mapping.get("sources_resolved") or []
            for src in sources[1:]:
                bid = src.get("block_id")
                if bid not in out_row:
                    continue
                block = blocks_by_id.get(bid)
                if block is None:
                    continue
                # 约束 1：每源用自己的 binding，不继承主源
                extra = _text_for_source(block, src, out_row[bid], tables_by_id)
                if extra is not None:
                    texts.append(str(extra))
        if any(t in literals for t in texts):
            return str(text)
        return ""
    return None


def build_columns(executable, conflicts, implied_conflicts=None):
    """§6：动态列。桥冲突 + 拼接 implied 冲突都展开为 target#mapping_index。"""
    conflict_of = {}
    for group in list(conflicts or []) + list(implied_conflicts or []):
        if not isinstance(group, dict):
            continue
        name = group.get("target_name")
        indexes = group.get("mapping_indexes") or []
        if name is None or not indexes:
            continue
        if name not in conflict_of:
            conflict_of[name] = list(indexes)
        else:
            for idx in indexes:
                if idx not in conflict_of[name]:
                    conflict_of[name].append(idx)

    cols = []
    seen = set()
    for m in sorted(executable or [], key=lambda x: int(x.get("mapping_index") or 0)):
        name = m.get("target_name")
        if name is None or name in seen:
            continue
        seen.add(name)
        if name in conflict_of:
            for idx in conflict_of[name]:
                cols.append("%s#%s" % (name, idx))
        else:
            cols.append(str(name))

    # #bare：未冲突列名与某一已有列字符串完全相同
    final = []
    used = set()
    for c in cols:
        name = str(c)
        if name in used:
            if "#" not in name:
                name = "%s#bare" % name
                while name in used:
                    name = name + "_"
            else:
                continue
        used.add(name)
        final.append(name)
    return final


def _column_for_mapping(mapping, columns, conflict_names):
    name = mapping.get("target_name")
    idx = mapping.get("mapping_index")
    if name in conflict_names:
        tagged = "%s#%s" % (name, idx)
        if tagged in columns:
            return tagged
    if name in columns:
        return name
    bare = "%s#bare" % name
    if bare in columns:
        return bare
    tagged = "%s#%s" % (name, idx)
    if tagged in columns:
        return tagged
    return None


def _fingerprint(mapping):
    return [
        (s.get("block_id"), s.get("row"), s.get("col"))
        for s in mapping.get("sources_resolved") or []
    ]


def _conflict_names(conflicts, implied):
    names = set()
    for group in list(conflicts or []) + list(implied or []):
        if isinstance(group, dict) and group.get("target_name") is not None:
            names.add(group.get("target_name"))
    return names


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
    return vals[0]


def _stitch_matrix_fields(stitch_input, executable, alignments, unresolved_conflicts):
    """宽表骨架（list 展开 + 属性广播）→ R2 → 熔长表。"""
    from pipeline.block_stitch.normalize import normalize_by_source_column
    from pipeline.block_stitch.matrix_long import melt_subtables

    block_plan = stitch_input.get("block_plan") or {}
    tables_by_id = stitch_input.get("tables_by_id") or stitch_input.get("tables") or {}
    dims = stitch_input.get("dimensions_by_block") or {}
    # flatten dims: prefer first block's map; melt uses per-subtable
    dim_flat = {}
    for _bid, by_sub in dims.items():
        if isinstance(by_sub, dict):
            for sid, lst in by_sub.items():
                dim_flat[int(sid) if not isinstance(sid, int) else sid] = list(lst)

    blocks = {b.get("block_id"): b for b in (block_plan or {}).get("blocks") or []}
    # group executable by subtable
    by_sub = {}
    for m in executable:
        src = None
        for s in m.get("sources_resolved") or []:
            if s.get("kind") == "field" or s.get("field_id"):
                src = s
                break
        if src is None:
            continue
        sid = int(src.get("subtable_id") or 0)
        by_sub.setdefault(sid, []).append((m, src))

    # build ONE wide table with all target_name columns (R2 across field keys)
    columns = []
    name_of = {}
    for sid, pairs in sorted(by_sub.items()):
        for m, src in pairs:
            name = m.get("target_name")
            if name not in columns:
                columns.append(name)
            name_of[src.get("field_id")] = name

    wide_rows = []
    origins = []
    for sid, pairs in sorted(by_sub.items()):
        src_of = {src.get("field_id"): src for _m, src in pairs}
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
            gis = set()
            for src in src_of.values():
                for seg in src.get("segments") or []:
                    if seg.get("group_index") is not None:
                        gis.add(seg.get("group_index"))
            if not gis:
                # sidebar-only: one row per nonempty source_row across columns
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
            else:
                for gi in sorted(gis):
                    skeleton.append(
                        {
                            "group_index": gi,
                            "source_row": None,
                            "source_col": None,
                            "list_value": "",
                            "list_field": None,
                        }
                    )

        for sk in skeleton:
            row = {c: "" for c in columns}
            gi = sk["group_index"]
            for fid, src in src_of.items():
                colname = name_of.get(fid)
                if not colname:
                    continue
                if src.get("role") == "list" and fid == sk["list_field"]:
                    row[colname] = sk["list_value"]
                elif src.get("role") in ("group", "attribute"):
                    seg = next(
                        (s for s in (src.get("segments") or []) if s.get("group_index") == gi),
                        None,
                    )
                    row[colname] = "" if seg is None else _span_scalar(grid, seg)
                elif src.get("role") == "column":
                    seg = (src.get("segments") or [{}])[0]
                    c = (seg.get("col_range") or [None])[0]
                    row[colname] = (
                        _cell(grid, sk["source_row"], c) if sk["source_row"] is not None else ""
                    )
            wide_rows.append(row)
            origins.append(
                {
                    "subtable_id": sid,
                    "group_index": gi,
                    "source_row": sk.get("source_row"),
                    "source_col": sk.get("source_col"),
                }
            )

    stitched = normalize_by_source_column(
        {
            "columns": columns,
            "rows": wide_rows,
            "cell_origin": origins,
            "unresolved_conflicts": unresolved_conflicts,
            "stitch_implied_conflicts": [],
        },
        executable,
        alignments,
        block_plan,
    )

    # melt AFTER R2 — rebuild long from field sources + dimensions (use post-R2 names if renamed)
    # Prefer dimensions from stitch_input; melt uses field grid values again for correctness
    subtables = melt_subtables(executable, block_plan, tables_by_id, dim_flat)
    stitched["subtables"] = subtables or []
    stitched["matrix_long"] = True
    # surface first subtable as columns/rows for callers that expect flat shape
    if subtables:
        stitched["columns"] = list(subtables[0].get("columns") or [])
        stitched["rows"] = list(subtables[0].get("rows") or [])
        stitched["wide_before_melt"] = {
            "columns": columns,
            "row_count": len(wide_rows),
        }
    return stitched


def stitch(stitch_input):
    """§7 主流程。先校验 alignments，再建列，再按连通段写出行。"""
    if not isinstance(stitch_input, dict):
        raise StitchError("stitch_input_invalid", "stitch_input is not an object")

    block_plan = stitch_input.get("block_plan") or {}
    tables_by_id = stitch_input.get("tables_by_id") or stitch_input.get("tables") or {}
    executable = list(stitch_input.get("executable") or [])
    conflicts_in = stitch_input.get("conflicts")
    if conflicts_in is None:
        conflicts_in = []
    if not isinstance(conflicts_in, list):
        raise StitchError("stitch_conflicts_not_array", "conflicts is not an array")

    if "alignments_resolved" in stitch_input and stitch_input.get("alignments_resolved") is not None:
        if not isinstance(stitch_input.get("alignments_resolved"), list):
            raise StitchError(
                "stitch_alignments_not_array",
                "alignments_resolved present but not array",
            )
        alignments = stitch_input.get("alignments_resolved")
    else:
        alignments = []

    unresolved_conflicts = copy.deepcopy(conflicts_in)
    stitch_implied_conflicts = []

    executable = sorted(executable, key=lambda m: int(m.get("mapping_index") or 0))
    blocks = list((block_plan or {}).get("blocks") or [])
    blocks_by_id = {bl.get("block_id"): bl for bl in blocks}

    has_field = any(
        (s.get("kind") == "field" or s.get("field_id"))
        for m in executable
        for s in (m.get("sources_resolved") or [])
        if isinstance(s, dict)
    )
    if has_field:
        return _stitch_matrix_fields(stitch_input, executable, alignments, unresolved_conflicts)

    # 桥 conflicts 决定初始展开；stitch_implied 仅在同格写出不同值时登记（约束 3）
    columns = build_columns(executable, unresolved_conflicts, stitch_implied_conflicts)
    if not executable:
        return {
            "columns": [],
            "rows": [],
            "cell_origin": [],
            "unresolved_conflicts": unresolved_conflicts,
            "stitch_implied_conflicts": [],
        }

    conflict_names = _conflict_names(unresolved_conflicts, stitch_implied_conflicts)
    components = _connected_components(blocks, alignments)

    rows = []
    origins = []
    # 已写：row_index -> col -> (mapping_index, value) 用于同列二次写入检测
    written = {}

    def ensure_implied(name, items_for_name, row_index, existing_idx, new_idx, old_v, new_v):
        """同一输出行同列写出不同值且桥未记 → stitch_implied_conflicts。"""
        nonlocal columns, conflict_names, rows, origins, written
        if name in conflict_names:
            return
        fps = [_fingerprint(m) for m in items_for_name]
        entry = {
            "target_name": name,
            "mapping_indexes": [m.get("mapping_index") for m in items_for_name],
            "sources_fingerprint": fps,
        }
        stitch_implied_conflicts.append(entry)
        conflict_names = _conflict_names(unresolved_conflicts, stitch_implied_conflicts)
        old_columns = list(columns)
        columns = build_columns(executable, unresolved_conflicts, stitch_implied_conflicts)
        # 把已写出的该目标单列拆到 #idx
        old_col = name if name in old_columns else None
        if old_col is None:
            return
        for r in rows:
            if old_col not in r:
                continue
            val = r.pop(old_col)
            for c in columns:
                if c not in r:
                    r[c] = ""
            # 原值归属 existing_idx
            tagged_old = "%s#%s" % (name, existing_idx)
            tagged_new = "%s#%s" % (name, new_idx)
            if tagged_old in columns:
                r[tagged_old] = val if r.get("row_index_marker") is None else val
            # 清理：重建该行键集合
            rebuilt = {c: r.get(c, "") for c in columns}
            if tagged_old in columns:
                rebuilt[tagged_old] = val
            if tagged_new in columns and row_index == rows.index(r):
                rebuilt[tagged_new] = new_v
            r.clear()
            r.update(rebuilt)
        # origins：把 column=name 的改成 #existing_idx
        for origin in origins:
            if origin.get("column") == old_col:
                origin["column"] = "%s#%s" % (name, origin.get("mapping_index"))
        # written 索引同步
        new_written = {}
        for ri, cols in written.items():
            new_written[ri] = {}
            for c, pair in cols.items():
                if c == old_col:
                    new_written[ri]["%s#%s" % (name, pair[0])] = pair
                else:
                    new_written[ri][c] = pair
        written.clear()
        written.update(new_written)

    for component in components:
        # 无对齐连通（单块段或彼此无共同对齐）：match_component 仍可用，
        # 但 keys_equal 对无共同 id 返回 false → 每块会各自开行，等于上下相接。
        # 多块无共同对齐时 shared 为空，第一块占满行后第二块永远 keys_equal=false → 另起行。正确。
        partials = match_component(component, alignments, tables_by_id)
        for partial in partials:
            # 约束 2：partial = {block_id: source_row}
            row = {c: "" for c in columns}
            row_index = len(rows)
            written.setdefault(row_index, {})
            for bid, source_row in partial.items():
                block = blocks_by_id.get(bid)
                if block is None:
                    continue
                for m in executable:
                    if (m.get("primitive") or "copy") not in ("copy", "replace", "gate"):
                        continue
                    primary = _primary_source(m)
                    if primary is None or primary.get("block_id") != bid:
                        continue
                    bind = binding(block, primary, tables_by_id)
                    if bind == "cell" and source_row.get("row") != primary.get("row"):
                        continue
                    text = _text_for_source(block, primary, source_row, tables_by_id)
                    if text is None:
                        continue
                    value = write_value(m, text, partial, blocks_by_id, tables_by_id)
                    if value is None:
                        continue
                    name = m.get("target_name")
                    col = _column_for_mapping(m, columns, conflict_names)
                    if col is None:
                        # 可能尚未展开：单列名
                        col = str(name) if str(name) in columns else None
                    if col is None:
                        continue
                    prev = written[row_index].get(col)
                    if prev is not None and prev[0] != m.get("mapping_index"):
                        if prev[1] == value:
                            # 精确相等：保留较小 mapping_index 的 origin，不重复写
                            if int(m.get("mapping_index") or 0) < int(prev[0] or 0):
                                # 替换 origin 归属
                                for origin in origins:
                                    if (
                                        origin.get("row_index") == row_index
                                        and origin.get("column") == col
                                    ):
                                        origin["mapping_index"] = m.get("mapping_index")
                                        origin["block_id"] = bid
                                        origin["source_row"] = source_row.get("row")
                                        origin["source_col"] = primary.get("col")
                                written[row_index][col] = (m.get("mapping_index"), value)
                            continue
                        # 精确不相等且桥未记 → implied 展开
                        items_for_name = [
                            x for x in executable if x.get("target_name") == name
                        ]
                        ensure_implied(
                            name,
                            items_for_name,
                            row_index,
                            prev[0],
                            m.get("mapping_index"),
                            prev[1],
                            value,
                        )
                        col = _column_for_mapping(m, columns, conflict_names)
                        if col is None:
                            continue
                        # 行对象可能已被 rebuild，同步本地 row
                        for c in columns:
                            if c not in row:
                                row[c] = ""
                        # 删掉旧单列键
                        if name in row and name not in columns:
                            del row[name]
                    row[col] = value
                    written[row_index][col] = (m.get("mapping_index"), value)
                    origins.append(
                        {
                            "row_index": row_index,
                            "column": col,
                            "block_id": bid,
                            "mapping_index": m.get("mapping_index"),
                            "source_row": source_row.get("row"),
                            "source_col": primary.get("col"),
                        }
                    )
            # 保证键集合 = columns
            row = {c: row.get(c, "") for c in columns}
            rows.append(row)

    from pipeline.block_stitch.normalize import normalize_by_source_column

    return normalize_by_source_column(
        {
            "columns": columns,
            "rows": rows,
            "cell_origin": origins,
            "unresolved_conflicts": unresolved_conflicts,
            "stitch_implied_conflicts": stitch_implied_conflicts,
        },
        executable,
        alignments,
        block_plan,
    )
