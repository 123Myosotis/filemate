# -*- coding: utf-8 -*-
"""B5: I/E query sub-chain (五元 / 丁戊 / 预算枚举 / 回滚锚并存)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline.block_cross_table.query_engine import (
    QueryEngineError,
    QueryTraceSink,
    ROLLBACK_QUERY_PLANNER_START,
    load_plan_fixture,
    read_query_trace_kinds,
    read_structure_status_queryable,
)
from pipeline.block_cross_table.query_engine.ie_query import (
    ACTION_ROLLBACK_QP,
    BUDGET_VERDICT_CONTINUE,
    BUDGET_VERDICT_STOP,
    BUDGET_VERDICT_SWITCH,
    E_ENTRY_DING,
    E_ENTRY_WU,
    FIVE_PART_FIELD_KEYS,
    OWNERSHIP_ROLLBACK_POINT,
    QueryPlanSession,
    STAGE_ROLLBACK_POINTS,
    apply_budget_exhausted_verdict,
    apply_user_correction_void_plan,
    assert_rollback_anchors_coexist,
    build_five_part_ask,
    portraits_ready_for_query,
    run_query_subchain_i,
    validate_five_part_ask,
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


def _tables():
    return json.loads((_FIX / "tables_abstract.json").read_text(encoding="utf-8"))


def _recon():
    return json.loads((_FIX / "reconciliation_ok.json").read_text(encoding="utf-8"))[
        "rows"
    ]


def _portraits_ok():
    return [
        {
            "structure_status": "layers_recognized",
            "portrait_query_extensions": {
                "queryable_columns": [{"column_id": "c_key"}],
                "column_value_profiles": [],
                "time_axis_candidates": [],
                "join_key_candidates": [],
            },
        }
    ]


def _sink():
    return QueryTraceSink(allowed_kinds=read_query_trace_kinds(_slice()))


def test_five_part_has_independent_fifth_segment():
    env = build_five_part_ask(
        speculation="s",
        evidence="e",
        what_might_be_wrong="w",
        next_step="n",
        please_confirm_or_correct="please_confirm_or_correct",
    )
    validate_five_part_ask(env)
    for k in FIVE_PART_FIELD_KEYS:
        assert k in env
    assert env["fifth_segment_separate"] is True
    assert "please_confirm_or_correct" not in env["prompt_parts_for_callback"]
    assert len(env["prompt_parts_for_callback"]) == 4


def test_five_part_rejects_fifth_merged_into_prompt():
    env = build_five_part_ask(
        speculation="s",
        evidence="e",
        what_might_be_wrong="w",
        next_step="n",
        please_confirm_or_correct="please_confirm_or_correct",
    )
    env["prompt_parts_for_callback"] = list(env["prompt_parts_for_callback"]) + [
        env["please_confirm_or_correct"]
    ]
    with pytest.raises(QueryEngineError, match="fifth segment"):
        validate_five_part_ask(env)


def test_entry_ding_qp_needs_user():
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    plan = copy.deepcopy(plan)
    plan["status"] = "needs_user"
    slots = _slots()
    out = run_query_subchain_i(
        portraits=_portraits_ok(),
        queryable_statuses=read_structure_status_queryable(_slice()),
        catalog=_catalog(),
        plan=plan,
        plan_id="p1",
        session=QueryPlanSession(),
        tables_by_id=_tables(),
        coeff_slots=slots["coeffs"],
        bound_eval=slots["eval"],
        query_exec_budget=slots["budget"],
    )
    assert out["status"] == "needs_user"
    assert out["e_entry"] == E_ENTRY_DING
    assert "please_confirm_or_correct" in out["five_part"]
    assert out["allow_enter_b"] is False


def test_entry_wu_qr_needs_user():
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    slots = _slots()
    out = run_query_subchain_i(
        portraits=_portraits_ok(),
        queryable_statuses=read_structure_status_queryable(_slice()),
        catalog=_catalog(),
        plan=plan,
        plan_id="p1",
        session=QueryPlanSession(),
        tables_by_id=_tables(),
        coeff_slots=slots["coeffs"],
        bound_eval=slots["eval"],
        query_exec_budget=slots["budget"],
        reconciliation_rows=_recon(),
        verdict_fixture={"verdict": "needs_user"},
    )
    assert out["status"] == "needs_user"
    assert out["e_entry"] == E_ENTRY_WU
    assert out["allow_enter_b"] is False


def test_entry_wu_qe_ask_user_mechanical_fail():
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    plan = copy.deepcopy(plan)
    plan["steps"][1]["args"] = {"left_key": "", "right_key": ""}
    slots = _slots()
    out = run_query_subchain_i(
        portraits=_portraits_ok(),
        queryable_statuses=read_structure_status_queryable(_slice()),
        catalog=_catalog(),
        plan=plan,
        plan_id="p1",
        session=QueryPlanSession(),
        tables_by_id=_tables(),
        coeff_slots=slots["coeffs"],
        bound_eval=slots["eval"],
        query_exec_budget=slots["budget"],
        qe_ask_user_loci=frozenset({"join_key"}),
    )
    assert out["status"] == "needs_user"
    assert out["e_entry"] == E_ENTRY_WU
    assert out["qe"]["fail_locus"] == "join_key"


def test_correction_voids_old_plan_full_replan():
    session = QueryPlanSession()
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    session.adopt_plan(plan, "plan_v1")
    out = apply_user_correction_void_plan(
        session,
        correction_payload={"note": "user_fix_abstract"},
    )
    assert out["old_plan_void"] is True
    assert out["qp_must_replan_full"] is True
    assert out["voided_plan_id"] == "plan_v1"
    assert out["rollback_anchor"] == ROLLBACK_QUERY_PLANNER_START
    assert session.current_plan is None
    assert session.replan_required is True
    assert "plan_v1" in session.voided_plan_ids


def test_budget_exhausted_ai_enum_no_structural_exception():
    sink = _sink()
    for v in (
        BUDGET_VERDICT_CONTINUE,
        BUDGET_VERDICT_STOP,
        BUDGET_VERDICT_SWITCH,
    ):
        applied = apply_budget_exhausted_verdict(v, trace_sink=sink)
        assert applied["structural_exception"] is False
        assert applied["budget_verdict"] == v

    slots = _slots()
    out = run_query_subchain_i(
        portraits=_portraits_ok(),
        queryable_statuses=read_structure_status_queryable(_slice()),
        catalog=_catalog(),
        plan=load_plan_fixture(_FIX / "plan_legal.json"),
        plan_id="p1",
        session=QueryPlanSession(),
        tables_by_id=_tables(),
        coeff_slots=slots["coeffs"],
        bound_eval=slots["eval"],
        query_exec_budget=slots["budget"],
        ask_count=3,
        ask_budget=3,
        budget_ai_verdict_fixture=BUDGET_VERDICT_CONTINUE,
        trace_sink=sink,
    )
    assert out["status"] == "budget_exhausted"
    assert out["structural_exception"] is False
    assert out["budget_verdict"] == BUDGET_VERDICT_CONTINUE
    assert any(r["call_kind"] == "query_budget_verdict" for r in sink.records)


def test_rollback_anchors_query_and_stage_coexist_not_mixed():
    stage = set(STAGE_ROLLBACK_POINTS) | {OWNERSHIP_ROLLBACK_POINT}
    query = {ROLLBACK_QUERY_PLANNER_START}
    assert_rollback_anchors_coexist(stage_anchors=stage, query_anchors=query)
    with pytest.raises(QueryEngineError, match="mixed"):
        assert_rollback_anchors_coexist(
            stage_anchors=stage | {ROLLBACK_QUERY_PLANNER_START},
            query_anchors=query,
        )


def test_i_happy_path_review_passed_allows_enter_b():
    assert portraits_ready_for_query(
        _portraits_ok(),
        queryable_statuses=read_structure_status_queryable(_slice()),
    )
    slots = _slots()
    out = run_query_subchain_i(
        portraits=_portraits_ok(),
        queryable_statuses=read_structure_status_queryable(_slice()),
        catalog=_catalog(),
        plan=load_plan_fixture(_FIX / "plan_legal.json"),
        plan_id="p_ok",
        session=QueryPlanSession(),
        tables_by_id=_tables(),
        coeff_slots=slots["coeffs"],
        bound_eval=slots["eval"],
        query_exec_budget=slots["budget"],
        reconciliation_rows=_recon(),
        verdict_fixture={"verdict": "review_passed"},
    )
    assert out["status"] == "review_passed"
    assert out["allow_enter_b"] is True


def test_review_failed_rolls_back_query_planner_start():
    slots = _slots()
    session = QueryPlanSession()
    out = run_query_subchain_i(
        portraits=_portraits_ok(),
        queryable_statuses=read_structure_status_queryable(_slice()),
        catalog=_catalog(),
        plan=load_plan_fixture(_FIX / "plan_legal.json"),
        plan_id="p_fail",
        session=session,
        tables_by_id=_tables(),
        coeff_slots=slots["coeffs"],
        bound_eval=slots["eval"],
        query_exec_budget=slots["budget"],
        reconciliation_rows=_recon()[:-1],
        verdict_fixture={"verdict": "review_passed"},
    )
    assert out["status"] == "review_failed"
    assert out["action"] == ACTION_ROLLBACK_QP
    assert out["rollback_anchor"] == ROLLBACK_QUERY_PLANNER_START
    assert session.replan_required is True
