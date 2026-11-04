# -*- coding: utf-8 -*-
"""
Dual-path jargon meaning inference (context vs content-only) + compare.

Chat model only. Inject chat_json_fn for tests (no live network required).
Prompt templates are structural placeholders — zero business-word branching.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from pipeline.block_cross_table.memory_rag.errors import (
    LOCUS_JARGON_INFERENCE,
    MemoryRagError,
)

ChatJsonFn = Callable[..., dict | None]


_PROMPT_WITH_CONTEXT = """词条内容
{content}

词条出现的上下文
{raw_content_list}
{previous_meaning_section}

请根据上下文推断该词条含义。
若信息不足无法推断，设置 no_info 为 true。
{previous_meaning_instruction}

以 JSON 输出：
{{"meaning": "含义说明", "no_info": false}}
"""

_PROMPT_CONTENT_ONLY = """词条内容
{content}

请仅根据词条本身推断含义。

以 JSON 输出：
{{"meaning": "含义说明"}}
"""

_PROMPT_COMPARE = """推断结果1（基于上下文）
{inference1}

推断结果2（仅基于词条）
{inference2}

请比较二者主要含义是否相同或类似（忽略细微差别）。

以 JSON 输出：
{{"is_similar": true, "reason": "判断理由"}}
"""


@dataclass(frozen=True)
class InferenceResult:
    content: str
    meaning: str
    is_jargon: bool
    no_info: bool
    inference_with_context: dict[str, Any]
    inference_content_only: dict[str, Any]
    comparison: dict[str, Any]


def _default_chat_json(prompt: str, **_kwargs: Any) -> dict | None:
    import config
    from ai.client import chat_json

    key = config.get_api_key()
    if not key:
        raise MemoryRagError(
            "chat api key missing for jargon inference",
            fail_locus=LOCUS_JARGON_INFERENCE,
            subreason="api_key_missing",
        )
    return chat_json(prompt, key, temperature=0.1)


def infer_jargon_meaning(
    *,
    content: str,
    context_snippets: list[str],
    previous_meaning: str = "",
    chat_json_fn: ChatJsonFn | None = None,
) -> InferenceResult:
    """
    Run context inference → content-only → compare.
    is_jargon = not is_similar (MaiBot rule).
    """
    term = str(content or "").strip()
    if not term:
        raise MemoryRagError(
            "empty jargon content",
            fail_locus=LOCUS_JARGON_INFERENCE,
            subreason="empty_content",
        )
    chat = chat_json_fn or _default_chat_json
    ctx_text = "\n".join(f"- {s}" for s in context_snippets if str(s).strip()) or "(无上下文)"
    prev = str(previous_meaning or "").strip()
    prev_section = f"\n上一次推断的含义（仅供参考）\n{prev}" if prev else ""
    prev_inst = (
        "请参考上一次推断的含义，结合新上下文给出更准确结果。" if prev else ""
    )
    prompt1 = _PROMPT_WITH_CONTEXT.format(
        content=term,
        raw_content_list=ctx_text,
        previous_meaning_section=prev_section,
        previous_meaning_instruction=prev_inst,
    )
    inf1 = chat(prompt1) or {}
    if not isinstance(inf1, dict):
        raise MemoryRagError(
            "context inference returned non-object",
            fail_locus=LOCUS_JARGON_INFERENCE,
            subreason="bad_context_response",
        )
    if bool(inf1.get("no_info")) or not str(inf1.get("meaning") or "").strip():
        return InferenceResult(
            content=term,
            meaning="",
            is_jargon=False,
            no_info=True,
            inference_with_context=inf1,
            inference_content_only={},
            comparison={},
        )

    prompt2 = _PROMPT_CONTENT_ONLY.format(content=term)
    inf2 = chat(prompt2) or {}
    if not isinstance(inf2, dict):
        raise MemoryRagError(
            "content-only inference returned non-object",
            fail_locus=LOCUS_JARGON_INFERENCE,
            subreason="bad_content_only_response",
        )

    prompt3 = _PROMPT_COMPARE.format(
        inference1=json.dumps(inf1, ensure_ascii=False),
        inference2=json.dumps(inf2, ensure_ascii=False),
    )
    cmp_ = chat(prompt3) or {}
    if not isinstance(cmp_, dict):
        raise MemoryRagError(
            "compare inference returned non-object",
            fail_locus=LOCUS_JARGON_INFERENCE,
            subreason="bad_compare_response",
        )
    is_similar = bool(cmp_.get("is_similar"))
    is_jargon = not is_similar
    meaning = str(inf1.get("meaning") or "").strip() if is_jargon else prev
    if is_jargon and not meaning:
        meaning = str(inf1.get("meaning") or "").strip()
    return InferenceResult(
        content=term,
        meaning=meaning,
        is_jargon=is_jargon,
        no_info=False,
        inference_with_context=inf1,
        inference_content_only=inf2,
        comparison=cmp_,
    )
