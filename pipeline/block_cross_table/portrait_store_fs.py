# -*- coding: utf-8 -*-
"""I 接线：画像切片文件系统持久化（替换 InMemory；满足整表停后保留）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional

from pipeline.block_cross_table.portrait_write import PortraitWriteRejected


def _safe_block_name(block_id: Any) -> str:
    text = str(block_id)
    if text == "":
        raise PortraitWriteRejected("block_id empty for portrait path")
    # 文件系统安全：仅替换路径分隔与空字符
    return (
        text.replace("/", "_")
        .replace("\\", "_")
        .replace("\0", "_")
    )


class FilePortraitStore:
    """按 run 目录持久化：slices/{block}.json + manifest.json。

    discard_all 删除切片文件但保留目录，供回滚到 A'/④ 后重写。
    """

    def __init__(self, root: str | Path, *, clear: bool = False) -> None:
        self.root = Path(root)
        self.slices_dir = self.root / "slices"
        self.manifest_path = self.root / "manifest.json"
        self.slices_dir.mkdir(parents=True, exist_ok=True)
        if clear:
            self.discard_all_slices()
        elif not self.manifest_path.exists():
            self._write_manifest({"written_block_ids": []})

    def _slice_path(self, block_id: Any) -> Path:
        return self.slices_dir / ("%s.json" % _safe_block_name(block_id))

    def _read_manifest(self) -> dict[str, Any]:
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"written_block_ids": []}
        if not isinstance(data, dict):
            return {"written_block_ids": []}
        return data

    def _write_manifest(self, data: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(dict(data), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def write_slice(self, block_id: Any, slice_data: Mapping[str, Any]) -> None:
        path = self._slice_path(block_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(dict(slice_data), ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except OSError as exc:
            raise PortraitWriteRejected(str(exc)) from exc
        if path.stat().st_size == 0:
            raise PortraitWriteRejected("empty portrait slice file")
        manifest = self._read_manifest()
        ids = list(manifest.get("written_block_ids") or [])
        key = str(block_id)
        if key not in ids:
            ids.append(key)
        manifest["written_block_ids"] = ids
        self._write_manifest(manifest)

    def get(self, block_id: Any) -> Optional[dict[str, Any]]:
        path = self._slice_path(block_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    def written_block_ids(self) -> list[str]:
        return list(self._read_manifest().get("written_block_ids") or [])

    def discard_all_slices(self) -> None:
        """回滚到 A' / ④ / 布局：丢弃各块画像切片。"""
        if self.slices_dir.is_dir():
            for path in self.slices_dir.glob("*.json"):
                try:
                    path.unlink()
                except OSError:
                    pass
        self._write_manifest({"written_block_ids": []})

    def discard_block(self, block_id: Any) -> None:
        path = self._slice_path(block_id)
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass
        manifest = self._read_manifest()
        ids = [x for x in (manifest.get("written_block_ids") or []) if str(x) != str(block_id)]
        manifest["written_block_ids"] = ids
        self._write_manifest(manifest)
