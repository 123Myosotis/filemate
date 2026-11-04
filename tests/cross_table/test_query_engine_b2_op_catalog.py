# -*- coding: utf-8 -*-
"""B2: query operator catalog + complexity / budget gate (contract-slot coeffs)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline.block_cross_table.query_engine import (
    ARG_EXTREMUM_OP_ID,
    FAIL_LOCUS_BUDGET,
    GROUP_TOP_K_OP_ID,
    OpCatalogError,
    SORT_TRUNCATE_OP_ID,
    estimate_step_cost,
    gate_step_budget,
    read_complexity_bound_coeffs,
    validate_op_catalog,
    validate_plan_ops_linkable,
    validate_result_projection,
)
from tests.cross_table.qe_test_helpers import (
    catalog_from_current,
    current_slice,
    slots_for_tests,
)

_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "query_engine"


def _catalog():
    return catalog_from_current()


def _slice():
    return current_slice()


def _slots():
    return slots_for_tests()


def test_catalog_all_ops_have_complexity_bound_class_no_inline_coeff():
    cat = _catalog()
    ids = validate_op_catalog(cat)
    assert GROUP_TOP_K_OP_ID in ids
    assert ARG_EXTREMUM_OP_ID in ids
    assert SORT_TRUNCATE_OP_ID in ids
    assert "op_result_projection" not in ids
    for op_id, spec in cat["operators"].items():
        assert "complexity_bound" in spec
        assert spec["complexity_bound"].get("class")
        assert "coeff" not in spec["complexity_bound"]


def test_reject_op_missing_complexity_bound():
    cat = _catalog()
    bad = copy.deepcopy(cat)
    del bad["operators"]["op_filter"]["complexity_bound"]
    with pytest.raises(OpCatalogError, match="complexity_bound"):
        validate_op_catalog(bad)


def test_reject_op_with_inline_coeff():
    cat = _catalog()
    bad = copy.deepcopy(cat)
    bad["operators"]["op_filter"]["complexity_bound"]["coeff"] = 1.0
    with pytest.raises(OpCatalogError, match="coeff must not live on op"):
        validate_op_catalog(bad)


def test_sort_truncate_must_forbid_group_latest_flag():
    cat = _catalog()
    bad = copy.deepcopy(cat)
    bad["operators"][SORT_TRUNCATE_OP_ID].pop("forbids_as_group_latest", None)
    with pytest.raises(OpCatalogError, match="forbids_as_group_latest"):
        validate_op_catalog(bad)


def test_estimate_and_budget_pass_under_limit_from_contract_slots():
    cat = _catalog()
    slots = _slots()
    cost = gate_step_budget(
        cat,
        "op_filter",
        {"nrows": 100.0},
        query_exec_budget=slots["budget"],
        coeff_slots=slots["coeffs"],
        bound_eval=slots["eval"],
    )
    assert cost == estimate_step_cost(
        cat,
        "op_filter",
        {"nrows": 100.0},
        coeff_slots=slots["coeffs"],
        bound_eval=slots["eval"],
    )
    assert cost <= float(slots["budget"]["max_cost_per_step"])


def test_current_production_coeffs_null_without_test_override():
    """CURRENT numeric slots are null; reading coeffs without override must fail."""
    with pytest.raises(OpCatalogError, match="no numeric values"):
        read_complexity_bound_coeffs(_slice())


def test_budget_gate_fail_locus_budget():
    cat = _catalog()
    slots = _slots()
    budget = dict(slots["budget"])
    budget["max_cost_per_step"] = 1.0
    with pytest.raises(OpCatalogError) as ei:
        gate_step_budget(
            cat,
            "op_sort_truncate",
            {"nrows": 10_000.0},
            query_exec_budget=budget,
            coeff_slots=slots["coeffs"],
            bound_eval=slots["eval"],
        )
    assert ei.value.fail_locus == FAIL_LOCUS_BUDGET


def test_plan_rejects_bare_sort_truncate_as_group_latest():
    cat = _catalog()
    validate_op_catalog(cat)
    with pytest.raises(OpCatalogError, match="group_latest"):
        validate_plan_ops_linkable(
            cat,
            [{"op": SORT_TRUNCATE_OP_ID, "claims_group_latest": True}],
        )


def test_plan_allows_group_top_k_as_group_latest():
    cat = _catalog()
    validate_op_catalog(cat)
    validate_plan_ops_linkable(
        cat,
        [
            {"op": "op_filter"},
            {"op": GROUP_TOP_K_OP_ID, "claims_group_latest": True},
        ],
    )


def test_plan_allows_sort_truncate_with_h_rewrite():
    cat = _catalog()
    validate_op_catalog(cat)
    validate_plan_ops_linkable(
        cat,
        [
            {
                "op": SORT_TRUNCATE_OP_ID,
                "claims_group_latest": True,
                "rewrite_of": "GroupTopK",
            }
        ],
    )


def test_plan_type_unlink_rejected():
    cat = _catalog()
    validate_op_catalog(cat)
    with pytest.raises(OpCatalogError, match="type unlink"):
        validate_plan_ops_linkable(
            cat,
            [{"op": "op_exists_any"}, {"op": "op_filter"}],
        )


def test_plan_rejects_result_projection_as_executable_step():
    cat = _catalog()
    validate_op_catalog(cat)
    with pytest.raises(OpCatalogError, match="unknown op"):
        validate_plan_ops_linkable(
            cat,
            [{"op": "op_result_projection"}],
        )


def test_result_projection_schema_fixture_validates_independently():
    data = json.loads(
        (_FIX / "result_projection_fixture.json").read_text(encoding="utf-8")
    )
    validate_result_projection(data["result_projection"])
