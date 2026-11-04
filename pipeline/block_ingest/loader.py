"""读入：Excel（复用 gridio）+ SQLite 单表 → 与 1a 同构的 tables[]。

不写外部库驱动、连接池、权限、自动发现。切分交给 1a。
"""

from __future__ import annotations

import copy
import datetime as _dt
import sqlite3
from pathlib import Path

from ai.context_budget import estimate_tokens
from pipeline.block_plan.gridio import load_tables as _gridio_load_tables
from pipeline.block_plan.policy import SerializeSpec
from pipeline.block_plan.tokens import measure_rows, serialize_rows


class IngestError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def cell_stringify(value):
    """6a §2.4：与 Excel None→'' + str.strip 对齐。"""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace").strip()
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return str(value).strip()
    return str(value).strip()


def measure_table_sizes(rows, spec: SerializeSpec = None):
    """byte_size = 单元格 UTF-8 之和（不含分隔符）；token_size = 序列化 blob 的 estimate_tokens。"""
    spec = spec or SerializeSpec()
    grid = rows or []
    byte_size = 0
    for row in grid:
        for cell in row or []:
            text = "" if cell is None else str(cell)
            byte_size += len(text.encode("utf-8"))
    blob = serialize_rows(
        [[("" if c is None else str(c)) for c in (row or [])] for row in grid],
        spec.cell_sep,
        spec.row_sep,
    )
    token_size = estimate_tokens(blob)
    # 与 measure_rows 同源校验（空表时均为 0）
    _ = measure_rows(grid, spec.cell_sep, spec.row_sep)
    return byte_size, token_size


def _normalize_empty_shape(grid):
    """仅表头无列 / 空表：写死 0 列 + 1 行空 header（与 1a 空 Excel 对齐，不报错）。"""
    if not grid:
        return [[]], []
    width = max((len(r) for r in grid), default=0)
    if width == 0:
        return [[]], []
    rows = []
    for row in grid:
        cells = list(row or [])
        if len(cells) < width:
            cells.extend([""] * (width - len(cells)))
        rows.append([cell_stringify(c) for c in cells[:width]])
    columns = [{"locator": str(c), "name": rows[0][c]} for c in range(width)]
    return rows, columns


def _annotate_table(table, source_kind, table_id=None):
    rows = table.get("rows") or table.get("grid") or []
    rows, columns = _normalize_empty_shape(rows)
    byte_size, token_size = measure_table_sizes(rows)
    out = {
        "table_id": table_id if table_id is not None else table.get("table_id") or "t0",
        "columns": columns,
        "rows": rows,
        "grid": rows,
        "byte_size": byte_size,
        "token_size": token_size,
        "merges": list(table.get("merges") or []),
        "fills": list(table.get("fills") or []),
        "comments": list(table.get("comments") or []),
        "source_kind": source_kind,
    }
    if table.get("sheet") is not None:
        out["sheet"] = table.get("sheet")
    return out


def load_excel(path, sheet_name=None):
    """只读用户指定文件（及可选 sheet）。不扫同目录。"""
    path = Path(path)
    if not path.exists() or not path.is_file():
        raise IngestError(
            "ingest_path_not_found",
            "Excel 路径不存在: %s" % path,
            {"path": str(path)},
        )
    try:
        raw = _gridio_load_tables(path, sheet_name=sheet_name)
    except ValueError as exc:
        msg = str(exc)
        if "sheet not found" in msg.lower() or "sheet" in msg.lower():
            raise IngestError(
                "ingest_sheet_not_found",
                "sheet 不存在: %s" % sheet_name,
                {"path": str(path), "sheet_name": sheet_name},
            ) from exc
        raise IngestError("ingest_query_failed", msg, {"path": str(path)}) from exc
    except Exception as exc:
        raise IngestError(
            "ingest_query_failed",
            "Excel 读入失败: %s" % exc,
            {"path": str(path)},
        ) from exc

    if sheet_name is not None and not raw:
        raise IngestError(
            "ingest_sheet_not_found",
            "sheet 不存在: %s" % sheet_name,
            {"path": str(path), "sheet_name": sheet_name},
        )

    tables = []
    for i, table in enumerate(raw):
        tables.append(_annotate_table(table, "excel", table_id="t%s" % i))
    if not tables:
        # 空文件：仍给一张 0 列 + 1 行空 header
        tables.append(
            _annotate_table(
                {
                    "table_id": "t0",
                    "rows": [[]],
                    "grid": [[]],
                    "merges": [],
                    "fills": [],
                    "comments": [],
                },
                "excel",
                table_id="t0",
            )
        )
    return tables


def _quote_ident(name):
    return '"' + str(name).replace('"', '""') + '"'


def load_sqlite_one_table(path, table_name):
    """只读用户指定的一张表。不列库内其它表。连接用完即关。"""
    path = Path(path)
    if not path.exists() or not path.is_file():
        raise IngestError(
            "ingest_path_not_found",
            "SQLite 路径不存在: %s" % path,
            {"path": str(path)},
        )
    name = "" if table_name is None else str(table_name).strip()
    if not name:
        raise IngestError(
            "ingest_table_not_found",
            "表名未指定",
            {"path": str(path), "table_name": table_name},
        )

    conn = None
    try:
        conn = sqlite3.connect(str(path))
        try:
            cur = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=? LIMIT 1",
                (name,),
            )
            if cur.fetchone() is None:
                raise IngestError(
                    "ingest_table_not_found",
                    "表不存在: %s" % name,
                    {"path": str(path), "table_name": name},
                )
            # PRAGMA 仅针对已指定表名（不是列库选表）
            info = list(conn.execute("PRAGMA table_info(%s)" % _quote_ident(name)))
            col_names = [row[1] for row in info]
            # 0 列表：仍产出 0 列 + 1 行空 header
            if not col_names:
                grid = [[]]
            else:
                rows = conn.execute("SELECT * FROM %s" % _quote_ident(name)).fetchall()
                header = [cell_stringify(c) for c in col_names]
                body = [[cell_stringify(cell) for cell in row] for row in rows]
                grid = [header] + body
        finally:
            # 游标随语句结束；连接在外层关闭
            pass
    except IngestError:
        raise
    except sqlite3.Error as exc:
        raise IngestError(
            "ingest_query_failed",
            "SQLite 查询失败: %s" % exc,
            {"path": str(path), "table_name": name},
        ) from exc
    finally:
        if conn is not None:
            conn.close()

    table = _annotate_table(
        {
            "table_id": "t0",
            "sheet": name,
            "rows": grid,
            "grid": grid,
            "merges": [],
            "fills": [],
            "comments": [],
        },
        "sqlite",
        table_id="t0",
    )
    # 测量事实：库无几何
    table["merges"] = []
    table["fills"] = []
    table["comments"] = []
    return [table]


def ingest(ingest_input):
    """入口：excel | sqlite | session。不猜不扫，不 fallback。"""
    data = ingest_input or {}
    kind = data.get("source_kind")
    if kind == "session":
        session = data.get("session") or {}
        tables = copy.deepcopy(session.get("tables") or [])
        out = []
        for i, table in enumerate(tables):
            annotated = _annotate_table(table, table.get("source_kind") or "session", table_id=table.get("table_id") or ("t%s" % i))
            out.append(annotated)
        return {"tables": out}

    if kind == "excel":
        excel = data.get("excel") or {}
        path = excel.get("path")
        if not path:
            raise IngestError("ingest_path_not_found", "未指定 Excel 路径", {})
        sheet = excel.get("sheet_name")
        tables = load_excel(path, sheet_name=sheet)
        return {"tables": tables}

    if kind == "sqlite":
        sqlite = data.get("sqlite") or {}
        path = sqlite.get("path")
        table_name = sqlite.get("table_name")
        if not path:
            raise IngestError("ingest_path_not_found", "未指定 SQLite 路径", {})
        tables = load_sqlite_one_table(path, table_name)
        return {"tables": tables}

    raise IngestError(
        "ingest_unsupported_kind",
        "不支持的 source_kind: %s（外部库本批不做）" % kind,
        {"source_kind": kind},
    )
