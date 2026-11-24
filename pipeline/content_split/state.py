# -*- coding: utf-8 -*-
"""内容分类的「目录身份 + 项目内状态」。

设计：
- 输出目录里只放隐藏 `.filemate/id`（UUID，几字节）；
- 索引与标签缓存放项目 `storage/content_index/<uuid>/`；
- 目录改名/移动 → UUID 跟随；
- 目录被复制 → 索引 root 不一致 → 换新 UUID 重建；
- 状态丢失/损坏 → 从现存文件重建，不重复生成。
环境变量 FILEMATE_CONTENT_STATE_DIR 可改 storage 根（测试隔离）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

ID_DIR = ".filemate"
ID_FILE = "id"
_LEGACY_INDEX = "_content_hash_index.json"
_LEGACY_CACHE = "_label_cache.json"
_IN_DIR_INDEX = "hash_index.json"
_IN_DIR_CACHE = "label_cache.json"
_LEGACY_MD = "_分类清单.md"


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def state_root() -> Path:
    env = os.environ.get("FILEMATE_CONTENT_STATE_DIR")
    root = Path(env) if env else (project_root() / "storage" / "content_index")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _hide(path: Path) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)
    except Exception:  # noqa: BLE001
        pass


def _read_id(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return value if re.fullmatch(r"[0-9a-f]{32}", value) else ""


def dir_id(root_out: Path, *, rotate: bool = False) -> str:
    """取（或创建）输出目录 UUID；rotate=True 强制换新。"""
    folder = Path(root_out) / ID_DIR
    id_file = folder / ID_FILE
    if not rotate:
        value = _read_id(id_file)
        if value:
            return value
    folder.mkdir(parents=True, exist_ok=True)
    _hide(folder)
    value = uuid.uuid4().hex
    try:
        tmp = id_file.with_suffix(".tmp")
        tmp.write_text(value, encoding="utf-8")
        os.replace(str(tmp), str(id_file))
    except OSError:
        try:
            id_file.write_text(value, encoding="utf-8")
        except OSError:
            pass
    return value


def index_path(root_out: Path) -> Path:
    _migrate_legacy(root_out)
    d = state_root() / dir_id(root_out)
    d.mkdir(parents=True, exist_ok=True)
    return d / "index.json"


def cache_path(root_out: Path, name: str = "label_cache.json") -> Path:
    _migrate_legacy(root_out)
    d = state_root() / dir_id(root_out)
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def _norm(path: Path) -> str:
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except OSError:
        return os.path.normcase(str(path))


def _migrate_legacy(root_out: Path) -> None:
    """旧位置状态 → storage；删旧文件与旧 Markdown 清单。"""
    root_out = Path(root_out)
    if not root_out.is_dir():
        return
    idv = dir_id(root_out)
    target_dir = state_root() / idv
    target_dir.mkdir(parents=True, exist_ok=True)

    pairs = [
        (root_out / ID_DIR / _IN_DIR_INDEX, target_dir / "index.json"),
        (root_out / _LEGACY_INDEX, target_dir / "index.json"),
        (root_out / ID_DIR / _IN_DIR_CACHE, target_dir / "label_cache.json"),
        (root_out / _LEGACY_CACHE, target_dir / "label_cache.json"),
    ]
    for src, dst in pairs:
        if not src.is_file():
            continue
        if dst.exists():
            try:
                src.unlink()
            except OSError:
                pass
            continue
        try:
            src.replace(dst)
        except OSError:
            pass

    # 仅当像本产品旧清单时才删，避免误删用户同名文件
    legacy_md = root_out / _LEGACY_MD
    if legacy_md.is_file():
        try:
            head = legacy_md.read_text(encoding="utf-8", errors="ignore")[:800]
        except OSError:
            head = ""
        if (
            "内容分类" in head
            or "得分 top2" in head
            or "本轮：" in head
            or "指纹" in head
        ):
            try:
                legacy_md.unlink()
            except OSError:
                pass


def _dir_bytes(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


# root 缺失时的宽限：避免「刚移动 / 短暂离线」被清掉，破坏 UUID 跟随
ORPHAN_MISSING_ROOT_DAYS = 7


def _drive_disconnected(path: Path) -> bool:
    """盘符不存在/未就绪（移动硬盘拔出等）→ True，永不因 root 缺失删除。"""
    drive = Path(path).drive
    if not drive:
        return False
    try:
        return not Path(drive + "\\").exists()
    except OSError:
        return True


def _index_age_days(uuid_dir: Path) -> float:
    idx = uuid_dir / "index.json"
    try:
        target = idx if idx.is_file() else uuid_dir
        return max(0.0, (time.time() - target.stat().st_mtime) / 86400.0)
    except OSError:
        return float(ORPHAN_MISSING_ROOT_DAYS + 1)


def _orphan_verdict(uuid_dir: Path) -> str:
    """
    keep    — 仍有效
    remove  — 可删（id 已换；或无索引；或 root 缺失且超宽限且盘符在线）
    defer   — root 缺失但受保护（未满 N 天 / 盘符断开），只报告不删
    """
    if not re.fullmatch(r"[0-9a-f]{32}", uuid_dir.name):
        return "keep"
    idx = uuid_dir / "index.json"
    if not idx.is_file():
        return "remove"
    try:
        data = json.loads(idx.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "remove"
    if not isinstance(data, dict):
        return "remove"
    recorded = data.get("root")
    if not recorded:
        return "remove"
    root = Path(str(recorded))
    try:
        alive = root.is_dir()
    except OSError:
        alive = False
    if alive:
        live_id = _read_id(root / ID_DIR / ID_FILE)
        return "keep" if live_id == uuid_dir.name else "remove"
    # root 不存在：盘符断开永远保留；否则满 N 天才删
    if _drive_disconnected(root):
        return "defer"
    if _index_age_days(uuid_dir) >= ORPHAN_MISSING_ROOT_DAYS:
        return "remove"
    return "defer"


def cleanup_orphan_indexes(*, dry_run: bool = False) -> dict:
    """
    清理 storage/content_index 孤儿。
    - root 在且 id 不匹配 → 删
    - root 缺失：盘符断开 → 永不删；未满 7 天 → 只报告；满 7 天 → 删
    不自动跑；聊天需「确认清理…」，设置菜单二次确认。
    """
    root = state_root()
    removed: list[str] = []
    kept: list[str] = []
    deferred: list[str] = []
    freed = 0
    try:
        children = list(root.iterdir())
    except OSError:
        children = []
    for d in children:
        if not d.is_dir() or not re.fullmatch(r"[0-9a-f]{32}", d.name):
            continue
        verdict = _orphan_verdict(d)
        if verdict == "keep":
            kept.append(d.name)
            continue
        if verdict == "defer":
            deferred.append(d.name)
            continue
        size = _dir_bytes(d)
        if not dry_run:
            try:
                shutil.rmtree(d)
            except OSError:
                kept.append(d.name)
                continue
        removed.append(d.name)
        freed += size
    return {
        "ok": True,
        "removed": removed,
        "kept": kept,
        "deferred": deferred,
        "freed_bytes": freed,
        "dry_run": bool(dry_run),
        "missing_root_grace_days": ORPHAN_MISSING_ROOT_DAYS,
        "state_root": str(root),
    }


def load_index(root_out: Path) -> dict | None:
    """
    读索引。root 不一致（目录被复制）→ 换 UUID 并返回 None（由调用方重建）。
    JSON 损坏 → 改名留档并返回 None。
    """
    p = index_path(root_out)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        try:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            p.replace(p.with_name(f"index.bad_{stamp}.json"))
        except OSError:
            pass
        return None
    if not isinstance(data, dict):
        return None

    recorded = data.get("root")
    if recorded and _norm(Path(str(recorded))) != _norm(Path(root_out)):
        # 复制：旧路径仍在且挂着同一 UUID → 换新身份重建
        # 改名/移动：旧路径已不在（或 id 已变）→ 更新 root，索引继续有效
        old_root = Path(str(recorded))
        same_id = False
        try:
            if old_root.is_dir():
                same_id = _read_id(old_root / ID_DIR / ID_FILE) == _read_id(
                    Path(root_out) / ID_DIR / ID_FILE
                )
        except OSError:
            same_id = False
        if same_id:
            dir_id(root_out, rotate=True)
            return None
        data["root"] = str(Path(root_out).resolve())
        save_index(root_out, data)
        return data
    if not recorded:
        data["root"] = str(Path(root_out).resolve())
        save_index(root_out, data)
    return data


def save_index(root_out: Path, payload: dict) -> None:
    """写入时补 root；临时文件 + 原子替换。"""
    p = index_path(root_out)
    data = dict(payload or {})
    data["root"] = str(Path(root_out).resolve())
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(str(tmp), str(p))
    except OSError:
        try:
            p.write_text(text, encoding="utf-8")
        except OSError:
            pass
