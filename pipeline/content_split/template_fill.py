# -*- coding: utf-8 -*-
"""P2B：模板填槽另存（原件不动，0 token；不瞎填缺失槽）。

槽位：{{名}} / 【名】 / ｛名｝。值来自指令或同目录材料（名=值 / 名：值）。
成稿 = 复制模板再就地替换（保留样式/表格/页眉页脚）；不用从零重建。
品类不绑死合同/报告；长文风格仿写不在本模块。
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from .common import list_top_docx, resolve_work_dir, safe_filename, unique_dest

OUT_ROOT = "模板成稿"

_SLOT_PATTERNS = (
    re.compile(r"\{\{\s*([^\{\}]+?)\s*\}\}"),
    re.compile(r"【\s*([^【】]+?)\s*】"),
    re.compile(r"｛\s*([^｛｝]+?)\s*｝"),
)
_ANY_SLOT = re.compile(
    r"\{\{[^\{\}]+?\}\}|【[^【】]+?】|｛[^｛｝]+?｝"
)
# 值：引号包一段，或单个非空白 token（避免吞掉后面的「乙方=…」）
_KV = re.compile(
    r"(?:^|[\n；;，,。\s])"
    r"([^\s:=：＝\{\}【】｛｝]{1,40})\s*[=:：＝]\s*"
    r"(?:「([^」]+)」|\"([^\"]+)\"|'([^']+)'|(\S+))"
)


def _find_slots(text: str) -> list[str]:
    names = []
    seen = set()
    for pat in _SLOT_PATTERNS:
        for m in pat.finditer(text or ""):
            name = m.group(1).strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _replace_slots(text: str, values: dict[str, str]) -> str:
    out = text or ""

    def repl_factory(pat):
        def _repl(m):
            key = m.group(1).strip()
            if key in values:
                return values[key]
            return m.group(0)

        return _repl

    for pat in _SLOT_PATTERNS:
        out = pat.sub(repl_factory(pat), out)
    return out


def _parse_kv_blob(blob: str) -> dict[str, str]:
    vals = {}
    for m in _KV.finditer("\n" + str(blob or "")):
        k = m.group(1).strip()
        v = next((g for g in m.groups()[1:] if g), "")
        v = str(v).strip().strip("「」\"' ")
        if k and v:
            vals[k] = v
    return vals


def _read_text_file(path: Path) -> str:
    try:
        if path.suffix.lower() == ".docx":
            from pipeline.stage1_parser.file_parser import parse_file
            return str(parse_file(path).get("text") or "")
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def _gather_values(user_input: str, work: Path, template: Path) -> dict[str, str]:
    vals = _parse_kv_blob(user_input)
    try:
        for p in sorted(work.iterdir()):
            if not p.is_file() or p.resolve() == template.resolve():
                continue
            if p.name.startswith("~$") or p.name.startswith("."):
                continue
            if "材料" not in p.stem and "填槽" not in p.stem:
                continue
            if p.suffix.lower() not in (".txt", ".md", ".docx"):
                continue
            vals.update(_parse_kv_blob(_read_text_file(p)))
    except OSError:
        pass
    return vals


def _pick_template(work: Path, selected_files) -> tuple[Path | None, str]:
    selected = []
    for raw in selected_files or []:
        p = Path(str(raw))
        if p.is_file() and p.suffix.lower() == ".docx" and not p.name.startswith("~$"):
            selected.append(p.resolve())
    if len(selected) == 1:
        return selected[0], ""
    if len(selected) > 1:
        return None, "选中了多个 .docx，请只选中一份模板再试。"

    named = [
        p for p in list_top_docx(work, exclude_dir_names=(OUT_ROOT, "内容分类", "内容拆分"))
        if "模板" in p.stem
    ]
    if len(named) == 1:
        return named[0], ""
    if len(named) > 1:
        names = "、".join(p.name for p in named[:8])
        return None, f"目录里有多份模板（{names}），请在文件管理器选中要用的那一份。"
    if not named:
        return None, (
            "未找到模板：请选中一份含槽位的 .docx，"
            "或把模板文件名带上「模板」放在目录顶层。"
        )
    return None, "未找到模板。"


def _iter_paragraphs(doc):
    """正文段落 + 表格单元格 + 页眉页脚。"""
    for p in doc.paragraphs:
        yield p
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    yield p
    for section in doc.sections:
        for part in (
            section.header, section.footer,
            section.first_page_header, section.first_page_footer,
            section.even_page_header, section.even_page_footer,
        ):
            if part is None:
                continue
            try:
                for p in part.paragraphs:
                    yield p
                for table in part.tables:
                    for row in table.rows:
                        for cell in row.cells:
                            for p in cell.paragraphs:
                                yield p
            except Exception:  # noqa: BLE001
                continue


def _paragraph_full_text(paragraph) -> str:
    return "".join((r.text or "") for r in paragraph.runs) or (paragraph.text or "")


def _set_paragraph_text_keep_style(paragraph, new_text: str) -> None:
    """整段替换：保留段落样式与首 run 字体；多 run 合并写入首 run。"""
    runs = list(paragraph.runs)
    if not runs:
        paragraph.add_run(new_text)
        return
    runs[0].text = new_text
    for r in runs[1:]:
        r.text = ""


def _fill_document_inplace(doc, values: dict[str, str]) -> int:
    """就地替换槽位，返回替换过的段落数。"""
    n = 0
    for p in _iter_paragraphs(doc):
        old = _paragraph_full_text(p)
        if not old or not _ANY_SLOT.search(old):
            continue
        new = _replace_slots(old, values)
        if new == old:
            continue
        _set_paragraph_text_keep_style(p, new)
        n += 1
    return n


def _template_all_text(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts = []
    for p in _iter_paragraphs(doc):
        t = _paragraph_full_text(p)
        if t:
            parts.append(t)
    return "\n".join(parts)


def _write_filled_copy(template: Path, dest: Path, values: dict[str, str]) -> str:
    """复制模板 → 就地填槽（样式/表格/页眉保留）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template, dest)
    from docx import Document

    doc = Document(str(dest))
    _fill_document_inplace(doc, values)
    doc.save(str(dest))
    return str(dest.resolve())


def _normalize_fill_row(raw) -> list[str] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) < 1:
        return None
    vals = [str(c or "") for c in list(raw)[:3]]
    while len(vals) < 3:
        vals.append("")
    return vals if any(vals) else None


def _parse_legacy_fill_md(md: Path) -> list[list]:
    rows: list[list] = []
    try:
        for line in md.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s.startswith("|"):
                continue
            if s.startswith("|---"):
                continue
            if ("模板" in s) and ("槽位" in s):
                continue
            cells = [c.strip().strip("`") for c in s.strip("|").split("|")]
            row = _normalize_fill_row(cells)
            if row and row[0] != "模板":
                rows.append(row)
    except OSError:
        return []
    return rows


def _parse_legacy_fill_xlsx(xlsx: Path) -> list[list]:
    try:
        from openpyxl import load_workbook

        ws = load_workbook(str(xlsx)).active
        rows: list[list] = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                continue
            norm = _normalize_fill_row(row)
            if norm:
                rows.append(norm)
        return rows
    except Exception:  # noqa: BLE001
        return []


def _fill_rows_path(root: Path) -> Path:
    """累计行存 storage（docx 只展示，不反读）。"""
    from . import state as content_state

    return content_state.cache_path(root, "fill_rows.json")


def _load_fill_manifest_rows(root: Path) -> list[list]:
    """读 storage 累计；首次把旧 md/xlsx 迁入后以 storage 为准。"""
    import json

    p = _fill_rows_path(root)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            raw = data.get("rows") if isinstance(data, dict) else data
            if isinstance(raw, list):
                out = []
                for item in raw:
                    norm = _normalize_fill_row(item)
                    if norm:
                        out.append(norm)
                return out
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    # 一次性迁移旧交付物 → storage
    migrated: list[list] = []
    xlsx = Path(root) / "成稿清单.xlsx"
    if xlsx.is_file():
        migrated = _parse_legacy_fill_xlsx(xlsx)
    if not migrated:
        md = Path(root) / "_成稿清单.md"
        if md.is_file():
            migrated = _parse_legacy_fill_md(md)
    if migrated:
        _save_fill_manifest_rows(root, migrated)
    return migrated


def _save_fill_manifest_rows(root: Path, rows: list) -> None:
    import json
    import os

    p = _fill_rows_path(root)
    payload = {
        "rows": [list(r)[:3] for r in rows if _normalize_fill_row(r)],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(str(tmp), str(p))
    except OSError:
        try:
            p.write_text(text, encoding="utf-8")
        except OSError:
            pass


def run_template_fill(user_input: str, target_dir, selected_files=None) -> dict:
    work = resolve_work_dir(user_input, Path(target_dir))
    if not work.is_dir():
        return {
            "ok": False,
            "handler": "系统",
            "message": "⚠️ 工作目录无效，无法做模板填槽。",
            "operations": [],
            "tool": "template_fill",
            "reason": "bad_work_dir",
        }

    template, err = _pick_template(work, selected_files)
    if template is None:
        return {
            "ok": False,
            "handler": "系统",
            "message": err,
            "operations": [],
            "tool": "template_fill",
            "reason": "no_template",
        }

    try:
        plain = _template_all_text(template)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "handler": "系统",
            "message": f"打不开模板「{template.name}」：{exc}",
            "operations": [],
            "tool": "template_fill",
            "reason": "bad_template",
        }

    slots = _find_slots(plain)
    if not slots:
        return {
            "ok": False,
            "handler": "系统",
            "message": (
                f"模板「{template.name}」里没有识别到槽位"
                "（支持 {{名称}} / 【名称】 / ｛名称｝；含表格与页眉页脚）。"
            ),
            "operations": [],
            "tool": "template_fill",
            "reason": "no_slots",
        }

    values = _gather_values(user_input, work, template)
    missing = [s for s in slots if s not in values or not str(values.get(s) or "").strip()]
    if missing:
        return {
            "ok": False,
            "handler": "系统",
            "message": (
                "槽位未齐，不瞎填。缺："
                + "、".join(missing)
                + "。\n可在指令里写「槽名=值」，或放一份文件名含「材料」的 txt/docx。"
            ),
            "operations": [],
            "tool": "template_fill",
            "reason": "missing_slots",
            "missing_slots": missing,
            "slots": slots,
        }

    root = work / OUT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = unique_dest(root, f"{safe_filename(template.stem)}_{stamp}.docx")
    try:
        path = _write_filled_copy(template, dest, values)
    except OSError as exc:
        return {
            "ok": False,
            "handler": "系统",
            "message": f"写入成稿失败：{exc}",
            "operations": [],
            "tool": "template_fill",
            "reason": "write_fail",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "handler": "系统",
            "message": f"填槽失败：{exc}",
            "operations": [],
            "tool": "template_fill",
            "reason": "fill_fail",
        }

    report_paths = [path]
    manifest_hint = "成稿清单.docx"
    try:
        headers = ["模板", "槽位", "新文档"]
        prior = _load_fill_manifest_rows(root)
        prior.append([
            template.name,
            ", ".join(slots),
            str(dest.relative_to(work)),
        ])
        _save_fill_manifest_rows(root, prior)
        from export.docx_report import write_table_report

        written = write_table_report(
            root / "成稿清单",
            "模板成稿清单",
            [
                f"工作目录：{work}",
                f"累计成稿：{len(prior)}",
            ],
            headers,
            prior,
            "说明：复制模板填槽另存；原件未改。撤回本轮可删产物。"
            "累计明细在项目 storage，清单仅为展示。",
            max_rows=500,
        )
        for mp in written:
            report_paths.append(str(mp.resolve()))
        if written:
            manifest_hint = "、".join(Path(p).name for p in written)
        legacy_md = root / "_成稿清单.md"
        if legacy_md.is_file():
            try:
                legacy_md.unlink()
            except OSError:
                pass
    except OSError:
        pass

    out_abs = str(root.resolve())
    return {
        "ok": True,
        "handler": "模板成稿",
        "message": (
            f"已按模板「{template.name}」填槽另存（复制模板保留样式；原件未改）。\n"
            f"输出目录：{out_abs}\n"
            f"新文档：{dest.name}\n"
            f"清单：{OUT_ROOT}/{manifest_hint}"
        ),
        "operations": [],
        "tool": "template_fill",
        "reason": "ok",
        "report_paths": report_paths,
        "slots": slots,
        "work_dir": str(work.resolve()),
        "output_dir": out_abs,
    }
