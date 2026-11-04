# export/word_renderer.py
"""
导出层：纯标准库（zipfile）生成 Word 报告，无需 python-docx。
生成最小合法 docx：[Content_Types].xml + _rels/.rels + word/document.xml。
块模型（blocks 列表）：
    ("h1", 文本) / ("h2", 文本) / ("p", 文本)      普通段落（支持 \n 换行）
    ("table", {"columns": [...], "rows": [{列: 值}]})  真实 Word 表格
"""
import zipfile  # docx 本质是 zip 包
from pathlib import Path
from xml.sax.saxutils import escape  # XML 转义，防特殊字符撑爆文档

_CT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""


def _paragraph(text: str, style: str = "") -> str:
    """一段文字 → 一个 <w:p>；style 为空是正文，否则引用标题样式"""
    style_xml = f'<w:pStyle w:val="{style}"/>' if style else ""
    # \n 拆成多个 <w:r><w:br/></w:r>，保留换行
    lines = str(text).split("\n")
    runs = []
    for i, ln in enumerate(lines):
        br = "<w:br/>" if i > 0 else ""
        runs.append(f'<w:r>{br}<w:t xml:space="preserve">{escape(ln)}</w:t></w:r>')
    return f'<w:p><w:pPr>{style_xml}</w:pPr>{"".join(runs)}</w:p>'


def _table(table: dict) -> str:
    """{columns, rows} → <w:tbl>；首行加粗为表头"""
    cols = table.get("columns", [])
    if not cols:
        return ""
    borders = ('<w:tblBorders>'
               + "".join(f'<w:{side} w:val="single" w:sz="4" w:color="999999"/>'
                         for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
               + "</w:tblBorders>")
    width = '<w:tblW w:w="5000" w:type="pct"/>'  # 占满页宽

    def row_xml(cells, bold=False):
        rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
        tcs = "".join(
            f'<w:tc><w:tcPr/><w:p><w:r>{rpr}<w:t xml:space="preserve">{escape(str(c))}</w:t></w:r></w:p></w:tc>'
            for c in cells
        )
        return f"<w:tr>{tcs}</w:tr>"

    rows_xml = row_xml(cols, bold=True)
    for r in table.get("rows", []):
        rows_xml += row_xml([r.get(c, "") for c in cols])
    return (f"<w:tbl><w:tblPr>{width}{borders}</w:tblPr>{rows_xml}</w:tbl>"
            f'<w:p><w:pPr/></w:p>')  # 表后补空段，防止两张表粘连


# 样式映射：h1/h2 借用内置 Heading 样式（Word 会自动显示为标题1/2，可生成导航目录）
_STYLE_MAP = {"h1": "Heading1", "h2": "Heading2"}


def build_docx(blocks: list) -> bytes:
    """块列表 → docx 二进制内容（内存中构建，不落盘）"""
    body = []
    for blk in blocks:
        kind = blk[0]
        if kind == "table":
            body.append(_table(blk[1]))
        else:
            body.append(_paragraph(blk[1], _STYLE_MAP.get(kind, "")))
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{"".join(body)}'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'  # A4 页面
        "</w:body></w:document>"
    )
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT_XML)
        z.writestr("_rels/.rels", _RELS_XML)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def render_report(out_path, title: str, blocks: list) -> str:
    """
    生成报告文件：自动加 H1 大标题，写入 out_path（Path 或字符串）。
    返回实际写入的路径字符串，方便上层展示。
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)  # 报告目录不存在则创建
    data = build_docx([("h1", title)] + list(blocks))
    out.write_bytes(data)
    return str(out)
