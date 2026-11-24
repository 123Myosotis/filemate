# -*- coding: utf-8 -*-
"""低置信时：固定枚举软标签（要 Key）；1~3 个；按内容 hash 缓存。

调用方应批量 load/save 缓存；本模块默认不每次落盘。
非法 / 异常 → other，source 可区分。
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.stage2_classifier.doc_classifier import (
    CLASSIFIER_VERSION,
    DOC_TYPES,
    RULE_TYPES,
    TYPE_LABELS,
    label_of,
)

from . import state as content_state

SOFT_LABEL_MAX_PER_RUN = 20


def _cache_path(root_out: Path) -> Path:
    return content_state.cache_path(root_out, "label_cache.json")


def load_label_cache(root_out: Path) -> dict:
    """版本不一致 → 空缓存，强制软标签重算。"""
    p = _cache_path(root_out)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if "classifier_version" in data or "entries" in data:
        if int(data.get("classifier_version") or 0) != CLASSIFIER_VERSION:
            return {}
        entries = data.get("entries")
        return entries if isinstance(entries, dict) else {}
    # 旧扁平格式视为 v1，与当前版本不一致则丢弃
    return {}


def save_label_cache(root_out: Path, cache: dict) -> None:
    p = _cache_path(root_out)
    try:
        payload = {
            "classifier_version": CLASSIFIER_VERSION,
            "entries": cache if isinstance(cache, dict) else {},
        }
        p.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _normalize_label_list(raw) -> list[str]:
    """英文键白名单，最多 3 个。"""
    items = []
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    out = []
    seen = set()
    for it in items:
        s = str(it or "").strip()
        if not s:
            continue
        key = s.lower() if s.lower() in DOC_TYPES else s
        if key not in DOC_TYPES:
            for k, zh in TYPE_LABELS.items():
                if s == zh or s in zh or zh.startswith(s):
                    key = k
                    break
        if key not in DOC_TYPES or key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= 3:
            break
    return out


def soft_label_from_model(
    text: str,
    content_hash: str,
    *,
    api_key: str = "",
    cache: dict | None = None,
) -> dict:
    """
    返回 {doc_type, tags, confidence, source}.

    tags: 1~3 个英文键；doc_type=tags[0]。
    source: cache | model | fallback_no_key | fallback_error | fallback_illegal
    """
    h = str(content_hash or "").strip()
    store = cache if cache is not None else {}
    if h and h in store:
        hit = store[h]
        if isinstance(hit, dict):
            tags = _normalize_label_list(
                hit.get("tags") or hit.get("doc_type") or hit.get("label")
            )
            if tags:
                return {
                    "doc_type": tags[0],
                    "tags": tags,
                    "confidence": float(hit.get("confidence") or 0),
                    "source": "cache",
                }

    key = (api_key or "").strip()
    if not key:
        return {
            "doc_type": "other",
            "tags": ["other"],
            "confidence": 0.0,
            "source": "fallback_no_key",
        }

    enum = "、".join(RULE_TYPES + ["other"])
    snippet = (text or "").strip()[:2500]
    prompt = (
        "你是文档类型分类器。只能从下列枚举中选 1~3 个 label（按相关度排序）：\n"
        f"{enum}\n"
        "只输出 JSON：{\"labels\":[\"contract\",\"project\"],\"confidence\":0.0到1.0}\n"
        "也可兼容 {\"label\":\"contract\",\"confidence\":0.8}。\n"
        "不要解释。若无法判断 labels 只含 other。\n\n"
        f"正文片段：\n{snippet}"
    )
    try:
        from ai.client import chat_json

        data = chat_json(prompt, key, max_tokens=96, temperature=0.1)
    except Exception:  # noqa: BLE001
        return {
            "doc_type": "other",
            "tags": ["other"],
            "confidence": 0.0,
            "source": "fallback_error",
        }

    if not isinstance(data, dict):
        return {
            "doc_type": "other",
            "tags": ["other"],
            "confidence": 0.0,
            "source": "fallback_error",
        }

    tags = _normalize_label_list(
        data.get("labels") or data.get("tags")
        or data.get("label") or data.get("doc_type")
    )
    try:
        conf = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if not tags:
        return {
            "doc_type": "other",
            "tags": ["other"],
            "confidence": 0.0,
            "source": "fallback_illegal",
        }

    if h:
        store[h] = {
            "doc_type": tags[0],
            "tags": tags,
            "confidence": conf,
            "zh": [label_of(t) for t in tags],
        }
    return {
        "doc_type": tags[0],
        "tags": tags,
        "confidence": conf,
        "source": "model",
    }
