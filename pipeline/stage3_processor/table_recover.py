# pipeline/stage3_processor/table_recover.py
"""
表格结构恢复（P2）：识别不出表头时的兜底闭环。

原则（AI 出方案、本地机械校验）：
- 把 grid 摘要 + 用户补充说明喂给模型；
- 模型二选一：
  a) status=recovered：给出 header_row / columns → 本地机械重建表；
  b) status=needs_user：给出五段式（推测/依据/可能问题/下一步/请确认或纠正）→ 交给 UI 求证；
- 校验失败 / 无 Key / 网络失败 / 模型乱回 → 返回 unavailable，调用方保留原样并提示，不抛异常。

五段字段与 block_cross_table.query_engine.FIVE_PART_FIELD_KEYS 同名，便于上层统一渲染。
本模块只依赖标准库；ai.client 在 LLM 分支里懒加载，避免测试环境硬依赖 requests。
"""

from __future__ import annotations

import json

from pipeline.stage3_processor import table_normalizer

FIVE_PART_FIELD_KEYS = (
    "speculation",
    "evidence",
    "what_might_be_wrong",
    "next_step",
    "please_confirm_or_correct",
)


def _param_int(key: str, default: int) -> int:
    try:
        import config
        return int(config.get_param(key, default))
    except Exception:
        return default


def _enabled() -> bool:
    try:
        import config
        v = config.get_param("table_recover.llm_recover")
        return True if v is None else bool(v)
    except Exception:
        return True


def _chat_json(prompt, api_key):
    """懒加载 ai.client；失败返回 None。"""
    try:
        from ai.client import chat_json
        return chat_json(prompt, api_key, max_tokens=1200, temperature=0.1, timeout=30)
    except Exception:
        return None


# ------------------------------------------------------------------
# 网格摘要 / 提示词
# ------------------------------------------------------------------

def _summary(grid: list, max_rows: int) -> str:
    lines = []
    for i, row in enumerate(grid[:max_rows]):
        cells = [f"{c}:{str(v)[:20]}" for c, v in enumerate(row)]
        lines.append(f"r{i} | " + " | ".join(cells))
    return "\n".join(lines)


def _prompt(summary: str, nrows: int, width: int, user_hint: str) -> str:
    hint = ""
    if user_hint:
        hint = f"用户补充说明（可能有用，也可能没用）：{str(user_hint)[:200]}\n"
    return (
        "你是表格结构恢复器。下面是一张自动识别失败的表格原始网格"
        "（行号 r0、r1… 从 0 开始，列号 c0、c1… 从 0 开始）。\n"
        f"{hint}"
        "请判断：\n"
        "1) 这张表有没有表头行？若有，表头在第几行？各列的列名是什么？\n"
        "2) 若无法可靠判断，返回 needs_user 五段式求证，不要瞎猜。\n\n"
        "只输出一个 JSON 对象：\n"
        '· 能恢复：{"status":"recovered","header_row":<整数>,"columns":["列名1","列名2",...]}\n'
        f"  —— columns 必须恰好 {width} 个且都非空；header_row 必须在 0..{nrows - 1} 之间。\n"
        '· 不能恢复：{"status":"needs_user","speculation":"","evidence":"",'
        '"what_might_be_wrong":"","next_step":"","please_confirm_or_correct":""}\n'
        "  —— 五段必须全部非空中文。\n\n"
        f"网格（前 {min(nrows, 8)} 行，共 {nrows} 行 × {width} 列）：\n{summary}"
    )


# ------------------------------------------------------------------
# 校验
# ------------------------------------------------------------------

def _validate_recovered(raw: dict, nrows: int, width: int):
    """校验 recovered 载荷。返回 (ok, reason)。"""
    hr = raw.get("header_row")
    cols = raw.get("columns")
    if not isinstance(hr, int) or isinstance(hr, bool) or not (0 <= hr < nrows):
        return False, "bad_header_row"
    if not isinstance(cols, list) or len(cols) != width:
        return False, "bad_columns_count"
    for c in cols:
        if not str(c or "").strip():
            return False, "empty_column"
    return True, ""


def _validate_five_part(raw: dict):
    """校验五段式。返回 dict（全非空）或 None。"""
    five = {}
    for k in FIVE_PART_FIELD_KEYS:
        v = str(raw.get(k) or "").strip()
        if not v:
            return None
        five[k] = v
    return five


# ------------------------------------------------------------------
# 主入口
# ------------------------------------------------------------------

def recover(table: dict, api_key=None, user_hint: str = "",
            max_rows: int | None = None) -> dict:
    """
    尝试恢复表结构。返回：
      {"status":"recovered", "table": <兼容契约的干净表>}
      {"status":"needs_user", "five_part": {...五段...}}
      {"status":"unavailable", "reason": "..."}
    """
    if not api_key:
        return {"status": "unavailable", "reason": "no_api_key"}
    if not _enabled():
        return {"status": "unavailable", "reason": "disabled"}
    if max_rows is None:
        max_rows = _param_int("table_recover.summary_rows", 8)

    grid = table_normalizer.grid_of(table)
    if not grid or not grid[0]:
        return {"status": "unavailable", "reason": "empty_grid"}
    nrows = len(grid)
    width = max((len(r) for r in grid), default=0)
    if nrows < 2 or width < 2:
        return {"status": "unavailable", "reason": "too_small"}

    summary = _summary(grid, max_rows)
    raw = _chat_json(_prompt(summary, nrows, width, user_hint), api_key)
    if not isinstance(raw, dict):
        return {"status": "unavailable", "reason": "no_json"}

    status = str(raw.get("status") or "").strip()
    if status == "recovered":
        ok, reason = _validate_recovered(raw, nrows, width)
        if not ok:
            return {"status": "unavailable", "reason": reason}
        out = table_normalizer.rebuild_from_header(
            table, int(raw["header_row"]), [str(c).strip() for c in raw["columns"]])
        out["header_row"] = int(raw["header_row"])
        return {"status": "recovered", "table": out}
    if status == "needs_user":
        five = _validate_five_part(raw)
        if not five:
            return {"status": "unavailable", "reason": "five_part_incomplete"}
        return {"status": "needs_user", "five_part": five}
    return {"status": "unavailable", "reason": "bad_status"}
