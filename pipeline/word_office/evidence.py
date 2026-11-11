# -*- coding: utf-8 -*-
"""Evidence（本地证据）：每次执行后写一条 JSON（gzip），30 天 / 1000 条自动清理。

**本地 only**（不上传）；只记 `{ts, user_input, tier, file, ops, before/after, status}`。
"""
from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "storage" / "evidence" / "word"


def _redact(event: dict) -> dict:
    """**RedactionPolicy**：落盘前抹掉敏感字段（默认 api_key / authorization）。"""
    try:
        from .objects import RedactionPolicy
        masks = {m.lower() for m in RedactionPolicy().mask_fields}
    except Exception:  # noqa: BLE001
        masks = {"api_key", "authorization"}
    out = {}
    for k, v in (event or {}).items():
        if str(k).lower() in masks:
            out[k] = "<redacted>"
        else:
            out[k] = v
    return out


def write_evidence(event: dict) -> str:
    DIR.mkdir(parents=True, exist_ok=True)
    ts = "%s-%09d" % (time.strftime("%Y%m%d-%H%M%S"), time.time_ns() % 1_000_000_000)
    path = DIR / (ts + ".json.gz")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(_redact(event), f, ensure_ascii=False, default=str)
    _cleanup()
    return str(path)


def _cleanup(days: int = 30, keep: int = 1000) -> None:
    try:
        files = sorted(DIR.glob("*.json.gz"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    cutoff = time.time() - days * 86400
    for p in files[:-keep]:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass
