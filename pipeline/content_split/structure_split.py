# -*- coding: utf-8 -*-
"""P2A：按文档内已有标题/章节结构拆成多份新文档（原件不动，0 token）。

不猜自由主题；无可靠切点则诚实失败，不整篇复制冒充拆段。
"""
from __future__ import annotations

import re
from pathlib import Path

from export.word_renderer import render_report

from .common import list_top_docx, resolve_work_dir, safe_filename, unique_dest

OUT_ROOT = "内容拆分"

# 标题样式名（Word 中英文常见）
_HEADING_STYLE = re.compile(
    r"^(Heading\s*[1-3]|标题\s*[一二三123]|标题\s*[1-3])$",
    re.I,
)
# 正文行章节锚点（无标题样式时的机械兜底）
_CHAPTER_LINE = re.compile(
    r"^(第[一二三四五六七八九十百千零〇\d]+[章节篇部]|[一二三四五六七八九十]+[、.．]\s*\S|"
    r"\d+(\.\d+)*\s+\S{1,40})$"
)


def _para_rows(path: Path) -> list[dict]:
    """[(text, style_name, is_heading)] via python-docx（与 WordOffice 同依赖）。"""
    from docx import Document

    doc = Document(str(path))
    rows = []
    for p in doc.paragraphs:
        text = (p.text or "").strip()
        style = ""
        try:
            style = str(p.style.name or "") if p.style is not None else ""
        except Exception:  # noqa: BLE001
            style = ""
        is_h = bool(_HEADING_STYLE.match(style.strip()))
        if not is_h and text and _CHAPTER_LINE.match(text):
            is_h = True
        rows.append({"text": text, "style": style, "is_heading": is_h})
    return rows


def _split_sections(rows: list[dict]) -> list[dict]:
    """切成 [{title, body_lines}]；至少一个标题切点才算成功。"""
    heading_idxs = [i for i, r in enumerate(rows) if r["is_heading"] and r["text"]]
    if len(heading_idxs) < 1:
        return []

    # 标题前的导语：并入第一个切片（标为「前言」仅当确有正文）
    sections = []
    first = heading_idxs[0]
    if first > 0:
        preamble = [rows[i]["text"] for i in range(first) if rows[i]["text"]]
        if preamble:
            sections.append({"title": "前言", "body": preamble})

    for j, hi in enumerate(heading_idxs):
        title = rows[hi]["text"]
        end = heading_idxs[j + 1] if j + 1 < len(heading_idxs) else len(rows)
        body = [rows[i]["text"] for i in range(hi + 1, end) if rows[i]["text"]]
        sections.append({"title": title, "body": body})

    # 至少两份产出才叫「拆」；仅一个标题且无前言 → 不够
    if len(sections) < 2:
        return []
    return sections


def _write_section(dest: Path, title: str, body: list[str]) -> str:
    blocks = [("p", line) for line in body] or [("p", "（本节无正文）")]
    return render_report(dest, title, blocks)


def run_structure_split(user_input: str, target_dir, selected_files=None) -> dict:
    work = resolve_work_dir(user_input, Path(target_dir))
    if not work.is_dir():
        return {
            "ok": False,
            "handler": "系统",
            "message": "⚠️ 工作目录无效，无法按结构拆段。",
            "operations": [],
            "tool": "structure_split",
            "reason": "bad_work_dir",
        }

    selected = []
    for raw in selected_files or []:
        p = Path(str(raw))
        if p.is_file() and p.suffix.lower() == ".docx" and not p.name.startswith("~$"):
            selected.append(p.resolve())

    sources = selected or [
        p for p in list_top_docx(work, exclude_dir_names=(OUT_ROOT, "内容分类", "模板成稿"))
        if OUT_ROOT not in p.parts
    ]
    if not sources:
        return {
            "ok": False,
            "handler": "系统",
            "message": (
                f"在「{work}」没有可拆的 .docx（可选中文件，或放在目录顶层）。\n"
                "已跳过 ~$ 临时文件。"
            ),
            "operations": [],
            "tool": "structure_split",
            "reason": "no_docx",
        }

    root_out = work / OUT_ROOT
    root_out.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    rows_log: list[dict] = []
    skipped: list[str] = []

    for src in sources:
        try:
            paras = _para_rows(src)
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{src.name}（无法打开：{exc}）")
            continue
        sections = _split_sections(paras)
        if not sections:
            skipped.append(
                f"{src.name}（未检出标题/章节结构；"
                "可改说「内容识别后分类进各自新文档」走六类整文件另存）"
            )
            continue
        folder = root_out / safe_filename(src.stem, "文档")
        folder.mkdir(parents=True, exist_ok=True)
        for sec in sections:
            fname = safe_filename(sec["title"]) + ".docx"
            dest = unique_dest(folder, fname)
            try:
                _write_section(dest, sec["title"], sec["body"])
            except OSError as exc:
                skipped.append(f"{src.name}/{sec['title']}（写入失败：{exc}）")
                continue
            created.append(str(dest.resolve()))
            rows_log.append({
                "src": src.name,
                "title": sec["title"],
                "dest": str(dest.relative_to(work)),
                "paras": len(sec["body"]),
            })

    from export.docx_report import write_table_report

    headers = ["原文件", "切片标题", "新文档", "段数"]
    table_rows = [
        [r["src"], r["title"], r["dest"], r["paras"]] for r in rows_log
    ]
    note = "说明：按标题/章节结构拆段另存；原件未改。撤回本轮可删产物。"
    if skipped:
        note += "\n跳过：\n· " + "\n· ".join(skipped)
    meta_lines = [
        f"工作目录：{work}",
        f"指令：{str(user_input or '').strip()}",
        f"本轮切片：{len(rows_log)} · 跳过 {len(skipped)}",
    ]
    try:
        for mp in write_table_report(
            root_out / "拆分清单",
            "结构拆分清单",
            meta_lines,
            headers,
            table_rows,
            note,
            max_rows=500,
        ):
            created.append(str(mp.resolve()))
        legacy_md = root_out / "_拆分清单.md"
        if legacy_md.is_file():
            try:
                legacy_md.unlink()
            except OSError:
                pass
    except OSError:
        pass

    if not rows_log:
        return {
            "ok": False,
            "handler": "系统",
            "message": (
                "未写出任何拆分段"
                + ("：\n· " + "\n· ".join(skipped) if skipped else "。")
            ),
            "operations": [],
            "tool": "structure_split",
            "reason": "no_structure",
            "report_paths": created,
        }

    out_abs = str((work / OUT_ROOT).resolve())
    msg = (
        f"已按标题/章节拆成 {len(rows_log)} 份新文档（原件未动）。\n"
        f"输出目录：{out_abs}\n"
        f"清单：{OUT_ROOT}/拆分清单.docx"
    )
    if skipped:
        msg += "\n跳过：\n· " + "\n· ".join(skipped)
    return {
        "ok": True,
        "handler": "结构拆分",
        "message": msg,
        "operations": [],
        "tool": "structure_split",
        "reason": "ok",
        "report_paths": created,
        "sections": rows_log,
        "work_dir": str(work.resolve()),
        "output_dir": out_abs,
    }
