# -*- coding: utf-8 -*-
"""Word 写文件事务（规格 L3 最小版）。

一份快照 → 应用 → 失败则整包还原。不是数据库事务，不是 v4 两阶段提交。
记忆不走这里（记忆不碰文件）。
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

KEEP = 20


def backup_root() -> Path:
    return Path(__file__).resolve().parents[2] / "storage" / "backups" / "word"


@dataclass
class Snapshot:
    path: Path
    archive: Path


def _prune(root: Path, filename: str) -> None:
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


def begin(path: Path) -> Snapshot:
    """写前快照：独立目录一份（权威）+ 同目录 .bak（给旧调用方看）。"""
    path = Path(path)
    root = backup_root()
    root.mkdir(parents=True, exist_ok=True)
    # 毫秒 + 序号，避免同一秒内链上两步互相覆盖归档
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


def restore_file(src: str | Path, dst: str | Path) -> None:
    shutil.copy2(src, dst)
