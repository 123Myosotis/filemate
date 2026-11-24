# -*- coding: utf-8 -*-
"""内容成稿族共用：工作目录 / Word 列举 / 不覆盖另存名。"""
from __future__ import annotations

import os
import re
from pathlib import Path

_DESKTOP = re.compile(r"桌面")

# 现代格式可直接 zip 解析；.doc 需转换或明确跳过
WORD_EXTS = {".docx", ".docm"}
LEGACY_WORD_EXTS = {".doc"}
ALL_WORD_EXTS = WORD_EXTS | LEGACY_WORD_EXTS


def resolve_work_dir(user_input: str, fallback: Path) -> Path:
    """用户点名「桌面」时尽量落到真实桌面；否则用频道工作区。"""
    fb = Path(fallback)
    if not _DESKTOP.search(str(user_input or "")):
        return fb
    candidates = []
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        )
        value, _ = winreg.QueryValueEx(key, "Desktop")
        winreg.CloseKey(key)
        expanded = Path(os.path.expandvars(str(Path(str(value)).expanduser())))
        candidates.append(expanded)
    except Exception:  # noqa: BLE001
        pass
    candidates.extend([
        Path.home() / "Desktop",
        Path.home() / "OneDrive" / "Desktop",
        Path.home() / "OneDrive" / "桌面",
    ])
    for c in candidates:
        try:
            if c.is_dir():
                return c.resolve()
        except OSError:
            continue
    return fb


def unique_dest(folder: Path, name: str) -> Path:
    dest = folder / name
    if not dest.exists():
        return dest
    stem, suf = Path(name).stem, Path(name).suffix
    for i in range(1, 200):
        cand = folder / f"{stem}_{i}{suf}"
        if not cand.exists():
            return cand
    return folder / f"{stem}_dup{suf}"


def list_top_docx(work_dir: Path, *, exclude_dir_names: tuple[str, ...] = ()) -> list[Path]:
    return list_docx(
        work_dir, depth=0, exclude_dir_names=exclude_dir_names,
    )


def list_docx(
    work_dir: Path,
    *,
    depth: int = 0,
    exclude_dir_names: tuple[str, ...] = (),
    exts: set[str] | None = None,
) -> list[Path]:
    """列举 Word：默认 .docx/.docm/.doc。depth=0 顶层；1=顶层+一层子目录。"""
    root = Path(work_dir)
    out: list[Path] = []
    exclude = set(exclude_dir_names or ())
    want = {e.lower() for e in (exts or ALL_WORD_EXTS)}

    def _ok_file(p: Path) -> bool:
        if not p.is_file():
            return False
        if p.name.startswith("~$") or p.name.startswith("."):
            return False
        if p.suffix.lower() not in want:
            return False
        try:
            if any(part in exclude for part in p.relative_to(root).parts):
                return False
        except ValueError:
            if any(part in exclude for part in p.parts):
                return False
        return True

    try:
        for p in sorted(root.iterdir()):
            if _ok_file(p):
                out.append(p)
            elif depth >= 1 and p.is_dir() and not p.name.startswith("."):
                if p.name in exclude:
                    continue
                try:
                    for c in sorted(p.iterdir()):
                        if _ok_file(c):
                            out.append(c)
                except OSError:
                    continue
    except OSError:
        return []
    return out


def safe_filename(title: str, fallback: str = "片段") -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(title or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .") or fallback
    return s[:60]
