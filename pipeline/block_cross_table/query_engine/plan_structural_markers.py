# -*- coding: utf-8 -*-
"""Structural marker constants for plan gates (not business vocabulary)."""
from __future__ import annotations

# 结构标记，非业务词 — used only for mechanical provenance_weak detection.
# Normalized empty / whitespace-only spans are also treated as placeholders.
PROVENANCE_PLACEHOLDER_SPANS = frozenset(
    {
        "span_proj",
        "span",
        "proj",
        "",
    }
)


def is_placeholder_utterance_span(span: object) -> bool:
    """True if span is missing/blank or equals a structural placeholder token."""
    if span is None:
        return True
    text = str(span).strip()
    return text in PROVENANCE_PLACEHOLDER_SPANS
