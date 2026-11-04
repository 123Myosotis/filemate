# agent_core/session_state.py
"""任务会话检查点：取消 / 暂停 / 续跑共用。"""
from __future__ import annotations

import gzip
import json
import threading
import time
from pathlib import Path
from typing import Optional


def _state_dir(work_dir: Path) -> Path:
    d = Path(work_dir) / "FileMate报告" / ".session"
    d.mkdir(parents=True, exist_ok=True)
    return d


def checkpoint_path(work_dir: Path, channel_id=None) -> Path:
    name = f"ch_{channel_id}.json" if channel_id is not None else "latest.json"
    return _state_dir(work_dir) / name


def save_checkpoint(work_dir: Path, payload: dict, channel_id=None) -> Path:
    path = checkpoint_path(work_dir, channel_id)
    latest = checkpoint_path(work_dir, None)
    data = dict(payload or {})
    data["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["work_dir"] = str(work_dir)
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    return path


def load_checkpoint(work_dir: Path, channel_id=None) -> Optional[dict]:
    for cand in (checkpoint_path(work_dir, channel_id), checkpoint_path(work_dir, None)):
        if not cand.exists():
            continue
        try:
            data = json.loads(cand.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            continue
    return None


def tables_blob_path(work_dir: Path, channel_id=None) -> Path:
    name = f"ch_{channel_id}.tables.json.gz" if channel_id is not None else "latest.tables.json.gz"
    return _state_dir(work_dir) / name


def save_tables_blob(work_dir: Path, tables: dict, channel_id=None) -> Optional[Path]:
    """把已解析表整表落盘，续跑时不必重扫。"""
    if not tables:
        return None
    slim = {}
    for name, t in tables.items():
        if not isinstance(t, dict):
            continue
        slim[str(name)] = {
            "name": t.get("name") or name,
            "columns": list(t.get("columns") or []),
            "rows": list(t.get("rows") or []),
        }
    if not slim:
        return None
    raw = json.dumps(slim, ensure_ascii=False, default=str).encode("utf-8")
    payload = gzip.compress(raw)
    path = tables_blob_path(work_dir, channel_id)
    latest = tables_blob_path(work_dir, None)
    path.write_bytes(payload)
    latest.write_bytes(payload)
    return path


def load_tables_blob(work_dir: Path, channel_id=None) -> Optional[dict]:
    for cand in (tables_blob_path(work_dir, channel_id), tables_blob_path(work_dir, None)):
        if not cand.exists():
            continue
        try:
            data = json.loads(gzip.decompress(cand.read_bytes()).decode("utf-8"))
            if isinstance(data, dict) and data:
                return data
        except (OSError, json.JSONDecodeError, gzip.BadGzipFile, UnicodeDecodeError):
            continue
    return None


def clear_checkpoint(work_dir: Path, channel_id=None):
    for cand in (
        checkpoint_path(work_dir, channel_id),
        checkpoint_path(work_dir, None),
        tables_blob_path(work_dir, channel_id),
        tables_blob_path(work_dir, None),
    ):
        try:
            if cand.exists():
                cand.unlink()
        except OSError:
            pass


def snapshot_tables(tables: dict) -> dict:
    """轻量快照：只保留列名与行数，避免超大 JSON；完整表仍在内存 ctx。"""
    out = {}
    for name, t in (tables or {}).items():
        if not isinstance(t, dict):
            continue
        out[name] = {
            "name": t.get("name") or name,
            "columns": list(t.get("columns") or []),
            "row_count": len(t.get("rows") or []),
        }
    return out


class TaskControl:
    """线程安全的任务控制：运行 / 取消 / 暂停。"""

    def __init__(self):
        self._cancel = threading.Event()
        self._pause = threading.Event()  # set = paused
        self._lock = threading.Lock()
        self.status = "idle"  # idle|running|paused|cancelled|done

    def reset(self):
        self._cancel.clear()
        self._pause.clear()
        self.status = "running"

    def request_cancel(self):
        self._cancel.set()
        self._pause.clear()
        with self._lock:
            self.status = "cancelled"

    def request_pause(self):
        self._pause.set()
        with self._lock:
            if not self._cancel.is_set():
                self.status = "paused"

    def request_resume(self):
        self._pause.clear()
        with self._lock:
            if not self._cancel.is_set():
                self.status = "running"

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def wait_if_paused(self, tick: float = 0.2) -> bool:
        """暂停时阻塞；若已取消返回 False。"""
        while self._pause.is_set():
            if self._cancel.is_set():
                return False
            time.sleep(tick)
        return not self._cancel.is_set()

    def checkpoint(self) -> bool:
        """每个任务步前调用：处理暂停；返回是否可继续。"""
        if self._cancel.is_set():
            return False
        return self.wait_if_paused()
