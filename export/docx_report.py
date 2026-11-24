# -*- coding: utf-8 -*-
"""通用表格报告：≤max_rows 只出 docx；超出再附 xlsx 全量。

写入：临时文件 → os.replace；目标被占用 → 时间戳副本。
过程日志仍走 .log，不经本模块。
"""
from __future__ import annotations

import os
import time
from pathlib import Path


def _atomic_write_bytes(dest: Path, data: bytes) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(str(tmp), str(dest))
        return dest
    except OSError:
        try:
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass
        stamp = time.strftime("%Y%m%d_%H%M%S")
        alt = dest.with_name(f"{dest.stem}_{stamp}{dest.suffix}")
        alt.write_bytes(data)
        return alt


def _build_docx_bytes(
    title: str,
    meta_lines: list,
    headers: list,
    rows: list,
    note: str,
    *,
    landscape: bool | None = None,
) -> bytes:
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor
    import io

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Microsoft YaHei"
    normal.font.size = Pt(10)
    try:
        normal.element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    except Exception:  # noqa: BLE001
        pass

    ncols = len(headers)
    use_land = landscape if landscape is not None else ncols >= 7
    sec = doc.sections[0]
    if use_land:
        sec.orientation = WD_ORIENT.LANDSCAPE
        sec.page_width, sec.page_height = sec.page_height, sec.page_width
    sec.left_margin = sec.right_margin = Cm(1.6)
    sec.top_margin = sec.bottom_margin = Cm(1.5)

    h = doc.add_heading(str(title or "清单"), level=1)
    for run in h.runs:
        run.font.name = "Microsoft YaHei"
        try:
            run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        except Exception:  # noqa: BLE001
            pass

    for line in meta_lines or []:
        p = doc.add_paragraph(str(line))
        p.paragraph_format.space_after = Pt(2)
        for run in p.runs:
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(0x6B, 0x72, 0x80)

    if headers:
        table = doc.add_table(rows=1, cols=ncols)
        table.style = "Table Grid"
        for i, name in enumerate(headers):
            cell = table.rows[0].cells[i]
            cell.text = str(name)
            for p in cell.paragraphs:
                for run in p.runs:
                    run.font.bold = True
                    run.font.size = Pt(9)
                    run.font.name = "Microsoft YaHei"
                    try:
                        run._element.rPr.rFonts.set(
                            qn("w:eastAsia"), "Microsoft YaHei"
                        )
                    except Exception:  # noqa: BLE001
                        pass
        # 均分可用宽
        page_w = float(sec.page_width.cm) - float(sec.left_margin.cm) - float(
            sec.right_margin.cm
        )
        col_w = max(page_w / max(ncols, 1), 1.5)
        for row_vals in rows or []:
            cells = table.add_row().cells
            for i in range(ncols):
                val = row_vals[i] if i < len(row_vals) else ""
                cells[i].text = str(val)
                for p in cells[i].paragraphs:
                    for run in p.runs:
                        run.font.size = Pt(8.5)
                        run.font.name = "Microsoft YaHei"
                        try:
                            run._element.rPr.rFonts.set(
                                qn("w:eastAsia"), "Microsoft YaHei"
                            )
                        except Exception:  # noqa: BLE001
                            pass
        for row in table.rows:
            for cell in row.cells:
                cell.width = Cm(col_w)

    if note:
        p = doc.add_paragraph(str(note))
        p.paragraph_format.space_before = Pt(6)
        for run in p.runs:
            run.font.size = Pt(8)
            run.font.color.rgb = RGBColor(0x8A, 0x90, 0x99)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _write_xlsx(path: Path, headers: list, rows: list) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "明细"
    ws.append([str(h) for h in headers])
    for row in rows:
        ws.append([str(c) for c in row])
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        wb.save(str(tmp))
        os.replace(str(tmp), str(dest))
        return dest
    except OSError:
        try:
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass
        stamp = time.strftime("%Y%m%d_%H%M%S")
        alt = dest.with_name(f"{dest.stem}_{stamp}{dest.suffix}")
        wb.save(str(alt))
        return alt


def write_table_report(
    base_path,
    title: str,
    meta_lines: list,
    headers: list,
    rows: list,
    note: str = "",
    *,
    max_rows: int = 500,
) -> list[Path]:
    """
    ≤max_rows → 仅 docx；>max_rows → xlsx 全量 + docx 摘要（前 100 行）。
    返回实际写出的路径列表（可能含时间戳副本）。
    """
    base = Path(base_path)
    headers = list(headers or [])
    rows = [list(r) for r in (rows or [])]
    meta = list(meta_lines or [])
    out: list[Path] = []
    n = len(rows)

    if n <= max_rows:
        data = _build_docx_bytes(title, meta, headers, rows, note)
        out.append(_atomic_write_bytes(base.with_suffix(".docx"), data))
        return out

    xlsx_path = _write_xlsx(base.with_suffix(".xlsx"), headers, rows)
    out.append(xlsx_path)
    preview = rows[:100]
    note2 = (
        f"{note}\n" if note else ""
    ) + f"共 {n} 行，完整明细见「{xlsx_path.name}」；下文仅摘要前 100 行。"
    meta2 = meta + [f"行数：{n}（超出 {max_rows}，已拆 xlsx）"]
    data = _build_docx_bytes(title, meta2, headers, preview, note2)
    out.insert(0, _atomic_write_bytes(base.with_suffix(".docx"), data))
    return out
