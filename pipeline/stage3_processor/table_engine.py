# pipeline/stage3_processor/table_engine.py
"""
Stage3 表格引擎：纯标准库（csv 算法自实现）处理结构化表格。
- 加载：file_parser 已把 CSV/XLSX 解析成 {columns, rows:[{列:值}]}
- 画像：每列类型（number/text/empty）与非空计数，落 table_profile 缓存
- 联查：等值 join（自动找同名列），多表"订单⨝客户"类需求
- 过滤/聚合：== != > < >= <= contains / sum avg count min max
"""
from pathlib import Path


def _to_number(v):
    """尽力把单元格转数字；失败返回 None（千分位/百分号容错）"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s.endswith("%"):
        try:
            return float(s[:-1]) / 100
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _is_number(v) -> bool:
    return _to_number(v) is not None


# ------------------------------------------------------------------
# 画像
# ------------------------------------------------------------------

def profile_table(table: dict) -> dict:
    """列画像：{列名: {"type": number|text|empty, "non_empty": n}}，供缓存与联查参考"""
    profile = {}
    for col in table["columns"]:
        values = [r.get(col, "") for r in table["rows"]]
        non_empty = [v for v in values if str(v).strip()]
        if not non_empty:
            ctype = "empty"
        elif all(_is_number(v) for v in non_empty):
            ctype = "number"
        else:
            ctype = "text"
        profile[col] = {"type": ctype, "non_empty": len(non_empty)}
    return profile


# ------------------------------------------------------------------
# 联查
# ------------------------------------------------------------------

def find_join_key(t1: dict, t2: dict):
    """自动找联接键：两侧同名、且双方都有非空值的第一个列；找不到返回 None"""
    for col in t1["columns"]:
        if col in t2["columns"]:
            v1 = [str(r.get(col, "")).strip() for r in t1["rows"]]
            v2 = [str(r.get(col, "")).strip() for r in t2["rows"]]
            if any(v1) and any(v2):
                return col, col
    return None


def join_tables(t1: dict, t2: dict, key1: str, key2: str = None) -> dict:
    """
    等值 join：t1.key1 == t2.key2 的行拼接（内连接）。
    结果列 = t1 全部列 + t2 除键外全部列（重名列加 ~2 后缀）。
    """
    key2 = key2 or key1
    # 右表按键建索引：同键多行都保留
    index = {}
    for r in t2["rows"]:
        index.setdefault(str(r.get(key2, "")).strip(), []).append(r)

    # 处理右表重名列：t1 已有的列名加 ~2
    t2_cols = []
    for c in t2["columns"]:
        t2_cols.append(f"{c}~2" if (c in t1["columns"] and c != key2) else c)

    columns = list(t1["columns"]) + [c for c in t2_cols if c != key2 or key2 not in t1["columns"]]
    rows = []
    for r1 in t1["rows"]:
        k = str(r1.get(key1, "")).strip()
        for r2 in index.get(k, []):
            merged = dict(r1)
            for i, c in enumerate(t2["columns"]):
                if c == key2 and key2 in t1["columns"]:
                    continue  # 联接键与左表同名时不重复输出
                merged[t2_cols[i]] = r2.get(c, "")
            rows.append(merged)
    return {"name": f"{t1['name'] or '表1'}⨝{t2['name'] or '表2'}", "columns": columns, "rows": rows}


# ------------------------------------------------------------------
# 过滤 / 聚合
# ------------------------------------------------------------------

_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a is not None and b is not None and a > b,
    "<": lambda a, b: a is not None and b is not None and a < b,
    ">=": lambda a, b: a is not None and b is not None and a >= b,
    "<=": lambda a, b: a is not None and b is not None and a <= b,
    "contains": lambda a, b: b.lower() in str(a).lower(),
}


def filter_rows(table: dict, column: str, op: str, value) -> dict:
    """按列过滤：数值列做数值比较，否则按字符串比较；op 非法时抛 ValueError"""
    if op not in _OPS:
        raise ValueError(f"不支持的过滤操作符：{op}")
    col_values = [r.get(column, "") for r in table["rows"]]
    numeric = col_values and all(_is_number(v) for v in col_values if str(v).strip())
    target = _to_number(value) if numeric else str(value).strip()

    rows = []
    for r in table["rows"]:
        cell = r.get(column, "")
        a = _to_number(cell) if numeric else str(cell).strip()
        if _OPS[op](a, target):
            rows.append(r)
    return {"name": table["name"], "columns": table["columns"], "rows": rows}


_AGG_FUNCS = ("sum", "avg", "count", "min", "max")


def aggregate(table: dict, group_by: str, value_col: str = None, func: str = "count") -> dict:
    """
    分组聚合：
    - count 不需要 value_col，统计每组行数
    - sum/avg/min/max 对 value_col 数值列计算（非数值单元格跳过）
    返回新表 {columns: [group_by, 列], rows: [...]}，按组名字典序输出
    """
    if func not in _AGG_FUNCS:
        raise ValueError(f"不支持的聚合函数：{func}，可选 {','.join(_AGG_FUNCS)}")
    if func != "count" and not value_col:
        raise ValueError(f"{func} 需要指定数值列")

    groups = {}
    for r in table["rows"]:
        key = str(r.get(group_by, "")).strip() or "（空）"
        groups.setdefault(key, []).append(r)

    out_col = f"{func}_{value_col}" if value_col else f"count_{group_by}"
    rows = []
    for key in sorted(groups):
        grp = groups[key]
        if func == "count":
            result = len(grp)
        else:
            nums = [n for n in (_to_number(r.get(value_col, "")) for r in grp) if n is not None]
            if not nums:
                result = None  # 整组无数值 → 结果留空，不造 0
            elif func == "sum":
                result = sum(nums)
            elif func == "avg":
                result = round(sum(nums) / len(nums), 4)
            elif func == "min":
                result = min(nums)
            else:
                result = max(nums)
        rows.append({group_by: key, out_col: result})
    return {"name": f"{table['name']}·{func}", "columns": [group_by, out_col], "rows": rows}


def to_display(table: dict, limit: int = 20) -> str:
    """把表格渲染成 Markdown 文本（聊天里直接可读，最多 limit 行）"""
    cols = table["columns"]
    if not cols:
        return "（空表）"
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for r in table["rows"][:limit]:
        lines.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    if len(table["rows"]) > limit:
        lines.append(f"...（共 {len(table['rows'])} 行，仅显示前 {limit} 行）")
    return "\n".join(lines)
