"""把表格文件读成 block_plan_input.tables[]。不是桥、拼接或复核。"""

from pathlib import Path


def _blank_row(width):
    return [""] * width


def load_tables(path, sheet_name=None):
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".xls":
        sheets = _load_xls(path)
    elif ext == ".xlsx":
        sheets = _load_xlsx(path)
    else:
        raise ValueError(f"unsupported table suffix: {ext}")
    if sheet_name is not None:
        sheets = [s for s in sheets if s["sheet"] == sheet_name]
        if not sheets:
            raise ValueError(f"sheet not found: {sheet_name}")
    tables = []
    for i, sheet in enumerate(sheets):
        grid = sheet["grid"]
        columns = []
        header = grid[0] if grid else []
        for c, name in enumerate(header):
            columns.append({"locator": str(c), "name": name})
        tables.append(
            {
                "table_id": f"t{i}",
                "sheet": sheet["sheet"],
                "columns": columns,
                "rows": grid,
                "grid": grid,
                "merges": sheet["merges"],
                "fills": sheet["fills"],
                "comments": sheet["comments"],
            }
        )
    return tables


def _load_xlsx(path):
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=False, data_only=True)
    sheets = []
    try:
        for name in wb.sheetnames:
            ws = wb[name]
            grid = []
            fills = []
            comments = []
            for r_idx, row in enumerate(ws.iter_rows()):
                vals = []
                for c_idx, cell in enumerate(row):
                    vals.append("" if cell.value is None else str(cell.value).strip())
                    tone = _fill_value(cell)
                    if tone:
                        fills.append({"r": r_idx, "c": c_idx, "value": tone})
                    comment = getattr(cell, "comment", None)
                    if comment is not None and getattr(comment, "text", None):
                        comments.append({"r": r_idx, "c": c_idx, "text": str(comment.text)})
                grid.append(vals)
            merges = []
            for mr in ws.merged_cells.ranges:
                merges.append((mr.min_row - 1, mr.min_col - 1, mr.max_row, mr.max_col))
            sheets.append(
                {"sheet": name, "grid": grid, "merges": merges, "fills": fills, "comments": comments}
            )
    finally:
        wb.close()
    return sheets


def _fill_value(cell):
    try:
        fill = cell.fill
    except Exception:
        return ""
    if fill is None or not getattr(fill, "patternType", None):
        return ""
    fg = getattr(fill, "fgColor", None)
    if fg is None:
        return ""
    kind = getattr(fg, "type", None)
    if kind == "rgb":
        raw = str(getattr(fg, "rgb", "") or "")
        if len(raw) >= 6 and raw not in ("00000000", "None"):
            return raw[-6:].upper()
    if kind == "theme":
        theme = getattr(fg, "theme", None)
        if theme is not None:
            return f"THEME{theme}"
    if kind == "indexed":
        idx = getattr(fg, "indexed", None)
        if idx is not None:
            return f"IDX{idx}"
    return ""


def _load_xls(path):
    import xlrd

    book = xlrd.open_workbook(str(path), formatting_info=False)
    sheets = []
    for idx in range(book.nsheets):
        sh = book.sheet_by_index(idx)
        grid = []
        for r in range(sh.nrows):
            vals = []
            for c in range(sh.ncols):
                v = sh.cell_value(r, c)
                vals.append("" if v is None else str(v).strip())
            grid.append(vals)
        merges = []
        for rlo, rhi, clo, chi in getattr(sh, "merged_cells", []) or []:
            merges.append((rlo, clo, rhi, chi))
        sheets.append({"sheet": sh.name, "grid": grid, "merges": merges, "fills": [], "comments": []})
    return sheets
