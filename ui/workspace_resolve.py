# -*- coding: utf-8 -*-
"""工作区内文内文件名解析：唯一命中才锁定；多命中不猜。

接入层只扩 file_paths 来源；不猜 tool / 不扫工作区外。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# 常见业务后缀；无后缀的裸词不解析（防「整理」「合同」等口语误锁）
_EXTS = (
    "docx", "doc", "xlsx", "xls", "xlsm", "csv", "pdf", "txt", "md",
    "pptx", "ppt", "zip", "png", "jpg", "jpeg", "gif",
)
_EXT_RE = "|".join(_EXTS)

# 「合同.docx」 "a.xlsx" 合同.docx  ./子目录/a.docx
_TOKEN_RE = re.compile(
    r"[「\"']([^「」\"'\n]+?\.(?:%s))[」\"']"
    r"|(?<![^\s，,。；;：:\u3000])"
    r"((?:[\w\u4e00-\u9fff.\-]+/)*[\w\u4e00-\u9fff.\-]+\.(?:%s))"
    r"(?![^\s，,。；;：:\u3000])"
    % (_EXT_RE, _EXT_RE),
    re.IGNORECASE,
)

_SKIP_DIR_NAMES = {
    ".git", ".svn", "__pycache__", ".venv", "venv", "node_modules",
    ".pytest_cache", "$RECYCLE.BIN", "System Volume Information",
}


@dataclass
class NameResolveResult:
    hits: list[str] = field(default_factory=list)  # 唯一锁定的绝对路径
    ambiguous: dict[str, list[str]] = field(default_factory=dict)  # 名 → 多路径
    missing: list[str] = field(default_factory=list)  # 工作区内找不到
    tokens: list[str] = field(default_factory=list)  # 抽到的候选名


def extract_filename_tokens(text: str) -> list[str]:
    """从指令里抽出带后缀的文件名/相对路径；去重保序。"""
    blob = str(text or "")
    out: list[str] = []
    seen: set[str] = set()
    for m in _TOKEN_RE.finditer(blob):
        tok = (m.group(1) or m.group(2) or "").strip().strip("/\\")
        if not tok:
            continue
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
    return out


def _index_workspace(root: Path, *, max_files: int = 8000) -> dict[str, list[Path]]:
    """文件名(小写) → 路径列表；顺带索引相对路径(小写)。"""
    by_name: dict[str, list[Path]] = {}
    n = 0
    root = root.resolve()
    for dirpath, dirnames, filenames in os_walk_safe(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES and not d.startswith(".")]
        for name in filenames:
            if n >= max_files:
                return by_name
            p = Path(dirpath) / name
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue
            n += 1
            key = name.lower()
            by_name.setdefault(key, []).append(p)
            try:
                rel = p.relative_to(root).as_posix().lower()
            except ValueError:
                continue
            if rel != key:
                by_name.setdefault(rel, []).append(p)
    return by_name


def os_walk_safe(root: Path):
    import os
    try:
        yield from os.walk(str(root))
    except OSError:
        return


def resolve_names_in_workspace(
    workspace: str | Path,
    text: str,
    *,
    max_files: int = 8000,
) -> NameResolveResult:
    """只在工作区内解析。唯一命中 → hits；多个 → ambiguous；没有 → missing。"""
    result = NameResolveResult()
    tokens = extract_filename_tokens(text)
    result.tokens = tokens
    if not tokens:
        return result
    root = Path(workspace)
    try:
        if not root.is_dir():
            result.missing = list(tokens)
            return result
        root = root.resolve()
    except OSError:
        result.missing = list(tokens)
        return result

    index = _index_workspace(root, max_files=max_files)
    hits: list[str] = []
    seen_hit: set[str] = set()

    for tok in tokens:
        # 1) 相对路径直接拼
        direct = (root / tok.replace("\\", "/")).resolve()
        try:
            if direct.is_file() and _is_under(direct, root):
                key = str(direct)
                if key not in seen_hit:
                    seen_hit.add(key)
                    hits.append(key)
                continue
        except OSError:
            pass

        key = tok.replace("\\", "/").lower()
        cands = list(index.get(key) or [])
        # 2) 仅文件名
        if not cands:
            cands = list(index.get(Path(tok).name.lower()) or [])

        # 去重
        uniq: list[Path] = []
        seen_p: set[str] = set()
        for p in cands:
            try:
                rp = str(p.resolve())
            except OSError:
                continue
            if rp in seen_p:
                continue
            seen_p.add(rp)
            uniq.append(p)

        if len(uniq) == 1:
            rp = str(uniq[0].resolve())
            if rp not in seen_hit:
                seen_hit.add(rp)
                hits.append(rp)
        elif len(uniq) > 1:
            result.ambiguous[tok] = [str(p.resolve()) for p in uniq[:8]]
        else:
            result.missing.append(tok)

    result.hits = hits
    return result


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def format_ambiguous_message(result: NameResolveResult) -> str:
    """多命中时给用户看的说明（不自动选）。"""
    lines = [
        "工作区内文件名不唯一，我不会猜。请「附加」精确文件，或写成相对路径（如 子目录/合同.docx）：",
        "",
    ]
    for name, paths in (result.ambiguous or {}).items():
        lines.append("「%s」命中 %d 个：" % (name, len(paths)))
        for p in paths:
            lines.append("  · " + p)
        lines.append("")
    return "\n".join(lines).rstrip()
