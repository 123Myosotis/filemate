# -*- coding: utf-8 -*-
"""Contract version library: load / hash / membership. No business heuristics."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


def resolve_contract_dir(explicit: Optional[str] = None) -> Path:
    """Resolve version library root. Priority: arg → env → package-relative."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("FILEMATE_CONTRACT_DIR")
    if env:
        return Path(env)
    # pipeline/synth_contract/../../contracts
    here = Path(__file__).resolve().parent
    return here.parent.parent / "contracts"


@dataclass(frozen=True)
class ContractVersion:
    version: str
    path: Path
    file_hash: str
    data: Dict[str, Any]

    def enum_values(self, name: str) -> Set[str]:
        enums = self.data.get("enums") or {}
        vals = enums.get(name)
        if not isinstance(vals, list):
            return set()
        return {str(v) for v in vals}

    def accepts_enum(self, name: str, value: str) -> bool:
        """Membership in current enum set, or deprecated still inside compat window."""
        v = str(value)
        if v in self.enum_values(name):
            return True
        deprecated = (self.data.get("deprecated_enum_values") or {}).get(name) or {}
        if not isinstance(deprecated, dict):
            return False
        meta = deprecated.get(v)
        if not isinstance(meta, dict):
            return False
        until = str(meta.get("accept_until_version") or "")
        if not until:
            return False
        # version order: library lists versions; compare by published path order later
        return _version_leq(self.version, until)

    def confidence_threshold(self, judgment_type: Optional[str] = None) -> Optional[float]:
        """Return threshold from version file. None if missing (caller treats as incomplete)."""
        block = self.data.get("confidence_thresholds")
        if not isinstance(block, dict):
            return None
        by = block.get("by_judgment_type") or {}
        if judgment_type and judgment_type in by:
            try:
                return float(by[judgment_type])
            except (TypeError, ValueError):
                return None
        if "default" not in block:
            return None
        try:
            return float(block["default"])
        except (TypeError, ValueError):
            return None

    def forbid_degenerate_confidence(self) -> bool:
        block = self.data.get("confidence_thresholds") or {}
        return bool(block.get("forbid_degenerate"))

    def degenerate_confidence_values(self) -> Set[float]:
        block = self.data.get("confidence_thresholds") or {}
        raw = block.get("degenerate_values") or []
        out = set()
        for x in raw:
            try:
                out.add(float(x))
            except (TypeError, ValueError):
                continue
        return out

    def state_by_id(self, state_id: str) -> Optional[Dict[str, Any]]:
        for row in self.data.get("states") or []:
            if isinstance(row, dict) and str(row.get("state_id")) == state_id:
                return row
        return None

    def review_min_fields_baseline(self) -> List[str]:
        raw = self.data.get("review_min_fields_baseline") or []
        return [str(x) for x in raw]

    def source_object_type(self, source_type: str) -> Optional[str]:
        mapping = (self.data.get("enums") or {}).get("source_type_object") or {}
        if not isinstance(mapping, dict):
            return None
        v = mapping.get(source_type)
        return str(v) if v is not None else None


def _version_leq(a: str, b: str) -> bool:
    """Mechanical version compare: strip leading v, split on '.', numeric then text."""
    def parts(s: str):
        s = str(s or "").lstrip("vV")
        out = []
        for p in s.split("."):
            if p.isdigit():
                out.append((0, int(p)))
            else:
                out.append((1, p))
        return tuple(out)

    return parts(a) <= parts(b)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_current_pointer(contract_dir: Optional[str] = None) -> str:
    """
    Read contracts/CURRENT as one pure version line.
    Only a single trailing newline may be present; no strip()-forgiveness for spaces.
    """
    root = resolve_contract_dir(contract_dir)
    current_path = root / "CURRENT"
    if not current_path.is_file():
        raise FileNotFoundError("contracts/CURRENT missing: %s" % current_path)
    raw = current_path.read_text(encoding="utf-8")
    if raw.endswith("\r\n"):
        body = raw[:-2]
    elif raw.endswith("\n"):
        body = raw[:-1]
    else:
        body = raw
    if body == "" or "\n" in body or "\r" in body or " " in body or "\t" in body:
        raise ValueError("CURRENT must be one pure version line, got %r" % raw)
    if body != body.strip():
        raise ValueError("CURRENT has surrounding whitespace: %r" % raw)
    return body


def load_contract(
    version: Optional[str] = None,
    contract_dir: Optional[str] = None,
    expected_hash: Optional[str] = None,
) -> ContractVersion:
    """
    Load one version file.
    version=None → CURRENT file → FILEMATE_CONTRACT_VERSION env → newest by name.
    expected_hash: if set, must equal file sha256 or raise.
    """
    root = resolve_contract_dir(contract_dir)
    ver_dir = root / "versions"
    if not ver_dir.is_dir():
        raise FileNotFoundError("contract versions dir missing: %s" % ver_dir)

    if not version:
        current_path = root / "CURRENT"
        if current_path.is_file():
            version = read_current_pointer(str(root))
        else:
            version = os.environ.get("FILEMATE_CONTRACT_VERSION")
    if not version:
        files = sorted(ver_dir.glob("*.json"))
        if not files:
            raise FileNotFoundError("no contract version files in %s" % ver_dir)
        path = files[-1]
        version = path.stem
    else:
        path = ver_dir / ("%s.json" % version.lstrip("vV") if not version.startswith("v") else "%s.json" % version)
        # normalize: accept both v1.0.0 and 1.0.0
        if not path.is_file():
            alt = ver_dir / ("v%s.json" % version.lstrip("vV"))
            path = alt if alt.is_file() else ver_dir / ("%s.json" % version)
        if not path.is_file():
            raise FileNotFoundError("contract version not found: %s" % version)
        version = path.stem

    digest = file_sha256(path)
    if expected_hash and expected_hash != digest:
        raise ValueError(
            "contract_hash mismatch for %s: expected %s got %s"
            % (path.name, expected_hash, digest)
        )
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("contract file must be a JSON object")
    declared = str(data.get("contract_version") or version)
    return ContractVersion(version=declared, path=path, file_hash=digest, data=data)


def verify_contract_hash(cv: ContractVersion, declared_hash: str) -> bool:
    return bool(declared_hash) and cv.file_hash == declared_hash


def list_versions(contract_dir: Optional[str] = None) -> List[str]:
    ver_dir = resolve_contract_dir(contract_dir) / "versions"
    if not ver_dir.is_dir():
        return []
    return [p.stem for p in sorted(ver_dir.glob("*.json"))]
