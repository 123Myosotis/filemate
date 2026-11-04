# -*- coding: utf-8 -*-
"""Mechanical meaning similarity (no business-word branching)."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


_WS_RE = re.compile(r"\s+", re.UNICODE)
_PUNCT_RE = re.compile(r"[\W_]+", re.UNICODE)


def normalize_meaning(text: str) -> str:
    raw = unicodedata.normalize("NFKC", str(text or "")).casefold().strip()
    raw = _WS_RE.sub(" ", raw)
    # Keep CJK/letters/digits; drop punctuation for comparison.
    raw = _PUNCT_RE.sub("", raw)
    return raw


def meaning_similarity(a: str, b: str) -> float:
    """Return [0,1] similarity; 1.0 if both empty after normalize."""
    na, nb = normalize_meaning(a), normalize_meaning(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return float(SequenceMatcher(None, na, nb).ratio())


def meanings_equivalent(a: str, b: str, *, threshold: float) -> bool:
    return meaning_similarity(a, b) >= float(threshold)
