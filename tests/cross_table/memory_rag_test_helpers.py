# -*- coding: utf-8 -*-
"""Test-only helpers: memory_rag numeric overrides (not production)."""
from __future__ import annotations

import json
from pathlib import Path

_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "memory_rag"


def test_only_numeric_overrides() -> dict:
    data = json.loads(
        (_FIX / "contract_slice_fixture.json").read_text(encoding="utf-8")
    )
    return dict(data["test_only_numeric_overrides"])


def slots_for_tests() -> dict:
    """Numerics from test fixture only; never from CURRENT."""
    overrides = test_only_numeric_overrides()
    return {
        "memory_recall_top_k": overrides["memory_recall_top_k"],
        "embedding_dim": overrides["embedding_dim"],
        "external_api_endpoint_allowlist": list(
            overrides.get("external_api_endpoint_allowlist") or []
        ),
        "memory_external_call_count_cap": overrides["memory_external_call_count_cap"],
        "memory_external_call_timeout_ms": overrides[
            "memory_external_call_timeout_ms"
        ],
        "memory_external_call_max_retries": overrides[
            "memory_external_call_max_retries"
        ],
        "overrides": overrides,
    }
