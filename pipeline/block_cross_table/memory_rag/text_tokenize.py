# -*- coding: utf-8 -*-
"""
Generic TXT tokenization for jargon frequency mining (M5.1 filters).

No business-word lists. Optional jieba if installed; else Unicode letter runs + CJK n-grams.
Filters: min length, reject pure punctuation, optional stopword set (config/env).
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Iterable, Sequence

_LATIN_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{1,63}", re.UNICODE)
_CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")
_PUNCT_ONLY = re.compile(r"^[\W_]+$", re.UNICODE)


def _cjk_ngrams(text: str, *, n_min: int, n_max: int) -> Iterable[str]:
    chars = _CJK_CHAR.findall(text)
    if not chars:
        return
    joined = "".join(chars)
    for n in range(int(n_min), int(n_max) + 1):
        if n <= 0:
            continue
        for i in range(0, max(0, len(joined) - n + 1)):
            yield joined[i : i + n]


def is_pure_punctuation(token: str) -> bool:
    t = str(token or "").strip()
    if not t:
        return True
    if _PUNCT_ONLY.match(t):
        return True
    # All chars are Unicode punctuation / symbol / separator
    cats = {unicodedata.category(ch)[0] for ch in t}
    return cats.issubset({"P", "S", "Z"}) and not any(
        unicodedata.category(ch).startswith("L") or unicodedata.category(ch).startswith("N")
        for ch in t
    )


def accept_token(
    token: str,
    *,
    min_token_len: int = 2,
    stopwords: Sequence[str] | None = None,
) -> bool:
    t = str(token or "").strip()
    if len(t) < int(min_token_len):
        return False
    if is_pure_punctuation(t):
        return False
    if stopwords:
        stopped = {str(s).strip() for s in stopwords if str(s).strip()}
        if t in stopped or t.casefold() in {s.casefold() for s in stopped}:
            return False
    return True


def tokenize_document(
    text: str,
    *,
    cjk_n_min: int = 2,
    cjk_n_max: int = 4,
    min_token_len: int = 2,
    stopwords: Sequence[str] | None = None,
) -> list[str]:
    raw = str(text or "")
    tokens: list[str] = []
    try:
        import jieba  # type: ignore

        for piece in jieba.cut(raw):
            t = str(piece).strip()
            if accept_token(t, min_token_len=min_token_len, stopwords=stopwords):
                tokens.append(t)
        if tokens:
            return tokens
    except Exception:
        pass
    tokens.extend(m.group(0) for m in _LATIN_WORD.finditer(raw))
    tokens.extend(_cjk_ngrams(raw, n_min=cjk_n_min, n_max=cjk_n_max))
    return [
        t
        for t in tokens
        if accept_token(t, min_token_len=min_token_len, stopwords=stopwords)
    ]


def count_terms(
    text: str,
    *,
    cjk_n_min: int = 2,
    cjk_n_max: int = 4,
    min_token_len: int = 2,
    stopwords: Sequence[str] | None = None,
) -> Counter[str]:
    return Counter(
        tokenize_document(
            text,
            cjk_n_min=cjk_n_min,
            cjk_n_max=cjk_n_max,
            min_token_len=min_token_len,
            stopwords=stopwords,
        )
    )


def context_windows(
    text: str,
    term: str,
    *,
    window_chars: int = 80,
    max_windows: int = 5,
) -> list[str]:
    """Extract surrounding snippets for a term (mechanical, not semantic)."""
    raw = str(text or "")
    needle = str(term or "")
    if not raw or not needle:
        return []
    out: list[str] = []
    start = 0
    w = max(8, int(window_chars))
    while len(out) < int(max_windows):
        idx = raw.find(needle, start)
        if idx < 0:
            break
        lo = max(0, idx - w)
        hi = min(len(raw), idx + len(needle) + w)
        out.append(raw[lo:hi].strip())
        start = idx + max(1, len(needle))
    return out
