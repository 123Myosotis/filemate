# pipeline/stage3_processor/table_normalizer.py
"""
表格规范化 / 二次制表（P0）。

把 file_parser 产出的 table（普通 {columns, rows}，或带 grid/merges/blocks 的复杂表）
规范成干净、可落库 / 可查询的表。纯标准库 + 复用 file_parser._flatten_crosstab，
无 LLM、无业务词表——只做「识别形状 → 清洗 → 二次制表」，语义问题留给 P1/P2。

shape 取值：
  tidy         首行即表头的普通明细表
  wide_matrix  多级横表头 / 交叉表 → 展开为长表（二次制表）
  multi_header 两级表头 → 拍平成复合列名（如「变电站·负责人」）
  headerless   识别不出表头 → 保留原样并标记 recognized=False（P2 接 LLM/五段求证）
  empty        无可用数据

返回 dict 兼容原表契约 {name, columns, rows}，并附带
  shape / recognized / cleaning_log / source_row_count / source_col_count。
这些附加键不会被落库（session_state 与 db_writer 都只取 name/columns/rows）。
"""

from __future__ import annotations

import re


def _param_int(key: str, default: int) -> int:
    """读配置参数，读不到回退默认；不依赖 config（模块可独立测试）。"""
    try:
        import config
        return int(config.get_param(key, default))
    except Exception:
        return default


# ------------------------------------------------------------------
# 基础网格工具
# ------------------------------------------------------------------

def _rect(grid) -> list:
    """把参差不齐的矩阵补成矩形（右补空串）。"""
    if not grid:
        return []
    width = max((len(r or []) for r in grid), default=0)
    return [list(r or []) + [""] * (width - len(r or [])) for r in grid]


def _nonempty(v) -> str:
    return str(v or "").strip()


def _nonempty_count(row) -> int:
    return sum(1 for v in row if _nonempty(v))


def _distinct_nonempty(row) -> int:
    return len({_nonempty(v) for v in row if _nonempty(v)})


def _is_numeric(v) -> bool:
    """尽力判断单元格是否是数值（含千分位/百分号）。"""
    s = _nonempty(v)
    if not s:
        return False
    s2 = s.replace(",", "")
    if s2.endswith("%"):
        s2 = s2[:-1]
    try:
        float(s2)
        return True
    except ValueError:
        return False


def _fill_merges(grid: list, merges: list) -> list:
    """合并单元格回填（向下/向右），使表头层级显式化。不修改入参。"""
    if not merges:
        return [row[:] for row in grid]
    g = [row[:] for row in grid]
    nrows = len(g)
    for m in merges:
        try:
            r0, c0, r1, c1 = (int(x) for x in m)
        except (TypeError, ValueError):
            continue
        if r0 < 0 or c0 < 0 or r0 >= nrows:
            continue
        row0 = g[r0]
        val = _nonempty(row0[c0]) if c0 < len(row0) else ""
        if not val:
            continue
        for rr in range(r0, min(r1, nrows - 1) + 1):
            row = g[rr]
            for cc in range(c0, min(c1, len(row) - 1) + 1):
                if not _nonempty(row[cc]):
                    row[cc] = val
    return g


def _remap_merges(merges: list, keep: list) -> list:
    """按 keep（保留的原始行号列表）把合并矩形的行号重映射到新网格。

    合并区域的全部行都被保留时才保留该合并；否则丢弃（回填已完成，合并信息不再需要）。
    """
    if not merges:
        return []
    old_to_new = {old: new for new, old in enumerate(keep)}
    n = max(keep, default=0) + 1
    out = []
    for m in merges:
        try:
            r0, c0, r1, c1 = (int(x) for x in m)
        except (TypeError, ValueError):
            continue
        r1 = min(r1, n - 1)
        rows = list(range(r0, r1 + 1))
        if not rows or any(rr not in old_to_new for rr in rows):
            continue
        out.append((old_to_new[r0], c0, old_to_new[r1], c1))
    return out


def _looks_headerless(header_row: list) -> bool:
    """首行不像表头：几乎全是数值（数字/年份不应当列名）。"""
    ne = [_nonempty(v) for v in header_row]
    ne = [v for v in ne if v]
    if not ne:
        return True
    numeric = sum(1 for v in ne if _is_numeric(v))
    return len(ne) >= 2 and numeric >= max(2, int(len(ne) * 0.8))


def _dedupe_columns(columns: list) -> list:
    """列名去重：空列名补「列N」，重名加 _2/_3 后缀，保持顺序。"""
    out, seen = [], set()
    for idx, raw in enumerate(columns, 1):
        c = _nonempty(raw) or f"列{idx}"
        base = c
        n = 2
        while c in seen:
            c = f"{base}_{n}"
            n += 1
        seen.add(c)
        out.append(c)
    return out


def _rows_from_grid(columns: list, grid: list) -> list:
    rows = []
    for r in grid:
        row = {}
        for i, c in enumerate(columns):
            row[c] = _nonempty(r[i]) if i < len(r) else ""
        rows.append(row)
    return rows


def _drop_empty_columns(columns: list, rows: list) -> tuple:
    """去掉整列全空的列（columns 与 rows 同步收缩）。"""
    keep = [i for i, c in enumerate(columns) if any(_nonempty(r.get(c)) for r in rows)]
    if len(keep) == len(columns):
        return columns, rows
    new_cols = [columns[i] for i in keep]
    new_rows = [{new_cols[j]: r[columns[i]] for j, i in enumerate(keep)} for r in rows]
    return new_cols, new_rows


def _clean_grid(grid, merges, max_top_bands):
    """回填合并 → 去空行 → 去标题/抬头。

    返回 (cleaned, cleaned_merges, dropped_empty, dropped_title)，供 normalize 与第二遍共用。
    """
    grid = _rect(grid)
    filled = _fill_merges(grid, merges)
    n = len(filled)
    keep = [i for i in range(n) if _nonempty_count(filled[i]) > 0]
    dropped = n - len(keep)
    n_title = 0
    while keep and n_title < max_top_bands:
        row = filled[keep[0]]
        width = len(row)
        ne = _nonempty_count(row)
        if ne == 0 or _distinct_nonempty(row) <= 1 or ne <= max(1, width // 3):
            keep.pop(0)
            n_title += 1
        else:
            break
    cleaned = [filled[i] for i in keep]
    cleaned_merges = _remap_merges(merges, keep)
    return cleaned, cleaned_merges, dropped, n_title


# ------------------------------------------------------------------
# 形状识别
# ------------------------------------------------------------------

def _hmerge_at_row(merges: list, row_idx: int) -> bool:
    """指定行是否存在横向合并（跨多列）。"""
    for m in merges:
        try:
            r0, c0, r1, c1 = (int(x) for x in m)
        except (TypeError, ValueError):
            continue
        if r0 == row_idx and r1 == row_idx and c1 > c0:
            return True
    return False


def _compound_header(grid: list, merges: list) -> tuple | None:
    """两级表头 → 复合列名。返回 (columns, data_start_row)；非多级返回 None。

    判定：首行含横向合并，或首行稀疏而次行更密，且次行非空数不少于首行。
    """
    if len(grid) < 2:
        return None
    h0 = grid[0]
    h1 = grid[1]
    width = len(h0)
    n0 = _nonempty_count(h0)
    n1 = _nonempty_count(h1)
    has_hmerge = _hmerge_at_row(merges, 0)
    h0_sparse = n0 <= max(1, width // 2) and n1 > n0
    if not (has_hmerge or h0_sparse):
        return None
    if n1 < n0:
        return None
    cols = []
    for c in range(width):
        g = _nonempty(h0[c]) if c < len(h0) else ""
        s = _nonempty(h1[c]) if c < len(h1) else ""
        if g and s and g != s:
            name = f"{g}·{s}"
        elif s:
            name = s
        elif g:
            name = g
        else:
            name = f"列{c + 1}"
        cols.append(name)
    return _dedupe_columns(cols), 2


# ------------------------------------------------------------------
# 主入口
# ------------------------------------------------------------------

def _result(name, columns, rows, shape, recognized, log, src_rows, src_cols) -> dict:
    return {
        "name": name,
        "columns": columns,
        "rows": rows,
        "shape": shape,
        "recognized": recognized,
        "cleaning_log": log,
        "source_row_count": src_rows,
        "source_col_count": src_cols,
    }


def _grid_from_table(table: dict) -> list:
    """无 grid（如 CSV / 简单 xlsx）时，用 columns + rows 反推矩阵。"""
    cols = table.get("columns") or []
    rows = table.get("rows") or []
    grid = [list(cols)]
    for r in rows:
        grid.append([r.get(c, "") for c in cols])
    return grid


def grid_of(table: dict) -> list:
    """取表的矩形网格：优先用 grid（复杂表），否则用 columns+rows 反推。"""
    grid = table.get("grid")
    if grid is not None:
        return _rect(grid)
    return _rect(_grid_from_table(table))


def rebuild_from_header(table: dict, header_row: int, columns: list) -> dict:
    """按指定表头行与列名重建表（供结构恢复使用）。返回兼容契约的表 dict。"""
    grid = grid_of(table)
    if not grid:
        return _result(table.get("name") or "", [], [], "empty", False,
                       ["空表，无法按表头重建"], 0, 0)
    header_row = max(0, min(int(header_row), len(grid) - 1))
    data = grid[header_row + 1:]
    cols = _dedupe_columns(columns)
    rows = _rows_from_grid(cols, data)
    cols, rows = _drop_empty_columns(cols, rows)
    return _result(
        table.get("name") or "", cols, rows, "recovered", True,
        [f"模型恢复表头：第 {header_row} 行，{len(cols)} 列"],
        len(grid), max((len(r) for r in grid), default=0),
    )


def normalize(table: dict, *, max_top_bands: int | None = None,
              matrix_min_rows: int | None = None) -> dict:
    """
    规范化一张表。入参为 file_parser 产出的 table dict。

    - 带 grid/merges 的复杂表（xlsx/xls 保留网格的 sheet）从原始网格重建；
    - 无 grid 的表从 columns+rows 反推。
    返回兼容 {name, columns, rows} 的 dict，并附 shape/recognized/cleaning_log。
    """
    if max_top_bands is None:
        max_top_bands = _param_int("table_normalizer.max_top_bands", 3)
    if matrix_min_rows is None:
        matrix_min_rows = _param_int("table_normalizer.matrix_min_rows", 5)

    name = table.get("name") or ""
    log: list = []
    grid = table.get("grid")
    merges = list(table.get("merges") or [])

    if grid is None:
        grid = _grid_from_table(table)
        source_rows = (len(table.get("rows") or []) or 0) + (1 if table.get("columns") else 0)
        source_cols = len(table.get("columns") or [])
    else:
        source_rows = len(grid)
        source_cols = max((len(r or []) for r in grid), default=0)

    grid = _rect(grid)
    if not grid:
        return _result(name, [], [], "empty", False, log, source_rows, source_cols)

    # 1) 矩阵 / 交叉表 → 二次制表为长表（复用 file_parser 的展开器）
    try:
        from pipeline.stage1_parser.file_parser import _flatten_crosstab
        flat = _flatten_crosstab(grid, merges, name)
    except Exception:
        flat = None
    if isinstance(flat, dict) and len(flat.get("rows") or []) >= matrix_min_rows:
        log.append(f"识别为矩阵/交叉表，二次制表为 {len(flat['rows'])} 行长表")
        return _result(
            name,
            list(flat.get("columns") or []),
            list(flat.get("rows") or []),
            "wide_matrix", True, log, source_rows, source_cols,
        )

    # 2-4) 清洗：回填合并 → 去空行 → 去标题/抬头
    cleaned, cleaned_merges, dropped, n_title = _clean_grid(grid, merges, max_top_bands)
    if dropped:
        log.append(f"去除 {dropped} 个空行")
    if n_title:
        log.append(f"去除 {n_title} 行标题/抬头")

    if not cleaned:
        return _result(name, [], [], "empty", False, log, source_rows, source_cols)

    # 5) 两级表头 → 复合列名
    compound = _compound_header(cleaned, cleaned_merges)
    if compound:
        cols, data_start = compound
        rows = _rows_from_grid(cols, cleaned[data_start:])
        cols, rows = _drop_empty_columns(cols, rows)
        log.append(f"识别为两级表头，拍平为 {len(cols)} 个复合列名")
        return _result(name, cols, rows, "multi_header", True, log, source_rows, source_cols)

    # 6) 首行表头 / 无表头
    header = cleaned[0]
    data = cleaned[1:]
    if _looks_headerless(header):
        cols = [f"列{i + 1}" for i in range(len(header))]
        rows = _rows_from_grid(cols, cleaned)  # 首行也当数据保留
        cols, rows = _drop_empty_columns(cols, rows)
        log.append("未识别出表头，按无表头保留原样（recognized=False）")
        return _result(name, cols, rows, "headerless", False, log, source_rows, source_cols)

    cols = _dedupe_columns(header)
    rows = _rows_from_grid(cols, data)
    cols, rows = _drop_empty_columns(cols, rows)
    return _result(name, cols, rows, "tidy", True, log, source_rows, source_cols)


def _find_better_header(cleaned: list):
    """首行不像表头时，尝试后续行当表头（数字带 + 文本表头）。

    返回 (idx, cols) 或 None；idx 为表头在 cleaned 中的行号（1 起）。
    只试 1..3 行，且要求候选行非「无表头」、至少 2 列、下方至少留一行数据。
    """
    for idx in range(1, min(len(cleaned), 4)):
        row = cleaned[idx]
        if not row or _looks_headerless(row):
            continue
        if len(row) < 2:
            continue
        if idx + 1 >= len(cleaned):
            continue
        cols = _dedupe_columns(row)
        if len([c for c in cols if c]) < 2:
            continue
        return idx, cols
    return None


def normalize_second_pass(table: dict, *, max_top_bands: int | None = None,
                          matrix_min_rows: int | None = None) -> dict:
    """第一遍认不出时的第二遍机械清洗（换策略，不重复第一遍）。

    - 多候选表头：首行是数字带/抬头时，试第 1/2/3 行当表头；
    - 仍认不出 → 原样返回 headerless（交给 table_recover / 五段式求证）。

    返回单张兼容表 dict，不改变返回契约。
    """
    first = normalize(table, max_top_bands=max_top_bands,
                      matrix_min_rows=matrix_min_rows)
    if first.get("recognized"):
        return first

    grid = table.get("grid")
    merges = list(table.get("merges") or [])
    if grid is None:
        grid = _grid_from_table(table)
    grid = _rect(grid)
    if not grid:
        return first
    if max_top_bands is None:
        max_top_bands = _param_int("table_normalizer.max_top_bands", 3)

    cleaned, _cm, _dropped, _title = _clean_grid(grid, merges, max_top_bands)
    cand = _find_better_header(cleaned)
    if cand is not None:
        idx, cols = cand
        rows = _rows_from_grid(cols, cleaned[idx + 1:])
        cols, rows = _drop_empty_columns(cols, rows)
        if rows and len(cols) >= 2:
            return _result(
                table.get("name") or "", cols, rows, "tidy", True,
                ["第二遍：候选表头在第 %d 行" % idx],
                len(grid), max((len(r) for r in grid), default=0),
            )
    return first


def consistency_issues(table: dict) -> list:
    """结构自洽校验（方法2）：返回可疑点字符串列表（空=自洽）。纯本地、无 LLM。

    只查明确的不变式（空列名/重复列名/占位列却标已识别/无数据行/单列），
    不猜「矩阵 vs 明细」这类歧义——那类交给复核。
    """
    issues = []
    cols = list(table.get("columns") or [])
    rows = list(table.get("rows") or [])
    if not cols:
        issues.append("无列")
        return issues
    names = [str(c) for c in cols]
    empties = [i + 1 for i, c in enumerate(names) if not c.strip()]
    if empties:
        issues.append("空列名：第 %s 列" % "、".join(map(str, empties)))
    dup = sorted({c for c in names if names.count(c) > 1})
    if dup:
        issues.append("重复列名：%s" % "、".join(dup))
    placeholder = [c for c in names if re.fullmatch(r"c\d+|列\d+", c)]
    if placeholder and table.get("recognized"):
        issues.append("有占位列名（%s）却标为已识别，疑似误判" % "、".join(placeholder))
    if not rows:
        issues.append("无数据行")
    if len(cols) < 2 and rows:
        issues.append("只有 1 列，疑似表头/转置误判")
    return issues
