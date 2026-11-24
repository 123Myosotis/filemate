# -*- coding: utf-8 -*-
"""
Startup contract-library check. No env escape hatch.
Callers catch ContractLibraryError; this module never sys.exit.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .registry import ContractVersion, load_contract, resolve_contract_dir

_BOOT_DONE = False
_BOOT_CV: Optional[ContractVersion] = None

# Pure version token: letters/digits/dot/underscore/hyphen only; no spaces.
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ContractLibraryError(Exception):
    """Contract library missing, CURRENT dirty, or version file unusable."""


def _read_current_version_line(current_path: Path) -> str:
    """
    CURRENT must be one pure version line.
    Only a single trailing LF or CRLF is allowed; no strip()-based forgiveness.
    """
    if not current_path.is_file():
        raise ContractLibraryError("contracts/CURRENT missing: %s" % current_path)
    raw = current_path.read_text(encoding="utf-8")
    if raw.endswith("\r\n"):
        body = raw[:-2]
    elif raw.endswith("\n"):
        body = raw[:-1]
    else:
        body = raw
    if body == "" or "\n" in body or "\r" in body or " " in body or "\t" in body:
        raise ContractLibraryError(
            "contracts/CURRENT must be one pure version line (no spaces/comments): %r"
            % raw
        )
    if body != body.strip():  # leading/trailing whitespace other than removed newline
        raise ContractLibraryError("contracts/CURRENT has surrounding whitespace: %r" % raw)
    if not _VERSION_RE.match(body):
        raise ContractLibraryError("contracts/CURRENT not a pure version token: %r" % body)
    return body


def ensure_contract_library(contract_dir: Optional[str] = None) -> ContractVersion:
    """
    Load and cache the library pointed by CURRENT.
    Idempotent: repeat calls are no-op (no re-read) once _BOOT_DONE.
    """
    global _BOOT_DONE, _BOOT_CV
    if _BOOT_DONE and _BOOT_CV is not None:
        return _BOOT_CV

    root = resolve_contract_dir(contract_dir)
    ver_dir = root / "versions"
    if not ver_dir.is_dir():
        raise ContractLibraryError("contract versions dir missing: %s" % ver_dir)
    files = list(ver_dir.glob("*.json"))
    if not files:
        raise ContractLibraryError("no contract version files in %s" % ver_dir)

    version = _read_current_version_line(root / "CURRENT")
    path = ver_dir / ("%s.json" % version)
    if not path.is_file():
        raise ContractLibraryError(
            "CURRENT points to missing version file: %s" % path.name
        )
    try:
        cv = load_contract(version=version, contract_dir=str(root))
    except (FileNotFoundError, ValueError, OSError) as e:
        raise ContractLibraryError(str(e)) from e

    if not isinstance(cv.data, dict):
        raise ContractLibraryError("contract file must be a JSON object")
    if not cv.data.get("enums"):
        raise ContractLibraryError("contract missing enums")
    if not cv.data.get("contract_version"):
        raise ContractLibraryError("contract missing contract_version field")
    if not isinstance(cv.data.get("word_office"), dict):
        raise ContractLibraryError("contract missing word_office section")
    fo = cv.data.get("file_organize")
    if not isinstance(fo, dict):
        raise ContractLibraryError("contract missing file_organize section")
    if not isinstance(fo.get("intent_rules"), list):
        raise ContractLibraryError("file_organize.intent_rules missing")
    if not isinstance(fo.get("veto_patterns"), list):
        raise ContractLibraryError("file_organize.veto_patterns missing")

    _BOOT_CV = cv
    _BOOT_DONE = True
    return cv


def assert_contract_library_ready() -> None:
    """
    Idempotent assert for execute_plan: library must already be booted.
    Does not read disk. Raises ContractLibraryError if boot never ran.
    """
    if not _BOOT_DONE or _BOOT_CV is None:
        raise ContractLibraryError(
            "contract library not loaded — call ensure_contract_library before execute"
        )


def reset_boot_for_tests() -> None:
    """Test-only: clear boot cache so ensure_contract_library re-reads."""
    global _BOOT_DONE, _BOOT_CV
    _BOOT_DONE = False
    _BOOT_CV = None


def boot_contract_version() -> Optional[ContractVersion]:
    return _BOOT_CV
