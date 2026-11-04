# -*- coding: utf-8 -*-
"""Weighted Reciprocal Rank Fusion (MaiBot-compatible formula)."""
from __future__ import annotations

from typing import Any, Callable, Sequence


def weighted_rrf(
    ranked_lists: Sequence[Sequence[dict[str, Any]]],
    *,
    weights: Sequence[float],
    rrf_k: int,
    id_key: str = "id",
) -> list[dict[str, Any]]:
    """
    Fuse multiple ranked lists.
    score(id) = sum_i w_i * 1/(rrf_k + rank_i)
    Each item must carry id_key; first occurrence's payload is kept.
    """
    if len(ranked_lists) != len(weights):
        raise ValueError("ranked_lists and weights length mismatch")
    k = max(1, int(rrf_k))
    merged: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    for items, weight in zip(ranked_lists, weights):
        w = float(weight)
        for rank, item in enumerate(items, start=1):
            item_id = str(item.get(id_key, "") or "")
            if not item_id:
                continue
            if item_id not in merged:
                merged[item_id] = dict(item)
            scores[item_id] = scores.get(item_id, 0.0) + w * (1.0 / (k + rank))
    out = list(merged.values())
    for item in out:
        item["rrf_score"] = float(scores.get(str(item.get(id_key)), 0.0))
        item["score"] = item["rrf_score"]
    out.sort(key=lambda x: float(x.get("rrf_score", 0.0)), reverse=True)
    return out


def assign_text_id(item: dict[str, Any], *, content_key: str = "content") -> str:
    return str(item.get(content_key, "") or "").strip()


def dedupe_text_rows(
    rows: Sequence[dict[str, Any]],
    *,
    content_key: str = "content",
    meaning_key: str = "meaning",
    equivalent: Callable[[str, str], bool] | None = None,
) -> list[dict[str, Any]]:
    """
    Merge rows that share the same content; if meanings equivalent (or equal),
    keep one. Conflicting meanings keep both tagged with conflict=True.
    """
    by_content: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        content = str(row.get(content_key, "") or "").strip()
        if not content:
            continue
        by_content.setdefault(content, []).append(dict(row))

    out: list[dict[str, Any]] = []
    for content, group in by_content.items():
        if len(group) == 1:
            item = group[0]
            item["id"] = content
            item["deduped"] = False
            out.append(item)
            continue
        kept: list[dict[str, Any]] = []
        for candidate in group:
            meaning = str(candidate.get(meaning_key, "") or "")
            matched = False
            for existing in kept:
                em = str(existing.get(meaning_key, "") or "")
                same = (
                    equivalent(meaning, em)
                    if equivalent is not None
                    else meaning.strip() == em.strip()
                )
                if same:
                    # Prefer higher count / prefer local source tag if present.
                    if int(candidate.get("count") or 0) > int(existing.get("count") or 0):
                        existing.update(candidate)
                    origins = existing.setdefault("origins", [])
                    if isinstance(origins, list):
                        origins.append(candidate.get("origin", "unknown"))
                    matched = True
                    break
            if not matched:
                candidate = dict(candidate)
                candidate["origins"] = [candidate.get("origin", "unknown")]
                kept.append(candidate)
        if len(kept) == 1:
            item = kept[0]
            item["id"] = content
            item["deduped"] = True
            out.append(item)
        else:
            for i, item in enumerate(kept):
                item["id"] = f"{content}#conflict:{i}"
                item["content"] = content
                item["conflict"] = True
                item["deduped"] = False
                out.append(item)
    return out
