"""§4–§7 块计划主流程。"""

from datetime import datetime, timezone

from ai.context_budget import estimate_tokens, resolve_context_tokens

from pipeline.block_plan.layout import classify_layout
from pipeline.block_plan.policy import (
    AXIS_COLUMN,
    AXIS_MIXED,
    AXIS_NONE,
    AXIS_ORDER,
    AXIS_REGION,
    AXIS_ROW,
    ERR_BUDGET_NON_POSITIVE,
    ERR_COLUMN_EXCEEDS,
    ERR_HEADER_EXCEEDS,
    ERR_ROW_WITH_HEADER,
    LAYOUT_MATRIX,
    LAYOUT_ORDERED,
    LayoutBounds,
    SerializeSpec,
)
from pipeline.block_plan.regions import cut_merge_count, geometry_regions
from pipeline.block_plan.tokens import TokenAccum, measure_rows


class PlanFailure(Exception):
    def __init__(self, code, message, details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class _Trace:
    def __init__(self):
        self.rows = []

    def add(self, kind, left, right, outcome, refs=None):
        self.rows.append(
            {
                "step": len(self.rows) + 1,
                "kind": kind,
                "left": left,
                "right": right,
                "outcome": outcome,
                "refs": refs or {},
            }
        )


def _rect_rows(grid, row_span, col_span):
    r0, r1 = row_span
    c0, c1 = col_span
    out = []
    for r in range(r0, r1):
        src = grid[r] if r < len(grid) else []
        out.append([src[c] if c < len(src) else "" for c in range(c0, c1)])
    return out


def _payload(grid, row_span, col_span, spec: SerializeSpec):
    rows = _rect_rows(grid, row_span, col_span)
    acc = measure_rows(rows, spec.cell_sep, spec.row_sep)
    return acc.tokens(), acc.nbytes


def _ordered_row_payload(grid, data_span, col_span, spec: SerializeSpec):
    """第 0 行 + 数据行。data_span 不含表头。"""
    header = _rect_rows(grid, (0, 1), col_span)
    body = _rect_rows(grid, data_span, col_span) if data_span[1] > data_span[0] else []
    acc = measure_rows(header + body, spec.cell_sep, spec.row_sep)
    return acc.tokens(), acc.nbytes


def compute_budget(runtime):
    runtime = runtime or {}
    model = runtime.get("model")
    window = runtime.get("context_tokens")
    if window is None:
        window = resolve_context_tokens(model)
    window = int(window)
    fill = float(runtime.get("fill_ratio"))
    reserved = int(runtime.get("reserved_output_tokens"))
    overhead = int(runtime.get("prompt_overhead_tokens"))
    user_tokens = int(runtime.get("user_instruction_tokens") or 0)
    usable = int(window * fill)  # floor via int() toward 0 for positive ratios
    # floor for positive: int() truncates toward 0, matches floor for non-negative
    b_raw = usable - reserved - overhead - user_tokens
    details = {
        "window": window,
        "usable": usable,
        "reserved_output": reserved,
        "overhead": overhead,
        "user_instruction": user_tokens,
        "B_raw": b_raw,
    }
    if b_raw <= 0:
        raise PlanFailure(
            ERR_BUDGET_NON_POSITIVE,
            "B_raw<=0",
            details,
        )
    budget = b_raw
    cap = max(1, window - reserved)
    if budget > cap:
        budget = cap
    details["B"] = budget
    return budget, details


def fit_column_directory(column_names, remaining_tokens, trace=None, spec: SerializeSpec = None):
    """§5：只缩短列名摘要。调用方不得把返回值写回 col_span。"""
    spec = spec or SerializeSpec()
    trace = trace if trace is not None else _Trace()
    names = list(column_names or [])
    chosen = []

    def _ser(items):
        return spec.cell_sep.join(items)

    for name in names:
        trial = chosen + [name]
        tokens = estimate_tokens(_ser(trial))
        if tokens <= remaining_tokens:
            chosen = trial
            trace.add("fit_columns", tokens, remaining_tokens, "accept", {"count": len(chosen)})
        else:
            trace.add("fit_columns", tokens, remaining_tokens, "reject", {"count": len(trial)})
            break
    if not chosen and names:
        kept = names[0]
        while kept and estimate_tokens(kept) > remaining_tokens:
            kept = kept[:-1]
        chosen = [kept]
        tokens = estimate_tokens(kept)
        trace.add("fit_columns", tokens, remaining_tokens, "accept", {"truncated": True})
    return chosen, trace


def _header_tokens(grid, spec):
    if not grid:
        return 0, 0
    c_count = max(len(r) for r in grid)
    return _payload(grid, (0, 1), (0, c_count), spec)


def _accumulate_ordered_rows(grid, b_tokens, spec, trace, merges):
    """§4.2.2：不可行候选丢弃；argmin 只比可行集；全空才按最后不可行轴终败。"""
    r_count = len(grid)
    c_count = max((len(r) for r in grid), default=0)
    full_tokens, _full_bytes = _payload(grid, (0, r_count), (0, c_count), spec)
    h_tokens, _h_bytes = _header_tokens(grid, spec)
    if h_tokens > b_tokens:
        raise PlanFailure(
            ERR_HEADER_EXCEEDS,
            "header exceeds budget",
            {"H": h_tokens, "B": b_tokens},
        )
    candidates = []
    last_err = None
    if full_tokens <= b_tokens:
        tok, nbytes = full_tokens, measure_rows(grid, spec.cell_sep, spec.row_sep).nbytes
        part = {
            "row_span": [0, r_count],
            "col_span": [0, c_count],
            "payload_tokens": tok,
            "payload_bytes": nbytes,
            "repeat_header": False,
        }
        candidates.append({"axis": AXIS_NONE, "parts": [part], "cuts": 0})

    # 行轴：每块 = 表头 + 数据行。不可行则丢弃候选，不在此处否决整表。
    parts = []
    cur = []
    header_line = spec.cell_sep.join(_rect_rows(grid, (0, 1), (0, c_count))[0]) if r_count else ""
    base = TokenAccum()
    base.feed(header_line)
    running = base.copy()
    row_feasible = True
    for r in range(1, r_count):
        line = spec.cell_sep.join(_rect_rows(grid, (r, r + 1), (0, c_count))[0])
        trial = running.copy()
        trial.feed(spec.row_sep)
        trial.feed(line)
        ok = trial.tokens() <= b_tokens
        trace.add(
            "fit_block",
            trial.tokens(),
            b_tokens,
            "accept" if ok else "reject",
            {"axis": AXIS_ROW, "row": r},
        )
        if ok:
            running = trial
            cur.append(r)
            continue
        if not cur:
            row_tok, _b = _ordered_row_payload(grid, (r, r + 1), (0, c_count), spec)
            last_err = PlanFailure(
                ERR_ROW_WITH_HEADER,
                "single data row with header exceeds budget",
                {"row": r, "H": h_tokens, "row_payload": row_tok, "B": b_tokens},
            )
            row_feasible = False
            break
        parts.append(list(cur))
        running = base.copy()
        running.feed(spec.row_sep)
        running.feed(line)
        if running.tokens() > b_tokens:
            last_err = PlanFailure(
                ERR_ROW_WITH_HEADER,
                "single data row with header exceeds budget",
                {"row": r, "H": h_tokens, "row_payload": running.tokens(), "B": b_tokens},
            )
            row_feasible = False
            break
        cur = [r]
    if row_feasible and cur:
        parts.append(cur)
    if r_count <= 1:
        parts = []
    if row_feasible and parts:
        row_parts = []
        for group in parts:
            span = (group[0], group[-1] + 1)
            tok, nbytes = _ordered_row_payload(grid, span, (0, c_count), spec)
            row_parts.append(
                {
                    "row_span": [span[0], span[1]],
                    "col_span": [0, c_count],
                    "payload_tokens": tok,
                    "payload_bytes": nbytes,
                    "repeat_header": True,
                }
            )
        candidates.append(
            {
                "axis": AXIS_ROW,
                "parts": row_parts,
                "cuts": cut_merge_count(row_parts, merges),
            }
        )

    col_parts, col_err = _accumulate_columns(grid, b_tokens, spec, trace)
    if col_parts is not None:
        candidates.append(
            {
                "axis": AXIS_COLUMN,
                "parts": col_parts,
                "cuts": cut_merge_count(col_parts, merges),
            }
        )
    elif col_err is not None:
        last_err = col_err

    if merges:
        region_cand = _matrix_parts(grid, merges, b_tokens, spec, trace, ordered_header=True)
        if region_cand is not None:
            candidates.append(region_cand)
        else:
            last_err = PlanFailure(
                ERR_COLUMN_EXCEEDS,
                "region/mixed candidate infeasible",
                {"B": b_tokens, "axis": AXIS_MIXED},
            )

    if not candidates:
        if last_err is not None:
            raise last_err
        raise PlanFailure(ERR_COLUMN_EXCEEDS, "no feasible axis", {"B": b_tokens})
    return _pick_candidate(candidates, trace)


def _accumulate_columns(grid, b_tokens, spec, trace):
    r_count = len(grid)
    c_count = max((len(r) for r in grid), default=0)
    if c_count == 0:
        return [], None
    groups = []
    start = 0
    while start < c_count:
        end = start
        last_ok = None
        while end < c_count:
            trial_end = end + 1
            tok, nbytes = _payload(grid, (0, r_count), (start, trial_end), spec)
            ok = tok <= b_tokens
            trace.add(
                "fit_block",
                tok,
                b_tokens,
                "accept" if ok else "reject",
                {"axis": AXIS_COLUMN, "col_span": [start, trial_end]},
            )
            if ok:
                last_ok = (trial_end, tok, nbytes)
                end = trial_end
            else:
                break
        if last_ok is None:
            tok, _nbytes = _payload(grid, (0, r_count), (start, start + 1), spec)
            err = PlanFailure(
                ERR_COLUMN_EXCEEDS,
                "single column exceeds budget",
                {"col": start, "payload": tok, "B": b_tokens},
            )
            return None, err
        groups.append(
            {
                "row_span": [0, r_count],
                "col_span": [start, last_ok[0]],
                "payload_tokens": last_ok[1],
                "payload_bytes": last_ok[2],
                "repeat_header": False,
            }
        )
        start = last_ok[0]
    return groups, None


def _accumulate_rect_rows(grid, rect, b_tokens, spec, trace, with_header):
    r0, c0, r1, c1 = rect
    parts = []
    # 数据起点：带表头时跳过源表第 0 行（若矩形包含它，表头另附）
    data_r0 = r0
    if with_header and r0 == 0:
        data_r0 = min(r1, 1)
    cur_start = None
    running = None
    header_base = None
    if with_header and grid:
        header_base = TokenAccum()
        header_base.feed(spec.cell_sep.join(_rect_rows(grid, (0, 1), (c0, c1))[0]))

    def _start_row(r):
        acc = header_base.copy() if header_base is not None else TokenAccum()
        line = spec.cell_sep.join(_rect_rows(grid, (r, r + 1), (c0, c1))[0])
        if header_base is not None:
            acc.feed(spec.row_sep)
        acc.feed(line)
        return acc

    scan_from = data_r0 if with_header else r0
    if scan_from >= r1:
        tok, nbytes = _payload(grid, (r0, r1), (c0, c1), spec)
        return [
            {
                "row_span": [r0, r1],
                "col_span": [c0, c1],
                "payload_tokens": tok,
                "payload_bytes": nbytes,
                "repeat_header": False,
            }
        ]
    for r in range(scan_from, r1):
        if cur_start is None:
            running = _start_row(r)
            if running.tokens() > b_tokens:
                return None
            cur_start = r
            trace.add("fit_block", running.tokens(), b_tokens, "accept", {"axis": AXIS_ROW, "row": r})
            continue
        trial = running.copy()
        trial.feed(spec.row_sep)
        trial.feed(spec.cell_sep.join(_rect_rows(grid, (r, r + 1), (c0, c1))[0]))
        ok = trial.tokens() <= b_tokens
        trace.add(
            "fit_block",
            trial.tokens(),
            b_tokens,
            "accept" if ok else "reject",
            {"axis": AXIS_ROW, "row": r, "rect": [r0, c0, r1, c1]},
        )
        if ok:
            running = trial
        else:
            tok = running.tokens()
            # 字节按最终序列化重算，避免增量与表头重复不一致
            parts.append((cur_start, r, tok))
            running = _start_row(r)
            if running.tokens() > b_tokens:
                return None
            cur_start = r
    if cur_start is not None:
        parts.append((cur_start, r1, running.tokens()))
    out = []
    for s, e, _tok in parts:
        if with_header:
            tok, nbytes = _ordered_row_payload(grid, (s, e), (c0, c1), spec)
            span = [s, e]
        else:
            tok, nbytes = _payload(grid, (s, e), (c0, c1), spec)
            span = [s, e]
        out.append(
            {
                "row_span": span,
                "col_span": [c0, c1],
                "payload_tokens": tok,
                "payload_bytes": nbytes,
                "repeat_header": with_header,
            }
        )
    return out


def _accumulate_rect_cols(grid, rect, b_tokens, spec, trace):
    r0, c0, r1, c1 = rect
    parts = []
    start = c0
    while start < c1:
        end = start
        last = None
        while end < c1:
            trial_end = end + 1
            tok, nbytes = _payload(grid, (r0, r1), (start, trial_end), spec)
            ok = tok <= b_tokens
            trace.add(
                "fit_region",
                tok,
                b_tokens,
                "accept" if ok else "reject",
                {"axis": AXIS_COLUMN, "col_span": [start, trial_end]},
            )
            if ok:
                last = (trial_end, tok, nbytes)
                end = trial_end
            else:
                break
        if last is None:
            return None
        parts.append(
            {
                "row_span": [r0, r1],
                "col_span": [start, last[0]],
                "payload_tokens": last[1],
                "payload_bytes": last[2],
                "repeat_header": False,
            }
        )
        start = last[0]
    return parts


def _matrix_parts(grid, merges, b_tokens, spec, trace, ordered_header):
    r_count = len(grid)
    c_count = max((len(r) for r in grid), default=0)
    full_tok, full_b = _payload(grid, (0, r_count), (0, c_count), spec)
    if full_tok <= b_tokens and not ordered_header:
        return {
            "axis": AXIS_NONE,
            "parts": [
                {
                    "row_span": [0, r_count],
                    "col_span": [0, c_count],
                    "payload_tokens": full_tok,
                    "payload_bytes": full_b,
                    "repeat_header": False,
                }
            ],
            "cuts": 0,
        }
    regions = geometry_regions(grid, merges)
    kept = []
    overflow = []
    for rect in regions:
        tok, nbytes = _payload(grid, (rect[0], rect[2]), (rect[1], rect[3]), spec)
        ok = tok <= b_tokens
        trace.add(
            "fit_region",
            tok,
            b_tokens,
            "accept" if ok else "split",
            {"rect": list(rect)},
        )
        if ok:
            kept.append(
                {
                    "row_span": [rect[0], rect[2]],
                    "col_span": [rect[1], rect[3]],
                    "payload_tokens": tok,
                    "payload_bytes": nbytes,
                    "repeat_header": False,
                }
            )
        else:
            overflow.append(rect)
    if not overflow:
        axis = AXIS_REGION
        return {"axis": axis, "parts": kept, "cuts": cut_merge_count(kept, merges)}
    mixed = list(kept)
    for rect in overflow:
        sub_row = _accumulate_rect_rows(grid, rect, b_tokens, spec, trace, with_header=ordered_header)
        sub_col = _accumulate_rect_cols(grid, rect, b_tokens, spec, trace)
        options = []
        if sub_row:
            options.append(sub_row)
        if sub_col:
            options.append(sub_col)
        if not options:
            return None
        options.sort(key=lambda parts: (len(parts), cut_merge_count(parts, merges)))
        mixed.extend(options[0])
    return {"axis": AXIS_MIXED, "parts": mixed, "cuts": cut_merge_count(mixed, merges)}


def _pick_candidate(candidates, trace):
    def key(cand):
        return (len(cand["parts"]), cand["cuts"], AXIS_ORDER[cand["axis"]])

    winner = min(candidates, key=key)
    for cand in candidates:
        chosen = cand is winner
        trace.add(
            "choose_axis",
            len(cand["parts"]),
            cand["cuts"],
            "accept" if chosen else "reject",
            {"axis": cand["axis"]},
        )
    return winner


def _normalize_grid(table):
    grid = [list(r) for r in (table.get("grid") or [])]
    if not grid and table.get("rows"):
        grid = [list(r) for r in table["rows"]]
    c_count = 0
    for row in grid:
        c_count = max(c_count, len(row))
    cols = table.get("columns") or []
    c_count = max(c_count, len(cols))
    grid = [row + [""] * (c_count - len(row)) for row in grid]
    merges = []
    for item in table.get("merges") or []:
        if isinstance(item, dict):
            merges.append((item["r0"], item["c0"], item["r1"], item["c1"]))
        else:
            merges.append(tuple(item[:4]))
    return grid, merges, c_count


def _assemble_context(block, parents):
    merges_out = []
    fills_out = []
    comments_out = []
    for parent, span in parents:
        rect_r = span["row_span"]
        rect_c = span["col_span"]
        # 重复表头的行块：上下文矩形含第 0 行，锚点才看得见表头侧合并
        r0 = 0 if span.get("repeat_header") else rect_r[0]
        r1 = rect_r[1]
        c0, c1 = rect_c
        for raw in parent.get("merges") or []:
            if isinstance(raw, dict):
                mr0, mc0, mr1, mc1 = raw["r0"], raw["c0"], raw["r1"], raw["c1"]
            else:
                mr0, mc0, mr1, mc1 = raw[0], raw[1], raw[2], raw[3]
            if mr0 < r1 and mr1 > r0 and mc0 < c1 and mc1 > c0:
                merges_out.append(
                    {
                        "table_id": parent["table_id"],
                        "r0": mr0,
                        "c0": mc0,
                        "r1": mr1,
                        "c1": mc1,
                    }
                )
        for fill in parent.get("fills") or []:
            if r0 <= fill["r"] < r1 and c0 <= fill["c"] < c1:
                fills_out.append(
                    {
                        "table_id": parent["table_id"],
                        "r": fill["r"],
                        "c": fill["c"],
                        "value": fill.get("value"),
                    }
                )
        for comment in parent.get("comments") or []:
            if r0 <= comment["r"] < r1 and c0 <= comment["c"] < c1:
                comments_out.append(
                    {
                        "table_id": parent["table_id"],
                        "r": comment["r"],
                        "c": comment["c"],
                        "text": comment.get("text"),
                    }
                )
    block["context"] = {"merges": merges_out, "fills": fills_out, "comments": comments_out}


def _column_locator(table, index):
    cols = table.get("columns") or []
    if index < len(cols) and isinstance(cols[index], dict) and cols[index].get("locator"):
        return str(cols[index]["locator"])
    return str(index)


def _attach_neighbors(blocks, tables_by_id, memory_budget_bytes):
    """实施方案：精确哈希；装不进内存预算的列整列跳过，不抽样。

    重叠度 = |交集| / min(|A|, |B|)，两边都是该块未跳过列里的非空取值集合。
    重叠度 > 0 才写入 neighbor_ids。不参与选轴。
    """
    skipped = []
    value_sets = []
    used = 0
    budget = memory_budget_bytes
    for block in blocks:
        values = set()
        for table_id in block["parent_table_ids"]:
            table = tables_by_id[table_id]
            grid, _merges, c_count = _normalize_grid(table)
            span = _span_for(block, table_id)
            r0, r1 = span["row_span"]
            if span.get("repeat_header"):
                r0 = 0
            c0, c1 = span["col_span"]
            for c in range(c0, c1):
                col_vals = []
                seen = set()
                for r in range(r0, r1):
                    cell = grid[r][c] if r < len(grid) and c < len(grid[r]) else ""
                    if cell and cell not in seen:
                        seen.add(cell)
                        col_vals.append(cell)
                nbytes = sum(len(v.encode("utf-8")) for v in col_vals)
                locator = _column_locator(table, c)
                key = {"table_id": table_id, "locator": locator}
                if budget is not None and used + nbytes > budget:
                    skipped.append(key)
                    continue
                used += nbytes
                values.update(col_vals)
        value_sets.append(values)
    for i, block in enumerate(blocks):
        neighbors = []
        for j, other in enumerate(blocks):
            if i == j:
                continue
            a = value_sets[i]
            b = value_sets[j]
            if not a or not b:
                continue
            inter = len(a & b)
            denom = min(len(a), len(b))
            if denom and inter / denom > 0:
                neighbors.append(other["block_id"])
        block["neighbor_ids"] = neighbors
    return skipped


def _span_for(block, table_id):
    for span in block["parent_spans"]:
        if span["table_id"] == table_id:
            return span
    return block["parent_spans"][0]


def _layout_of(layouts):
    if any(item == LAYOUT_MATRIX for item in layouts):
        return LAYOUT_MATRIX
    return LAYOUT_ORDERED


def plan_blocks(block_plan_input, bounds: LayoutBounds = None, spec: SerializeSpec = None):
    bounds = bounds or LayoutBounds()
    spec = spec or SerializeSpec()
    trace = _Trace()
    runtime = (block_plan_input or {}).get("runtime") or {}
    try:
        b_tokens, b_details = compute_budget(runtime)
    except PlanFailure as exc:
        return {"ok": False, "error": exc.code, "message": exc.message, "details": exc.details, "pack_trace": []}

    tables = list((block_plan_input or {}).get("tables") or [])
    prepared = []
    for index, table in enumerate(tables):
        grid, merges, _c = _normalize_grid(table)
        layout, metrics = classify_layout(grid, merges, bounds)
        table = dict(table)
        table["layout"] = layout
        table["_grid"] = grid
        table["_merges"] = merges
        if "table_id" not in table:
            table["table_id"] = str(index)
        tok, nbytes = _payload(grid, (0, len(grid)), (0, _c), spec) if grid else (0, 0)
        trace.add(
            "classify_layout",
            0,
            0,
            "classified",
            {
                "table_id": table["table_id"],
                "layout": layout,
                "row0_density": metrics["row0_density"],
                "top_sparse_band": metrics["top_sparse_band"],
                "merge_top_ratio": metrics["merge_top_ratio"],
                "avg_len_row0": metrics["avg_len_row0"],
                "avg_len_row1": metrics["avg_len_row1"],
                "looks_like_field_header": metrics["looks_like_field_header"],
            },
        )
        prepared.append({"table": table, "tokens": tok, "bytes": nbytes, "index": index})

    if not prepared:
        trace.add("fit_table", 0, b_tokens, "accept", {"tables": 0})
        plan = _finish(b_tokens, 0, [], trace, runtime, [])
        return {"ok": True, "block_plan": plan}

    try:
        blocks = _pack(prepared, b_tokens, spec, trace)
    except PlanFailure as exc:
        return {
            "ok": False,
            "error": exc.code,
            "message": exc.message,
            "details": exc.details,
            "pack_trace": trace.rows,
        }

    by_id = {item["table"]["table_id"]: item["table"] for item in prepared}
    for block in blocks:
        parents = []
        for span in block["parent_spans"]:
            parents.append((by_id[span["table_id"]], span))
        _assemble_context(block, parents)

    memory_budget = runtime.get("memory_budget_bytes")
    if memory_budget is not None:
        memory_budget = int(memory_budget)
    skipped = _attach_neighbors(blocks, by_id, memory_budget)

    names = []
    for item in prepared:
        for col in item["table"].get("columns") or []:
            if isinstance(col, dict):
                names.append(str(col.get("name") or ""))
            else:
                names.append(str(col))
    schema, _ = fit_column_directory(names, b_tokens, trace, spec)

    total = sum(item["tokens"] for item in prepared)
    plan = _finish(b_tokens, total, blocks, trace, runtime, skipped)
    plan["schema_directory"] = schema
    return {"ok": True, "block_plan": plan}


def _pack(prepared, b_tokens, spec, trace):
    order = sorted(prepared, key=lambda item: (-item["tokens"], item["index"]))
    blocks = []
    open_members = []
    remaining = b_tokens

    def seal():
        nonlocal open_members, remaining
        if not open_members:
            return
        layouts = [m["table"]["layout"] for m in open_members]
        spans = []
        tokens = 0
        nbytes = 0
        ids = []
        for member in open_members:
            grid = member["table"]["_grid"]
            r_count = len(grid)
            c_count = max((len(r) for r in grid), default=0)
            spans.append(
                {
                    "table_id": member["table"]["table_id"],
                    "row_span": [0, r_count],
                    "col_span": [0, c_count],
                    "repeat_header": False,
                }
            )
            tokens += member["tokens"]
            nbytes += member["bytes"]
            ids.append(member["table"]["table_id"])
        blocks.append(_make_block(ids, AXIS_NONE, _layout_of(layouts), spans, tokens, nbytes))
        open_members = []

    for member in order:
        t = member["tokens"]
        # 当前 open 块尚未封存；装得下就继续装（§4.1）
        can = t <= remaining and not open_members
        outcome = "accept" if can else ("split" if t > b_tokens else "seal_block")
        trace.add(
            "fit_table",
            t,
            remaining,
            outcome,
            {"table_id": member["table"]["table_id"]},
        )
        if can:
            open_members.append(member)
            remaining -= t
            continue
        if t <= b_tokens:
            seal()
            open_members = [member]
            remaining = b_tokens - t
            continue
        seal()
        parts = _split_one(member, b_tokens, spec, trace)
        for part in parts:
            span = {
                "table_id": member["table"]["table_id"],
                "row_span": part["row_span"],
                "col_span": part["col_span"],
                "repeat_header": part.get("repeat_header", False),
            }
            blocks.append(
                _make_block(
                    [member["table"]["table_id"]],
                    part["axis"],
                    member["table"]["layout"],
                    [span],
                    part["payload_tokens"],
                    part["payload_bytes"],
                )
            )
        remaining = b_tokens
    seal()
    for i, block in enumerate(blocks):
        block["block_id"] = i
    return blocks


def _split_one(member, b_tokens, spec, trace):
    table = member["table"]
    grid = table["_grid"]
    merges = table["_merges"]
    if table["layout"] == LAYOUT_MATRIX:
        cand = _matrix_parts(grid, merges, b_tokens, spec, trace, ordered_header=False)
        if cand is None:
            raise PlanFailure(ERR_COLUMN_EXCEEDS, "matrix region cannot fit", {"B": b_tokens})
        for part in cand["parts"]:
            part["axis"] = cand["axis"]
        return cand["parts"]
    cand = _accumulate_ordered_rows(grid, b_tokens, spec, trace, merges)
    for part in cand["parts"]:
        part["axis"] = cand["axis"]
    return cand["parts"]


def _make_block(ids, axis, layout, spans, tokens, nbytes):
    primary = spans[0]
    return {
        "block_id": None,
        "parent_table_ids": list(ids),
        "axis": axis,
        "layout": layout,
        "row_span": list(primary["row_span"]),
        "col_span": list(primary["col_span"]),
        "payload_tokens": tokens,
        "payload_bytes": nbytes,
        "parent_spans": spans,
        "context": {"merges": [], "fills": [], "comments": []},
        "neighbor_ids": [],
    }


def _finish(b_tokens, total, blocks, trace, runtime, skipped):
    return {
        "budget": {
            "input_tokens": b_tokens,
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "payload_token_total": total,
        },
        "blocks": blocks,
        "pack_trace": trace.rows,
        "overlap_skipped_columns": skipped,
    }


def safety_boundary_inventory():
    bounds = LayoutBounds()
    spec = SerializeSpec()
    return [
        {"item": "axis none/row/column/region/mixed", "class": "安全边界", "where": "policy.AXIS_*"},
        {"item": "layout ordered/matrix", "class": "安全边界", "where": "policy.LAYOUT_*"},
        {"item": f"sparse_row_density={bounds.sparse_row_density}", "class": "安全边界", "where": "LayoutBounds"},
        {"item": f"field_header_min_density={bounds.field_header_min_density}", "class": "安全边界", "where": "LayoutBounds"},
        {"item": f"dense_header_density={bounds.dense_header_density}", "class": "安全边界", "where": "LayoutBounds"},
        {"item": f"label_vs_data_len_ratio={bounds.label_vs_data_len_ratio}", "class": "安全边界", "where": "LayoutBounds"},
        {"item": f"max_top_sparse_band={bounds.max_top_sparse_band}", "class": "安全边界", "where": "LayoutBounds"},
        {
            "item": f"merge_top_ratio_exclusive_max={bounds.merge_top_ratio_exclusive_max}",
            "class": "安全边界",
            "where": "LayoutBounds",
        },
        {"item": "serialize cell/row separators", "class": "安全边界", "where": "SerializeSpec，对齐 §3.4 制表符/换行"},
        {"item": "estimate_tokens CJK=1 其余//4 +1", "class": "安全边界", "where": "ai.context_budget"},
        {"item": "B_raw<=0 失败，不读 min_input_tokens", "class": "安全边界", "where": "compute_budget"},
        {"item": "H>B → header_exceeds_budget", "class": "安全边界", "where": "_accumulate_ordered_rows"},
        {"item": "错误码四枚", "class": "安全边界", "where": "policy.ERR_*"},
        {"item": "列档 [8,4,2,1]", "class": "禁止项已删", "where": "fit_column_directory 逐列"},
        {"item": "按表名/目录名分支", "class": "禁止项已删", "where": "无路径分支"},
        {"item": "运行时推翻 layout", "class": "禁止项已删", "where": "只写 classify_layout 一次"},
        {
            "item": "单列超预算时丢掉 column 候选，仅当无其它可行轴才 column_exceeds_budget",
            "class": "安全边界",
            "where": "否则行轴可行的表会被列轴误杀；§9.B 漏扫/任务存在单列>B",
        },
    ]
