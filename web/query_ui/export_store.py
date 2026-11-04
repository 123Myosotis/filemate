# -*- coding: utf-8 -*-
"""Ephemeral Excel export files with TTL cleanup (mechanical)."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any


def _ttl_seconds() -> int:
    raw = os.environ.get("QUERY_UI_EXPORT_TTL_S")
    if raw is not None and str(raw).strip() != "":
        try:
            return max(60, int(str(raw).strip()))
        except ValueError:
            pass
    return 3600


def _root_dir() -> Path:
    env = os.environ.get("QUERY_UI_EXPORT_DIR")
    if env and str(env).strip():
        p = Path(str(env).strip())
    else:
        p = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") / "filemate_query_ui_exports"
    p.mkdir(parents=True, exist_ok=True)
    return p


_LOCK = threading.Lock()


def cleanup_expired(*, now: float | None = None) -> int:
    """Delete export dirs older than TTL. Returns removed count."""
    root = _root_dir()
    ttl = float(_ttl_seconds())
    clock = float(now if now is not None else time.time())
    removed = 0
    with _LOCK:
        for child in list(root.iterdir()):
            if not child.is_dir():
                continue
            meta = child / "meta.json"
            try:
                if meta.is_file():
                    data = json.loads(meta.read_text(encoding="utf-8"))
                    created = float(data.get("created_at") or 0)
                else:
                    created = child.stat().st_mtime
            except Exception:
                created = 0.0
            if created <= 0 or (clock - created) > ttl:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
    return removed


def save_result_xlsx(
    columns: list[str],
    rows: list[dict[str, Any]],
    *,
    filename_stem: str | None = None,
) -> dict[str, Any]:
    """Write rows to a temp xlsx; return {export_id, download_path, expires_in_s}."""
    from openpyxl import Workbook

    cleanup_expired()
    export_id = uuid.uuid4().hex
    folder = _root_dir() / export_id
    folder.mkdir(parents=True, exist_ok=True)
    stem = (filename_stem or "query_result").strip() or "query_result"
    # sanitize stem to path-safe without hardcoding business names
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)[:80]
    xlsx_path = folder / ("%s.xlsx" % safe)
    wb = Workbook()
    ws = wb.active
    ws.title = "result"
    cols = [str(c) for c in columns]
    if cols:
        ws.append(cols)
    for row in rows:
        if not isinstance(row, dict):
            continue
        ws.append([row.get(c) for c in cols])
    wb.save(xlsx_path)
    created = time.time()
    meta = {
        "export_id": export_id,
        "created_at": created,
        "ttl_s": _ttl_seconds(),
        "path": str(xlsx_path),
        "filename": xlsx_path.name,
    }
    (folder / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "export_id": export_id,
        "filename": xlsx_path.name,
        "expires_in_s": _ttl_seconds(),
        "download_url": "/api/query/download/%s" % export_id,
    }


def resolve_export_file(export_id: str) -> Path | None:
    cleanup_expired()
    eid = str(export_id or "").strip()
    if not eid or any(ch in eid for ch in ("/", "\\", "..")):
        return None
    folder = _root_dir() / eid
    meta_path = folder / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    created = float(meta.get("created_at") or 0)
    if created <= 0 or (time.time() - created) > float(_ttl_seconds()):
        shutil.rmtree(folder, ignore_errors=True)
        return None
    path = Path(str(meta.get("path") or ""))
    if not path.is_file():
        # fallback: first xlsx in folder
        cands = list(folder.glob("*.xlsx"))
        if not cands:
            return None
        path = cands[0]
    return path
