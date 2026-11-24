# pipeline/stage1_parser/file_parser.py
"""
Stage1 解析层：把磁盘上的文件统一解析成两种中间结构
- kind="text"  → {"text": 全文}            供 Stage2 判别 / Stage3 分块
- kind="table" → {"table": {columns, rows}} 供 Stage3 表格引擎
支持：TXT / MD / DOCX（zipfile 标准库提取）/ CSV；
PDF、XLSX 依赖可选库（pypdf / openpyxl），未安装时优雅提示，不崩溃。
图片等二进制文件返回 kind="unsupported"，明确告知原因，绝不静默吞掉。
"""
import csv  # 标准库 CSV 解析
import hashlib  # 内容指纹（缓存 / 去重 / 增量）
import html  # 还原 DOCX XML 里的 &amp; 等实体
import io  # 把 CSV 文本包成文件对象喂给 csv.reader
import re  # 清洗 DOCX 的 XML 标签
import zipfile  # 标准库读 DOCX（docx 本质是 zip）
from pathlib import Path

# 可解析为纯文本的扩展名 / 可解析为表格的扩展名
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".docx", ".docm", ".pdf", ".rtf", ".log", ".pptx",
}
TABLE_EXTS = {".csv", ".tsv", ".xlsx", ".xls"}


def _max_bytes() -> int:
    """单文件大小上限（MB），由参数 parser.max_size_mb 控制"""
    import config
    return int(config.get_param("parser.max_size_mb", 20)) * 1024 * 1024


def parse_file(path) -> dict:
    """
    统一入口：任意文件 → 中间结构。

    返回 dict 字段:
        path/filename/ext/mtime/hash  基础信息（hash 为内容 sha256）
        kind                          "text" | "table" | "unsupported"
        text                          kind=text 时的全文
        table                         kind=table 时的 {name, columns, rows}
        note                          附加提示（如"未安装依赖"），可为 None
    """
    path = Path(path)
    info = {
        "path": str(path), "filename": path.name, "ext": path.suffix.lower(),
        "mtime": path.stat().st_mtime if path.exists() else 0,
        "hash": "", "kind": "unsupported", "text": "", "table": None, "note": None,
    }
    if not path.exists() or not path.is_file():
        info["note"] = "文件不存在"
        return info
    try:
        max_bytes = _max_bytes()
        if path.stat().st_size > max_bytes:
            info["note"] = f"文件超过 {max_bytes // 1024 // 1024}MB，暂不处理"
            return info
        raw = path.read_bytes()  # 一次读入，同时算 hash 和文本
    except PermissionError:
        # 常见于：文件正被 Word/WPS 打开、OneDrive 同步锁定、系统权限不足。
        # 单文件失败不能让整条管线崩溃 → 标记为跳过并说明原因，其余文件继续。
        info["note"] = "没有读取权限（文件可能正被 Word/WPS 占用或被 OneDrive 锁定，请关闭后重试）"
        return info
    except OSError as e:
        info["note"] = f"文件读取失败：{e}"
        return info
    info["hash"] = hashlib.sha256(raw).hexdigest()
    ext = info["ext"]

    # ---- 表格类 ----
    if ext == ".csv" or ext == ".tsv":
        text = raw.decode("utf-8-sig", errors="replace")  # 兼容带 BOM 的导出文件
        info["kind"] = "table"
        info["table"] = _parse_csv(text, delimiter="\t" if ext == ".tsv" else ",")
        return info

    if ext == ".xls":
        tables, note = _parse_xls(path)
        if not tables:
            tables, note2 = _parse_xlsx(path)
            note = note2 or note
        if tables:
            info["kind"] = "table"
            info["table"] = tables[0]
            info["extra_tables"] = tables[1:]
            if note:
                info["note"] = note
        else:
            info["note"] = note or "无法解析表格"
        return info

    if ext == ".xlsx":
        tables, note = _parse_xlsx(path)
        if tables:
            info["kind"] = "table"
            info["table"] = tables[0]
            info["extra_tables"] = tables[1:]
            if note:
                info["note"] = note
        else:
            info["note"] = note or "无法解析表格"
        return info

    # ---- 文本类 ----
    if ext in (".txt", ".md", ".markdown", ".log", ".rtf"):
        info["kind"] = "text"
        info["text"] = raw.decode("utf-8", errors="replace")
        return info

    if ext in (".docx", ".docm"):
        # .docm 同为 OOXML zip；只抽文本，不执行宏
        info["kind"] = "text"
        info["text"] = _docx_to_text(raw)
        if ext == ".docm":
            info["note"] = "macro_docm"
        return info

    if ext == ".doc":
        # 老格式不在此直接解析；由 content_split.legacy_word 可选 COM 转换
        info["note"] = "legacy_doc_no_converter"
        return info

    if ext == ".pptx":
        info["kind"] = "text"
        info["text"] = _pptx_to_text(raw)
        if not info["text"].strip():
            info["note"] = "PPTX 中未提取到文本（可能全是图片/形状）"
        return info

    if ext == ".pdf":
        text, note = _pdf_to_text(raw)
        if text is not None:
            info["kind"], info["text"] = "text", text
        else:
            info["note"] = note
        return info

    info["note"] = f"暂不支持解析 {ext or '无扩展名'} 文件"
    return info


# ------------------------------------------------------------------
# 各格式解析
# ------------------------------------------------------------------

def _parse_csv(text: str, delimiter: str = ",") -> dict:
    """CSV 文本 → {name, columns, rows:[{列名: 值}]}；列名缺失时自动补 列1/列2..."""
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows_raw = [r for r in reader if any(c.strip() for c in r)]  # 跳过全空行
    if not rows_raw:
        return {"name": "", "columns": [], "rows": [], "grid": []}
    grid = [[c.strip() for c in r] for r in rows_raw]  # 原始矩阵，供 table_normalizer 二次制表
    columns = [c if c else f"列{i + 1}" for i, c in enumerate(grid[0])]  # 空列名兜底
    rows = []
    for r in grid[1:]:
        row = {}
        for i, col in enumerate(columns):
            row[col] = r[i].strip() if i < len(r) else ""  # 短行缺列补空串
        rows.append(row)
    return {"name": "", "columns": columns, "rows": rows, "grid": grid}


def _parse_xlsx(path: Path):
    """xlsx 依赖 openpyxl；返回 (tables_list, note)。多 sheet 全部解析，不写死只读活动表。"""
    try:
        from openpyxl import load_workbook  # 可选依赖，按需导入
    except ImportError:
        return [], "解析 XLSX 需要安装 openpyxl（pip install openpyxl），本次已跳过"
    try:
        # 不用 read_only：合并单元格只有普通模式能读到，否则空白格会丢上层归属
        wb = load_workbook(path, read_only=False, data_only=True)
        tables = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = []
            fills = []
            for row in ws.iter_rows():
                vals, tones = [], []
                for cell in row:
                    vals.append("" if cell.value is None else str(cell.value).strip())
                    tones.append(_cell_fill_rgb(cell))
                rows.append(vals)
                fills.append(tones)
            if not rows:
                continue
            merges = []
            for mr in ws.merged_cells.ranges:
                merges.append((mr.min_row - 1, mr.min_col - 1, mr.max_row - 1, mr.max_col - 1))
            tables.append(_table_from_matrix(
                path.name, sheet_name, len(wb.sheetnames), rows, merges, fills=fills))
        wb.close()
        if not tables:
            return [{"name": path.name, "columns": [], "rows": []}], None
        note = None if len(tables) == 1 else f"已解析 {len(tables)} 个工作表"
        return tables, note
    except Exception as e:  # 损坏的 xlsx 等异常统一提示
        return [], f"XLSX 解析失败：{e}"


def _parse_xls(path: Path):
    """旧版 xls 走 xlrd。打不开时返回空，调用方再试 xlsx。"""
    try:
        import xlrd
    except ImportError:
        return [], "解析 XLS 需要安装 xlrd（pip install xlrd），本次已跳过"
    try:
        book = xlrd.open_workbook(str(path), formatting_info=False)
    except Exception as e:
        return [], f"XLS 解析失败：{e}"
    tables = []
    nsheets = book.nsheets
    for idx in range(nsheets):
        sh = book.sheet_by_index(idx)
        if sh.nrows <= 0:
            continue
        rows = []
        for r in range(sh.nrows):
            vals = []
            for c in range(sh.ncols):
                v = sh.cell_value(r, c)
                vals.append("" if v is None else str(v).strip())
            rows.append(vals)
        merges = []
        for rlo, rhi, clo, chi in getattr(sh, "merged_cells", []) or []:
            merges.append((rlo, clo, rhi - 1, chi - 1))
        tables.append(_table_from_matrix(path.name, sh.name, nsheets, rows, merges))
    if not tables:
        return [{"name": path.name, "columns": [], "rows": []}], None
    note = None if len(tables) == 1 else f"已解析 {len(tables)} 个工作表"
    return tables, note


def _cell_fill_rgb(cell) -> str:
    """读单元格底色为 RRGGBB；主题色/索引色记成可区分的标记，不强行猜业务含义。"""
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


def _cell_comment_text(cell) -> str:
    """读单元格批注纯文本；无批注返回空串。只存储，不解释语义。"""
    try:
        c = cell.comment
    except Exception:
        return ""
    if c is None:
        return ""
    text = getattr(c, "text", None)
    if text is None:
        return ""
    return text if isinstance(text, str) else str(text)


def _table_from_matrix(filename: str, sheet_name: str, nsheets: int,
                       rows: list, merges: list, fills: list = None) -> dict:
    columns = [c if c else f"列{i + 1}" for i, c in enumerate(rows[0])]
    data = [{columns[i]: (r[i] if i < len(r) else "") for i in range(len(columns))}
            for r in rows[1:]]
    tname = filename if nsheets == 1 else f"{filename}::{sheet_name}"
    blocks = _geometry_blocks(rows, merges)
    table = {"name": tname, "columns": columns, "rows": data, "blocks": blocks}
    if _sheet_keeps_grid(rows, merges, blocks):
        table["grid"] = rows
        table["merges"] = merges
        if fills:
            table["fills"] = fills
    return table


def _sheet_keeps_grid(rows: list, merges: list, blocks: list) -> bool:
    """只有看表头还还原不了正文时才留网格。普通密表靠列名即可，不复制全量。"""
    if merges or len(blocks or []) > 1:
        return True
    if not rows:
        return False
    width = max((len(r) for r in rows), default=0) or 1
    top = sum(1 for v in rows[0] if str(v or "").strip())
    return top < max(2, width // 2)


def _column_bands(rows: list, merges: list, r0: int, r1: int, width: int) -> list:
    """一整行带里，被整列空白隔开、且没有合并格跨过空白的左右两块，拆成并排的表。"""
    used = [
        c for c in range(width)
        if any(c < len(rows[r]) and str(rows[r][c] or "").strip() for r in range(r0, r1))
    ]
    if not used:
        return []
    spans = []
    start = prev = used[0]
    for c in used[1:]:
        gutter = list(range(prev + 1, c))
        crossed = False
        for a, b, _c, d in merges or []:
            if _c < r0 or a >= r1:
                continue
            if b <= prev and d >= c:
                crossed = True
                break
        if gutter and not crossed:
            spans.append((start, prev + 1))
            start = c
        prev = c
    spans.append((start, prev + 1))
    return spans


def _geometry_blocks(rows: list, merges: list) -> list:
    """按全空行、再按并排空列把一张表切成块。只记录表头、行侧和少量格子。"""
    if not rows:
        return []
    width = max(len(r) for r in rows)
    blank = {
        i for i, r in enumerate(rows)
        if not any(str(c or "").strip() for c in r)
    }
    bands = []
    start = None
    for i in range(len(rows) + 1):
        if i == len(rows) or i in blank:
            if start is not None:
                bands.append((start, i))
                start = None
        elif start is None:
            start = i
    blocks = []
    bi = 0
    for r0, r1 in bands:
        for c0, c1 in _column_bands(rows, merges, r0, r1, width):
            head = []
            for r in range(r0, min(r1, r0 + 6)):
                cells = []
                for c in range(c0, c1):
                    v = rows[r][c].strip() if c < len(rows[r]) else ""
                    if v:
                        cells.append(f"{c - c0}:{v[:24]}")
                head.append(" | ".join(cells[:18]))
            side = []
            for r in range(min(r1, r0 + 6), min(r1, r0 + 12)):
                cells = []
                for c in range(c0, min(c1, c0 + 2)):
                    v = rows[r][c].strip() if c < len(rows[r]) else ""
                    if v:
                        cells.append(v[:24])
                if cells:
                    side.append(" | ".join(cells))
            span = []
            for a, b, c, d in merges or []:
                if c < r0 or a >= r1 or d < c0 or b >= c1:
                    continue
                v = rows[a][b].strip() if b < len(rows[a]) else ""
                span.append(f"r{a - r0}c{b - c0} {c - a + 1}x{d - b + 1} {v[:16]}")
                if len(span) >= 16:
                    break
            samples = []
            step = max(1, (r1 - r0) // 12)
            for r in range(r0, r1, step):
                for c in range(c0, c1):
                    v = rows[r][c].strip() if c < len(rows[r]) else ""
                    if v and v not in samples:
                        samples.append(v[:40])
                    if len(samples) >= 10:
                        break
                if len(samples) >= 10:
                    break
            blocks.append({
                "id": f"b{bi}",
                "r0": r0, "r1": r1, "c0": c0, "c1": c1,
                "head": head, "side": side, "merges": span, "samples": samples,
            })
            bi += 1
    return blocks


def _flatten_crosstab(rows: list, merges: list, base_name: str):
    """
    多级横表头矩阵 → 长表。合并单元格先回填，每列只继承本列更上方的标题，
    不把同一分区里其它列的值拼进来。普通明细表（首行就是密表头）返回 None。
    """
    if not rows or len(rows) < 4:
        return None
    width = max(len(r) for r in rows)
    if width < 4:
        return None
    grid = []
    for r in rows:
        grid.append([str(c or "").strip() for c in list(r)] + [""] * (width - len(r)))
    for min_r, min_c, max_r, max_c in merges or []:
        if min_r >= len(grid) or min_c >= width:
            continue
        val = grid[min_r][min_c]
        if not val:
            continue
        for rr in range(min_r, min(max_r, len(grid) - 1) + 1):
            for cc in range(min_c, min(max_c, width - 1) + 1):
                grid[rr][cc] = val
    # 首行过密则是普通表头，不展开
    top_ne = sum(1 for v in grid[0] if v)
    wide_merges = sum(1 for a, b, c, d in (merges or []) if a == 0 and d - b >= 1)
    if top_ne > max(2, width // 3) and wide_merges < 2:
        return None

    def _banner(vals) -> bool:
        ne = [v for v in vals if v]
        if not ne:
            return False
        ratio = len(ne) / width
        uniq = len(set(ne)) / len(ne)
        adj_n = adj = 0
        for a, b in zip(vals, vals[1:]):
            if a and b:
                adj_n += 1
                if a == b:
                    adj += 1
        same = (adj / adj_n) if adj_n else 0.0
        med = sorted(len(v) for v in ne)[len(ne) // 2]
        if ratio <= 0.22:
            return True
        if same >= 0.22:
            return True
        if uniq <= 0.62 and med <= 12 and ratio < 0.85:
            return True
        return False

    leading = []
    for i, row in enumerate(grid[:8]):
        if _banner(row):
            leading.append(i)
        elif any(row):
            break
    if not leading or leading[0] > 2:
        return None
    nlevels = len(leading)
    level_of = {ri: k for k, ri in enumerate(leading)}
    col_stack = [[""] * nlevels for _ in range(width)]

    def _section_reset(vals) -> bool:
        ne = [v for v in vals if v]
        return bool(ne) and (len(ne) / width) <= 0.22

    out = []
    seen = set()
    for ri, row in enumerate(grid):
        if ri in level_of or (ri > leading[-1] and _banner(row)):
            if ri in level_of:
                lv = level_of[ri]
                for c, v in enumerate(row):
                    if not v:
                        continue
                    col_stack[c][lv] = v
                    for deeper in range(lv + 1, nlevels):
                        col_stack[c][deeper] = ""
            else:
                reset = _section_reset(row)
                for c, v in enumerate(row):
                    if not v:
                        continue
                    if reset:
                        col_stack[c][0] = v
                        for deeper in range(1, nlevels):
                            col_stack[c][deeper] = ""
                    else:
                        col_stack[c][nlevels - 1] = v
            continue
        for c, v in enumerate(row):
            if not _crosstab_entity(v):
                continue
            stack = list(col_stack[c])
            if not any(stack):
                continue
            key = (tuple(stack), v)
            if key in seen:
                continue
            seen.add(key)
            out.append((stack, v))
    if len(out) < 5:
        return None
    depth = nlevels
    columns = [f"层级{i + 1}" for i in range(depth)] + ["条目"]
    records = []
    for stack, v in out:
        rec = {"条目": v}
        for i in range(depth):
            rec[f"层级{i + 1}"] = stack[i] if i < len(stack) else ""
        records.append(rec)
    return {
        "name": f"{base_name}::版式展开",
        "columns": columns,
        "rows": records,
    }


def _crosstab_entity(value: str) -> bool:
    """矩阵正文格：去掉纯数字、过短、过长说明。不维护业务词。"""
    v = str(value or "").strip()
    if len(v) < 2 or len(v) > 40:
        return False
    if re.fullmatch(r"[\d\.\-\+\s]+", v):
        return False
    if re.fullmatch(r"\d+\s*[kK][vV]", v):
        return False
    return True


def _pdf_to_text(raw: bytes):
    """PDF 依赖 pypdf（或旧名 PyPDF2）；未安装时返回 (None, 提示)"""
    reader = None
    try:
        from pypdf import PdfReader as reader  # 首选新包名
    except ImportError:
        try:
            from PyPDF2 import PdfReader as reader  # 兼容旧包名
        except ImportError:
            return None, "解析 PDF 需要安装 pypdf（pip install pypdf），本次已跳过"
    try:
        import io as _io
        pdf = reader(_io.BytesIO(raw))
        pages = [(p.extract_text() or "") for p in pdf.pages]
        return "\n\n".join(pages), None
    except Exception as e:
        return None, f"PDF 解析失败（可能是扫描件）：{e}"


def _docx_to_text(raw: bytes) -> str:
    """
    用标准库 zipfile 直接读 DOCX 的 word/document.xml（无需 python-docx）。
    段落 </w:p> 转换行、<w:tab/> 转制表符、去其余标签、还原 XML 实体。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError):
        return ""  # 伪 docx（实为改名的其他格式）→ 空文本，由上游提示内容过短
    xml = xml.replace("</w:p>", "\n")          # 段落边界
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)  # 制表符
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)   # 软换行
    text = re.sub(r"<[^>]+>", "", xml)         # 去掉所有剩余标签
    return html.unescape(text)                 # 还原 &amp; 等实体


def _pptx_to_text(raw: bytes) -> str:
    """
    用标准库 zipfile 直接读 PPTX 的 ppt/slides/slideN.xml（无需 python-pptx）。
    每张幻灯片的 <a:t> 文本按幻灯片自然顺序拼接，幻灯片之间用空行分隔。
    处理含图片/无文本的幻灯片时返回空字符串，由上游提示。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            slide_names = [n for n in z.namelist()
                           if re.match(r"ppt/slides/slide\d+\.xml$", n)]
            if not slide_names:
                return ""
            # 按幻灯片序号自然排序（slide10.xml 不能排到 slide2.xml 前面）
            slide_names.sort(key=lambda n: int(re.search(r"slide(\d+)\.xml", n).group(1)))
            pages = []
            for name in slide_names:
                try:
                    xml = z.read(name).decode("utf-8", errors="replace")
                except (KeyError, OSError):
                    continue
                texts = re.findall(r"<a:t>(.*?)</a:t>", xml, re.S)
                texts = [html.unescape(re.sub(r"<[^>]+>", "", t)) for t in texts]
                page = "".join(texts).strip()
                if page:
                    pages.append(page)
    except (zipfile.BadZipFile, KeyError):
        return ""  # 伪 pptx → 空文本
    return "\n\n".join(pages)
