# -*- coding: utf-8 -*-
"""Shared helpers: CURRENT contract + test-only numeric overrides (not production)."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.block_cross_table.query_engine import (
    load_cross_table_from_current,
    load_op_catalog_from_current,
    read_complexity_bound_eval,
    read_query_exec_budget,
)

_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "query_engine"


def current_slice() -> dict:
    return load_cross_table_from_current()


def catalog_from_current() -> dict:
    return load_op_catalog_from_current()


def test_only_numeric_overrides() -> dict:
    data = json.loads(
        (_FIX / "contract_slice_fixture.json").read_text(encoding="utf-8")
    )
    return dict(data["test_only_numeric_overrides"])


def slots_for_tests() -> dict:
    """Enums/eval from CURRENT; coeffs/budget from test-only overrides."""
    sl = current_slice()
    overrides = test_only_numeric_overrides()
    budget = dict(read_query_exec_budget(sl))
    # Overlay test numbers onto structure from CURRENT.
    budget.update(overrides["query_exec_budget"])
    return {
        "coeffs": dict(overrides["complexity_bound_coeffs"]),
        "eval": read_complexity_bound_eval(sl),
        "budget": budget,
        "ask_budget": overrides.get("query_ask_budget"),
        "replan_budget": overrides.get("query_replan_budget"),
        "term_expand_domain_enum_cap": overrides.get("term_expand_domain_enum_cap"),
        "slice": sl,
    }
