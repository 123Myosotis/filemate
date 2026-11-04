# -*- coding: utf-8 -*-
"""Scope as first-class: file > sheet > region > catalog_id > cell_coord."""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class Scope:
    file_id: str
    sheet: str
    region: str = ""
    catalog_id: str = ""
    cell_coord: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {
            "file_id": self.file_id,
            "sheet": self.sheet,
            "region": self.region,
            "catalog_id": self.catalog_id,
            "cell_coord": self.cell_coord,
        }

    def key_upto(self, level: str) -> Tuple[str, ...]:
        order = ("file_id", "sheet", "region", "catalog_id", "cell_coord")
        if level not in order:
            raise ValueError("unknown scope level: %s" % level)
        out = []
        for name in order:
            out.append(getattr(self, name))
            if name == level:
                break
        return tuple(out)

    def is_complete_upto(self, level: str) -> bool:
        """Every field up to level must be non-empty string."""
        order = ("file_id", "sheet", "region", "catalog_id", "cell_coord")
        if level not in order:
            return False
        for name in order:
            val = str(getattr(self, name) or "").strip()
            if not val:
                return False
            if name == level:
                return True
        return True


def parse_scope(obj: Any, require_upto: str = "sheet") -> Optional[Scope]:
    """
    Parse scope from dict. Missing required fields → None (caller: contract incomplete).
    Does not invent defaults for file/sheet/region.
    """
    if not isinstance(obj, dict):
        return None
    file_id = str(obj.get("file_id") or "").strip()
    sheet = str(obj.get("sheet") or "").strip()
    region = str(obj.get("region") or "").strip()
    catalog_id = str(obj.get("catalog_id") or "").strip()
    cell_coord = str(obj.get("cell_coord") or "").strip()
    scope = Scope(
        file_id=file_id,
        sheet=sheet,
        region=region,
        catalog_id=catalog_id,
        cell_coord=cell_coord,
    )
    if not scope.is_complete_upto(require_upto):
        return None
    return scope


def scope_covers(outer: Scope, inner: Scope, level: str = "region") -> bool:
    """
    Mechanical containment: outer and inner share the same prefix up to `level`,
    and outer's fields at deeper levels are either empty (wildcard) or equal.
    """
    order = ("file_id", "sheet", "region", "catalog_id", "cell_coord")
    if level not in order:
        return False
    for name in order:
        ov = str(getattr(outer, name) or "").strip()
        iv = str(getattr(inner, name) or "").strip()
        if name == level:
            return bool(ov) and ov == iv
        if not ov or not iv or ov != iv:
            return False
    return True


def scopes_disjoint(a: Scope, b: Scope, level: str = "region") -> bool:
    """True iff scopes differ at or before `level` (no shared identity at that level)."""
    order = ("file_id", "sheet", "region", "catalog_id", "cell_coord")
    if level not in order:
        return True
    for name in order:
        av = str(getattr(a, name) or "").strip()
        bv = str(getattr(b, name) or "").strip()
        if name == level:
            return av != bv
        if av != bv:
            return True
    return False


def scopes_equal_upto(a: Scope, b: Scope, level: str) -> bool:
    return a.key_upto(level) == b.key_upto(level)
