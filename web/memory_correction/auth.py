# -*- coding: utf-8 -*-
"""X-Memory-Token: file-backed token; never Cookie."""
from __future__ import annotations

import secrets
from pathlib import Path

TOKEN_HEADER = "X-Memory-Token"
TOKEN_FILENAME = ".token"
FAIL_LOCUS_AUTH = "memory_web_auth"


def token_path(root: Path | None = None) -> Path:
    base = root or Path(__file__).resolve().parent
    return base / TOKEN_FILENAME


def ensure_token(root: Path | None = None) -> str:
    path = token_path(root)
    if path.exists():
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            return raw
    value = secrets.token_urlsafe(32)
    path.write_text(value + "\n", encoding="utf-8")
    return value


def read_token(root: Path | None = None) -> str | None:
    path = token_path(root)
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    return raw or None


def validate_token(provided: str | None, root: Path | None = None) -> bool:
    expected = read_token(root) or ensure_token(root)
    if not provided:
        return False
    return secrets.compare_digest(str(provided), str(expected))
