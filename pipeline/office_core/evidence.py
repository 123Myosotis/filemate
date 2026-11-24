# -*- coding: utf-8 -*-
"""通用本地证据：每次执行后写一条 JSON（gzip），按 tool 分目录。

只记 `{ts, tool, user_input, file, ops, before, status}`；
本地 only，不上传；30 天 / 1000 条自动清理。
"""
from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_MASKS = {"api_key", "authorization"}


def evidence_dir(tool: str) -> Path:
    name = str(tool or "misc").strip() or "misc"
    return ROOT / "storage" / "evidence" / name


def _redact(event: dict) -> dict:
    out = {}
    for k, v in (event or {}).items():
        out[k] = "<redacted>" if str(k).lower() in _MASKS else v
    return out


def write_evidence(tool: str, event: dict) -> str:
    folder = evidence_dir(tool)
    folder.mkdir(parents=True, exist_ok=True)
    ts = "%s-%09d" % (time.strftime("%Y%m%d-%H%M%S"), time.time_ns() % 1_000_000_000)
    path = folder / (ts + ".json.gz")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(_redact(event), f, ensure_ascii=False, default=str)
    _cleanup(folder)
    return str(path)


def _cleanup(folder: Path, days: int = 30, keep: int = 1000) -> None:
    try:
        files = sorted(folder.glob("*.json.gz"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    cutoff = time.time() - days * 86400
    for p in files[:-keep]:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass
