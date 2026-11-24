# -*- coding: utf-8 -*-
"""通用写文件事务：一份快照 → 应用 → 失败整包还原。

与 `word_office.txn` 逻辑一致，唯一区别是备份目录按 tool 拆分：
`storage/backups/<tool>/`，避免不同格式的清理策略互相影响。
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

KEEP = 20
ROOT = Path(__file__).resolve().parents[2]


def backup_root(tool: str) -> Path:
    """按业务线分目录：storage/backups/word、storage/backups/text。"""
    name = str(tool or "misc").strip() or "misc"
    return ROOT / "storage" / "backups" / name


@dataclass
class Snapshot:
    path: Path
    archive: Path


def _prune(root: Path, filename: str) -> None:
    """同名文件只保留最近 KEEP 份，防止备份无限增长。"""
    try:
        files = [f for f in root.iterdir()
                 if f.is_file() and f.name.split("_", 1)[-1] == filename]
    except OSError:
        return
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass


def begin(tool: str, path: Path) -> Snapshot:
    """写前快照：独立目录一份（权威）+ 同目录 .bak（给旧调用方看）。"""
    path = Path(path)
    root = backup_root(tool)
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    ms = int((time.time() % 1) * 1000)
    archive = root / ("%s-%03d_%s" % (stamp, ms, path.name))
    n = 0
    while archive.exists():
        n += 1
        archive = root / ("%s-%03d-%d_%s" % (stamp, ms, n, path.name))
    shutil.copy2(path, archive)
    sidecar = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, sidecar)
    _prune(root, path.name)
    return Snapshot(path=path, archive=archive)


def restore(snap: Snapshot) -> None:
    if not snap or not Path(snap.archive).is_file():
        raise FileNotFoundError("snapshot missing")
    shutil.copy2(snap.archive, snap.path)


def try_restore(snap: Snapshot | None) -> tuple[bool, str]:
    if snap is None:
        return False, "没有快照"
    try:
        restore(snap)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
