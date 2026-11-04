# -*- coding: utf-8 -*-
"""
Endpoint allowlist: empty list = explicit exemption (not silent skip).

Forbidden pattern: ``if allowlist and url not in allowlist``
(empty would silently skip the check).
"""
from __future__ import annotations

from typing import Sequence

from pipeline.block_cross_table.memory_rag.constants import (
    ALLOWLIST_MODE_EMPTY,
    ALLOWLIST_MODE_ENFORCED,
)
from pipeline.block_cross_table.memory_rag.errors import (
    LOCUS_BACKEND_GATE,
    MemoryRagError,
)


def normalize_endpoint(endpoint: str) -> str:
    return str(endpoint or "").strip().rstrip("/")


def evaluate_endpoint_allowlist(
    endpoint: str,
    allowlist: Sequence[str] | None,
) -> tuple[str, bool]:
    """
    Returns (allowlist_mode, allowed).

    - empty / None → mode=empty_exemption, allowed=True (explicit pass)
    - non-empty → mode=enforced; allowed iff normalized endpoint in set
    """
    items = [normalize_endpoint(x) for x in list(allowlist or []) if str(x).strip()]
    # Explicit empty-set branch — do NOT use ``if items and ...``.
    if len(items) == 0:
        return ALLOWLIST_MODE_EMPTY, True
    target = normalize_endpoint(endpoint)
    allowed = target in set(items)
    return ALLOWLIST_MODE_ENFORCED, allowed


def require_endpoint_allowlisted(
    endpoint: str,
    allowlist: Sequence[str] | None,
) -> str:
    """Raise on enforced miss; return allowlist_mode on pass."""
    mode, allowed = evaluate_endpoint_allowlist(endpoint, allowlist)
    if not allowed:
        raise MemoryRagError(
            "endpoint not on allowlist",
            fail_locus=LOCUS_BACKEND_GATE,
            subreason="endpoint_not_allowlisted",
            detail={
                "endpoint": normalize_endpoint(endpoint),
                "allowlist_mode": mode,
            },
        )
    return mode
