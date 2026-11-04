# -*- coding: utf-8 -*-
"""
Meaning equivalence for dual-DB dedup (M5.2 dual engine).

chat engine: AI JSON {is_equivalent} first; SequenceMatcher @ fallback on failure.
vector engine + local: SequenceMatcher only @ fallback_threshold (default 0.6).
vector engine + ai: SequenceMatcher screen (≥threshold) then AI confirm (宽进严出).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pipeline.block_cross_table.memory_rag.constants import (
    ENGINE_CHAT,
    ENGINE_VECTOR,
    MEANING_EQ_AI,
    MEANING_EQ_LOCAL,
)
from pipeline.block_cross_table.memory_rag.meaning_similarity import (
    meaning_similarity,
    meanings_equivalent,
)

ChatJsonFn = Callable[..., dict | None]

METHOD_EXACT = "exact"
METHOD_AI = "ai"
METHOD_FALLBACK = "fallback"
METHOD_VECTOR_SCREEN = "vector_screen"

_EQUIV_PROMPT = """句子 A: {meaning_a}
句子 B: {meaning_b}
这两个含义是否等价？忽略细微措辞差别，关注主要语义是否相同。
以 JSON 输出：
{{"is_equivalent": true}}
"""


@dataclass(frozen=True)
class EquivalenceVerdict:
    equivalent: bool
    method: str
    similarity: float | None
    detail: dict[str, Any]


def _default_chat_json(prompt: str, **_kwargs: Any) -> dict | None:
    import config
    from ai.client import chat_json

    key = config.get_api_key()
    if not key:
        return None
    return chat_json(prompt, key, temperature=0.1)


def _ai_verdict(
    a: str,
    b: str,
    *,
    sim: float,
    fallback_threshold: float,
    chat_json_fn: ChatJsonFn | None,
) -> EquivalenceVerdict:
    chat = chat_json_fn if chat_json_fn is not None else _default_chat_json
    detail_err: dict[str, Any] = {}
    try:
        prompt = _EQUIV_PROMPT.format(meaning_a=a, meaning_b=b)
        raw = chat(prompt)
    except Exception as exc:
        raw = None
        detail_err = {"error": str(exc)}
    if isinstance(raw, dict) and "is_equivalent" in raw:
        return EquivalenceVerdict(
            bool(raw.get("is_equivalent")),
            METHOD_AI,
            sim,
            {"raw": raw, **detail_err},
        )
    eq = meanings_equivalent(a, b, threshold=float(fallback_threshold))
    return EquivalenceVerdict(
        eq,
        METHOD_FALLBACK,
        sim,
        {"reason": "ai_unavailable_or_bad_response", **detail_err},
    )


def meanings_equivalent_smart(
    meaning_a: str,
    meaning_b: str,
    *,
    fallback_threshold: float = 0.6,
    chat_json_fn: ChatJsonFn | None = None,
    prefer_ai: bool = True,
) -> EquivalenceVerdict:
    """
    Legacy entry: prefer_ai=True → chat first; else SequenceMatcher only.
    Prefer meanings_equivalent_for_engine for M5.2.
    """
    a = str(meaning_a or "").strip()
    b = str(meaning_b or "").strip()
    if not a and not b:
        return EquivalenceVerdict(True, METHOD_EXACT, 1.0, {})
    if not a or not b:
        return EquivalenceVerdict(False, METHOD_EXACT, 0.0, {})
    if a == b:
        return EquivalenceVerdict(True, METHOD_EXACT, 1.0, {})

    sim = meaning_similarity(a, b)
    if prefer_ai:
        return _ai_verdict(
            a,
            b,
            sim=sim,
            fallback_threshold=fallback_threshold,
            chat_json_fn=chat_json_fn,
        )
    eq = meanings_equivalent(a, b, threshold=float(fallback_threshold))
    return EquivalenceVerdict(eq, METHOD_FALLBACK, sim, {"reason": "prefer_ai_false"})


def meanings_equivalent_for_engine(
    meaning_a: str,
    meaning_b: str,
    *,
    engine: str,
    meaning_equivalence_mode: str = MEANING_EQ_AI,
    fallback_threshold: float = 0.6,
    chat_json_fn: ChatJsonFn | None = None,
) -> EquivalenceVerdict:
    """
    Engine-aware equivalence.
    chat: AI-first (MaiBot-style).
    vector+local: SequenceMatcher @ fallback_threshold.
    vector+ai: screen by SequenceMatcher then AI confirm.
    """
    a = str(meaning_a or "").strip()
    b = str(meaning_b or "").strip()
    if not a and not b:
        return EquivalenceVerdict(True, METHOD_EXACT, 1.0, {})
    if not a or not b:
        return EquivalenceVerdict(False, METHOD_EXACT, 0.0, {})
    if a == b:
        return EquivalenceVerdict(True, METHOD_EXACT, 1.0, {})

    eng = str(engine or ENGINE_CHAT).strip().lower()
    mode = str(meaning_equivalence_mode or MEANING_EQ_AI).strip().lower()
    sim = meaning_similarity(a, b)
    thr = float(fallback_threshold)

    if eng == ENGINE_CHAT:
        return _ai_verdict(
            a, b, sim=sim, fallback_threshold=thr, chat_json_fn=chat_json_fn
        )

    # vector engine
    local_eq = meanings_equivalent(a, b, threshold=thr)
    if mode == MEANING_EQ_LOCAL or mode != MEANING_EQ_AI:
        return EquivalenceVerdict(
            local_eq,
            METHOD_FALLBACK,
            sim,
            {"reason": "vector_local", "engine": ENGINE_VECTOR},
        )

    # vector + ai: 宽进 — only candidates ≥ threshold go to AI; 严出 — AI decides
    if not local_eq:
        return EquivalenceVerdict(
            False,
            METHOD_VECTOR_SCREEN,
            sim,
            {"reason": "vector_screen_reject", "threshold": thr},
        )
    verdict = _ai_verdict(
        a, b, sim=sim, fallback_threshold=thr, chat_json_fn=chat_json_fn
    )
    detail = dict(verdict.detail)
    detail["screen_passed"] = True
    detail["engine"] = ENGINE_VECTOR
    return EquivalenceVerdict(
        verdict.equivalent, verdict.method, verdict.similarity, detail
    )
