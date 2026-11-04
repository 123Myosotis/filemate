# -*- coding: utf-8 -*-
"""
Lightweight system directory picker for local Web (M5.3).

Default backend: tkinter.filedialog.askdirectory (injectable for tests).
Does not touch MaiBot DB; only returns a path string.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

AskDirectoryFn = Callable[[], str | None]


def _default_ask_directory() -> str | None:
    """Open OS folder dialog; return absolute path or None if cancelled."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        chosen = filedialog.askdirectory(mustexist=True, title="选择 MaiBot 数据目录")
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    text = str(chosen or "").strip()
    return text or None


def pick_directory(*, ask_directory_fn: AskDirectoryFn | None = None) -> dict:
    """
    Run folder picker.
    Returns {status, path?, cancelled?}.
    """
    ask = ask_directory_fn if ask_directory_fn is not None else _default_ask_directory
    try:
        raw = ask()
    except Exception as exc:
        return {
            "status": "error",
            "cancelled": False,
            "path": None,
            "error": str(exc),
        }
    if raw is None or not str(raw).strip():
        return {"status": "cancelled", "cancelled": True, "path": None}
    path = str(Path(str(raw).strip()).expanduser().resolve())
    return {"status": "ok", "cancelled": False, "path": path}


def validate_maibot_data_dir(path: str) -> dict:
    """
    Check whether configured path resolves to a MaiBot DB (read-only discovery).
    Empty path → valid=False, reason empty.
    """
    from pipeline.block_cross_table.memory_rag.maibot_ro import discover_maibot_db_path

    cleaned = str(path or "").strip()
    if not cleaned:
        return {
            "path": "",
            "valid": False,
            "db_found": False,
            "db_path": None,
            "message": "",
        }
    p = Path(cleaned).expanduser()
    if not p.exists():
        return {
            "path": cleaned,
            "valid": False,
            "db_found": False,
            "db_path": None,
            "message": "未找到数据库文件",
        }
    db = discover_maibot_db_path(cleaned)
    if db is None:
        return {
            "path": cleaned,
            "valid": False,
            "db_found": False,
            "db_path": None,
            "message": "未找到数据库文件",
        }
    return {
        "path": cleaned,
        "valid": True,
        "db_found": True,
        "db_path": str(db),
        "message": "",
    }
