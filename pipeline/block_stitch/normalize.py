"""按源列归一（R2）。插在 stitch 写行与 implied 之后。"""

from __future__ import annotations


def _is_english_name(name):
    s = str(name or "").replace("_", "")
    return bool(s) and all(c.isascii() and c.isalnum() for c in s)


def _blocks_by_id(block_plan):
    return {b.get("block_id"): b for b in (block_plan or {}).get("blocks") or []}


def _parent_table_id(block):
    if not block:
        return None
    ids = block.get("parent_table_ids") or []
    if len(ids) == 1:
        return ids[0]
    return None


def _source_key(source, block_plan):
    """§1.1：可参与则返回 (parent_table_id, source_col) 或 (parent_table_id, field_id)。
    跨表隔离：仍带 parent_table_id，不跨表合并。"""
    bid = source.get("block_id")
    if bid is None:
        return None
    block = _blocks_by_id(block_plan).get(bid)
    tid = _parent_table_id(block)
    if tid is None:
        return None
    if source.get("kind") == "field" or source.get("field_id"):
        fid = source.get("field_id")
        if fid is None:
            return None
        return (tid, "field:%s" % fid)
    col = source.get("col")
    if col is None:
        return None
    return (tid, int(col))


class _UF:
    def __init__(self):
        self.p = {}

    def add(self, x):
        self.p.setdefault(x, x)

    def find(self, x):
        self.add(x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _pick_display_name(candidates):
    """candidates: list of (mapping_index, target_name)。§3。"""
    if not candidates:
        return None
    ordered = sorted(candidates, key=lambda x: int(x[0] if x[0] is not None else 10**9))
    english = [c for c in ordered if _is_english_name(c[1])]
    pool = english if english else ordered
    return str(pool[0][1])


def _slot_key(slot, block_plan):
    bid = slot.get("block_id")
    col = slot.get("col")
    if bid is None or col is None:
        return None
    block = _blocks_by_id(block_plan).get(bid)
    tid = _parent_table_id(block)
    if tid is None:
        return None
    return (tid, int(col))


def _build_groups(executable, alignments_resolved, block_plan):
    """返回 root_key -> [(mapping_index, target_name, source_dict), ...]。"""
    uf = _UF()
    membership = []  # (key, mid, name, source)

    for item in executable or []:
        mid = item.get("mapping_index")
        name = item.get("target_name")
        for src in item.get("sources_resolved") or []:
            key = _source_key(src, block_plan)
            if key is None:
                continue
            uf.add(key)
            membership.append((key, mid, name, src))

    for align in alignments_resolved or []:
        if not isinstance(align, dict):
            continue
        slots = align.get("slots_resolved") or []
        keys = []
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            k = _slot_key(slot, block_plan)
            if k is not None:
                keys.append(k)
        for i in range(1, len(keys)):
            uf.union(keys[0], keys[i])

    groups = {}
    for key, mid, name, src in membership:
        root = uf.find(key)
        groups.setdefault(root, []).append((mid, name, src))
    return groups


def _origin_for(origins, row_index, mapping_index, old_columns):
    for o in origins or []:
        if o.get("row_index") != row_index:
            continue
        if o.get("mapping_index") != mapping_index:
            continue
        if old_columns is not None and o.get("column") not in old_columns:
            continue
        return o
    for o in origins or []:
        if o.get("row_index") == row_index and o.get("mapping_index") == mapping_index:
            return o
    return None


def _columns_for_mapping(mid, name, stitched_columns, origins):
    """该 mapping 在结果里可能占用的列名。"""
    cols = set()
    bare = str(name) if name is not None else None
    if bare and bare in (stitched_columns or []):
        cols.add(bare)
    tagged = "%s#%s" % (name, mid)
    if tagged in (stitched_columns or []):
        cols.add(tagged)
    bare_tag = "%s#bare" % name
    if bare_tag in (stitched_columns or []):
        cols.add(bare_tag)
    for o in origins or []:
        if o.get("mapping_index") == mid and o.get("column") is not None:
            cols.add(o.get("column"))
    return cols


def _value_on_row(row, cols):
    """该 mapping 在这一输出行写下的非空值。空串/缺格不算写出。"""
    for c in cols:
        if c not in row:
            continue
        v = row.get(c)
        if v is None:
            continue
        if str(v).strip() == "":
            continue
        return v
    return None


def normalize_by_source_column(stitched, executable, alignments_resolved, block_plan):
    """按 (parent_table_id, source_col) 归一列。冲突数组原样保留。"""
    stitched = stitched or {}
    columns = list(stitched.get("columns") or [])
    rows = [dict(r) for r in (stitched.get("rows") or [])]
    origins = [dict(o) for o in (stitched.get("cell_origin") or [])]
    unresolved = stitched.get("unresolved_conflicts")
    implied = stitched.get("stitch_implied_conflicts")

    by_mid = {m.get("mapping_index"): m for m in (executable or [])}
    groups = _build_groups(executable, alignments_resolved, block_plan)

    # mid -> 所属组 root（仅参与归一的）
    mid_root = {}
    for root, members in groups.items():
        for mid, _name, _src in members:
            mid_root[mid] = root

    # 不参与归一的 mapping：保留原列
    untouched_mids = set(by_mid) - set(mid_root)

    mid_old_cols = {}
    for mid, item in by_mid.items():
        mid_old_cols[mid] = _columns_for_mapping(
            mid, item.get("target_name"), columns, origins
        )

    # 决定每组模式：仅当同一输出行上 ≥2 个 mapping 都有非空值且不相等才拆列。
    # 不同行各写各的、或同一行只有一个非空 → 合成一列，值纵向排列。
    group_mode = {}
    group_display = {}
    for root, members in groups.items():
        mids = sorted({m[0] for m in members}, key=lambda x: int(x if x is not None else 10**9))
        cands = []
        seen_mid = set()
        for mid, name, _src in members:
            if mid in seen_mid:
                continue
            seen_mid.add(mid)
            cands.append((mid, name))
        group_display[root] = _pick_display_name(cands)
        mode = "merge"
        if len(mids) >= 2:
            for ri, row in enumerate(rows):
                vals = []
                for mid in mids:
                    v = _value_on_row(row, mid_old_cols.get(mid) or set())
                    if v is None:
                        continue
                    vals.append(str(v).strip())
                if len(vals) >= 2 and len(set(vals)) > 1:
                    mode = "split"
                    break
        group_mode[root] = mode

    # 新列顺序：按 root 稳定序（最小 mid），再未触碰列
    new_columns = []
    col_plan = []  # (new_col, root|None, mid|None, mode)

    roots_ordered = sorted(
        groups.keys(),
        key=lambda r: min(int(m[0]) if m[0] is not None else 10**9 for m in groups[r]),
    )
    for root in roots_ordered:
        members = groups[root]
        mids = sorted({m[0] for m in members}, key=lambda x: int(x if x is not None else 10**9))
        mode = group_mode[root]
        if mode == "merge":
            # 单 mapping 组：保留拼接已经展开的列名（如同 target、不同 source_col 的 #index）。
            # 多 mapping 合成：用 §3 显示名。列名不得与已有列撞车。
            if len(mids) == 1:
                olds = [c for c in columns if c in (mid_old_cols.get(mids[0]) or set())]
                name = olds[0] if len(olds) == 1 else group_display[root]
            else:
                name = group_display[root]
            if name is None:
                continue
            name = str(name)
            if name in new_columns:
                name = "%s#%s" % (name, mids[0])
            new_columns.append(name)
            col_plan.append((name, root, None, "merge"))
        else:
            for mid in mids:
                item = by_mid.get(mid) or {}
                tname = item.get("target_name")
                col = "%s#%s" % (tname, mid)
                new_columns.append(col)
                col_plan.append((col, root, mid, "split"))

    # 未参与归一的 mapping：保留其原列
    for c in columns:
        if c in new_columns:
            continue
        if any(c in (mid_old_cols.get(m) or set()) for m in untouched_mids):
            new_columns.append(c)
            col_plan.append((c, None, None, "keep"))

    # 重建 rows
    new_rows = []
    for ri, row in enumerate(rows):
        new_row = {c: "" for c in new_columns}
        for col, root, mid, mode in col_plan:
            if mode == "keep":
                new_row[col] = row.get(col, "")
                continue
            members = groups[root]
            mids = sorted({m[0] for m in members}, key=lambda x: int(x if x is not None else 10**9))
            if mode == "merge":
                # 取任一非空值（应全等）
                val = ""
                for m in mids:
                    v = _value_on_row(row, mid_old_cols.get(m) or set())
                    if v is not None:
                        val = v
                        break
                new_row[col] = val if val is not None else ""
            else:
                # split：该 mid 的值；若与更小 mid 同值则只写在最小 mid 列
                my_v = _value_on_row(row, mid_old_cols.get(mid) or set())
                if my_v is None:
                    new_row[col] = ""
                    continue
                sv = str(my_v).strip()
                peers = []
                for m in mids:
                    v = _value_on_row(row, mid_old_cols.get(m) or set())
                    if v is not None and str(v).strip() == sv:
                        peers.append(m)
                if not peers:
                    new_row[col] = ""
                    continue
                keeper = min(peers, key=lambda x: int(x if x is not None else 10**9))
                new_row[col] = my_v if mid == keeper else ""
        new_rows.append(new_row)

    # 重建 origins
    new_origins = []
    for ri, row in enumerate(rows):
        for col, root, mid, mode in col_plan:
            if mode == "keep":
                for o in origins:
                    if o.get("row_index") == ri and o.get("column") == col:
                        new_origins.append(dict(o))
                continue
            members = groups[root]
            mids = sorted({m[0] for m in members}, key=lambda x: int(x if x is not None else 10**9))
            if mode == "merge":
                present = []
                for m in mids:
                    v = _value_on_row(row, mid_old_cols.get(m) or set())
                    if v is None:
                        continue
                    present.append(m)
                if not present:
                    continue
                primary_mid = min(present, key=lambda x: int(x if x is not None else 10**9))
                old_cols = mid_old_cols.get(primary_mid) or set()
                base = _origin_for(origins, ri, primary_mid, old_cols)
                if base is None:
                    item = by_mid.get(primary_mid) or {}
                    src0 = (item.get("sources_resolved") or [{}])[0]
                    base = {
                        "row_index": ri,
                        "block_id": src0.get("block_id"),
                        "mapping_index": primary_mid,
                        "source_row": src0.get("row"),
                        "source_col": src0.get("col"),
                    }
                main = dict(base)
                main["column"] = col
                main["mapping_index"] = primary_mid
                also = []
                for m in present:
                    if m == primary_mid:
                        continue
                    oc = _origin_for(origins, ri, m, mid_old_cols.get(m) or set())
                    item = by_mid.get(m) or {}
                    src0 = (item.get("sources_resolved") or [{}])[0]
                    also.append(
                        {
                            "block_id": (oc or {}).get("block_id", src0.get("block_id")),
                            "mapping_index": m,
                            "source_row": (oc or {}).get("source_row", src0.get("row")),
                            "source_col": (oc or {}).get("source_col", src0.get("col")),
                        }
                    )
                also.sort(
                    key=lambda x: int(
                        x.get("mapping_index") if x.get("mapping_index") is not None else 10**9
                    )
                )
                main["also_from"] = also
                new_origins.append(main)
            else:
                val = new_rows[ri].get(col)
                if val is None or (isinstance(val, str) and val == ""):
                    continue
                old_cols = mid_old_cols.get(mid) or set()
                base = _origin_for(origins, ri, mid, old_cols)
                if base is None:
                    item = by_mid.get(mid) or {}
                    src0 = (item.get("sources_resolved") or [{}])[0]
                    base = {
                        "row_index": ri,
                        "block_id": src0.get("block_id"),
                        "mapping_index": mid,
                        "source_row": src0.get("row"),
                        "source_col": src0.get("col"),
                    }
                o = dict(base)
                o["column"] = col
                o["mapping_index"] = mid
                o.pop("also_from", None)
                new_origins.append(o)

    return {
        "columns": new_columns,
        "rows": new_rows,
        "cell_origin": new_origins,
        "unresolved_conflicts": unresolved if unresolved is not None else [],
        "stitch_implied_conflicts": implied if implied is not None else [],
    }
