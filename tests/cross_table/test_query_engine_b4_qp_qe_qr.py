# -*- coding: utf-8 -*-
"""B4: QP / QE / QR (fixture AI stand-ins; zero business words)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline.block_cross_table.query_engine import (
    ACTION_DELIVER,
    ACTION_ROLLBACK_QP,
    ACTION_ROUTE_E,
    E_ENTRY_WU,
    FAIL_LOCUS_BUDGET,
    QueryEngineError,
    QueryTraceSink,
    ROLLBACK_QUERY_PLANNER_START,
    STANDARD_IDS,
    VERDICT_FAILED,
    VERDICT_NEEDS_USER,
    VERDICT_PASSED,
    assemble_qp_input,
    execute_query_plan,
    load_plan_fixture,
    qp_from_fixture,
    read_query_trace_kinds,
    run_query_review,
    validate_query_plan_ast,
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


def _sink():
    return QueryTraceSink(allowed_kinds=read_query_trace_kinds(_slice()))


def _run_qe(plan, *, budget=None, sink=None, reports=None):
    slots = _slots()
    if budget is not None:
        slots = dict(slots)
        slots["budget"] = budget

    def _on_fail(rep):
        if reports is not None:
            reports.append(rep)

    return execute_query_plan(
        plan,
        _catalog(),
        tables_by_id=_tables(),
        coeff_slots=slots["coeffs"],
        bound_eval=slots["eval"],
        query_exec_budget=slots["budget"],
        trace_sink=sink,
        on_fail_report=_on_fail if reports is not None else None,
    )


# --- QP ---


def test_qp_legal_plan_passes():
    out = qp_from_fixture(_FIX / "plan_legal.json", _catalog())
    assert out["status"] == "plan_ready"
    assert out["plan"]["query_time_basis"] == "data_max_time"


def test_qp_assemble_input_envelope():
    env = assemble_qp_input(
        utterance="span_utterance_abstract",
        portraits=[{"structure_status": "layers_recognized"}],
        catalog=_catalog(),
        query_time_basis_candidates=["data_max_time", "wall_clock_now"],
    )
    assert "operator_catalog_op_ids" in env
    assert env["utterance"] == "span_utterance_abstract"


def test_qp_reject_unknown_op():
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    plan = copy.deepcopy(plan)
    plan["steps"][0]["op"] = "op_not_in_catalog"
    with pytest.raises(QueryEngineError, match="unknown op"):
        validate_query_plan_ast(plan, _catalog())


def test_qp_reject_missing_bound_op():
    cat = copy.deepcopy(_catalog())
    del cat["operators"]["op_filter"]["complexity_bound"]
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    with pytest.raises(QueryEngineError):
        validate_query_plan_ast(plan, cat)


def test_qp_reject_result_projection_as_step():
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    plan = copy.deepcopy(plan)
    plan["steps"].append(
        {
            "step_id": "s_bad_proj",
            "op": "op_result_projection",
            "args": {},
            "inputs": ["h_derive"],
            "output": "h_bad",
            "provenance": {"utterance_span": "span_x"},
        }
    )
    with pytest.raises(QueryEngineError, match="result_projection"):
        validate_query_plan_ast(plan, _catalog())


def test_qp_reject_empty_provenance():
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    plan = copy.deepcopy(plan)
    plan["steps"][0]["provenance"] = {}
    with pytest.raises(QueryEngineError, match="provenance"):
        validate_query_plan_ast(plan, _catalog())


def test_qp_reject_missing_h_window_projection():
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    plan = copy.deepcopy(plan)
    plan["steps"] = [
        s
        for s in plan["steps"]
        if s["op"] not in ("op_group_top_k", "op_arg_extremum")
    ]
    with pytest.raises(QueryEngineError, match="missing H"):
        validate_query_plan_ast(plan, _catalog())


def test_qp_reject_missing_rollback_anchor():
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    plan = copy.deepcopy(plan)
    plan["rollback_anchors"] = []
    with pytest.raises(QueryEngineError, match="query_planner_start"):
        validate_query_plan_ast(plan, _catalog())


def test_qp_reject_missing_result_projection_block():
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    plan = copy.deepcopy(plan)
    del plan["result_projection"]
    with pytest.raises(QueryEngineError, match="result_projection"):
        validate_query_plan_ast(plan, _catalog())


def test_qp_reject_type_mismatch():
    """B4 收尾：DAG 类型不匹配拒。"""
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    plan = copy.deepcopy(plan)
    # scalar (exists) cannot feed filter (expects table).
    plan["steps"] = [
        {
            "step_id": "s_ex",
            "op": "op_exists_any",
            "args": {"column_id": "c_flag", "value": "yes", "mode": "EXISTS"},
            "inputs": ["src:t0"],
            "output": "h_ex",
            "provenance": {"utterance_span": "span_ex"},
        },
        {
            "step_id": "s_bad",
            "op": "op_filter",
            "args": {"column_id": "c_flag", "cmp": "eq", "value": "yes"},
            "inputs": ["h_ex"],
            "output": "h_bad",
            "provenance": {"utterance_span": "span_bad"},
        },
    ]
    with pytest.raises(QueryEngineError, match="type mismatch"):
        validate_query_plan_ast(plan, _catalog(), require_full_capability_cover=False)


def test_qp_reject_query_time_basis_unset():
    """B4 收尾：query_time_basis 未选定拒。"""
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    plan = copy.deepcopy(plan)
    plan["query_time_basis"] = None
    with pytest.raises(QueryEngineError, match="query_time_basis"):
        validate_query_plan_ast(plan, _catalog())


# --- QE ---


def test_qe_budget_exceeded_fail_locus_budget_no_exec():
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    reports = []
    budget = dict(_slots()["budget"])
    budget["max_cost_per_step"] = 0.0
    out = _run_qe(plan, budget=budget, reports=reports)
    assert out["status"] == "failed"
    assert out["fail_locus"] == FAIL_LOCUS_BUDGET
    assert out["steps_executed"] == 0
    assert out["report_to_i"] is True
    assert reports and reports[0]["fail_locus"] == FAIL_LOCUS_BUDGET
    assert reports[0].get("executed") is False


def test_qe_reject_sort_truncate_as_group_latest():
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    plan = copy.deepcopy(plan)
    # as_group_latest in args only — QP linkable may pass; QE must refuse.
    plan["steps"] = [
        {
            "step_id": "s_bad",
            "op": "op_sort_truncate",
            "args": {
                "sort_column_id": "c_score",
                "limit": 1,
                "as_group_latest": True,
            },
            "inputs": ["src:t0"],
            "output": "h_bad",
            "provenance": {"utterance_span": "span_bad"},
        }
    ]
    plan["edges"] = [{"from": "h_bad", "to": "x", "cardinality": "one_to_many"}]
    plan["condition_trees"] = {"ct": {"node_kind": "EXISTS", "children": []}}
    slots = _slots()
    out = execute_query_plan(
        plan,
        _catalog(),
        tables_by_id=_tables(),
        coeff_slots=slots["coeffs"],
        bound_eval=slots["eval"],
        query_exec_budget=slots["budget"],
        require_full_capability_cover=False,
    )
    assert out["status"] == "failed"
    assert out["fail_locus"] == "group_latest"
    assert out["report_to_i"] is True


def test_qe_mechanical_exec_ops_and_one_to_many():
    """
    逐 op 断言清单：
    - op_filter → h_filt 行均 c_flag==yes
    - op_join → h_join 含 c_attr
    - op_interval_build ×2 → h_iv_a / h_iv_b 含 interval
    - op_stamp_window ×2 → h_win_a / h_win_b 行 c_ts 落窗
    - op_exists_any → h_exists.scalar is True
    - op_group_top_k → h_topk 每 c_key 至多 1 行
    - op_arg_extremum → h_arg 非空
    - op_derive → h_derive 含 c_mark
    - result_projection → 仅 c_key/c_mark
    - one_to_many → h_win_a_fan 存在且等于 h_win_a 行数
    """
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    sink = _sink()
    sink.write("query_plan", {"plan_status": "plan_ready"})
    out = _run_qe(plan, sink=sink)
    assert out["status"] == "ok"
    assert out["steps_executed"] == len(plan["steps"])
    h = out["handles"]

    # op_filter
    assert h["h_filt"]["rows"]
    assert all(r.get("c_flag") == "yes" for r in h["h_filt"]["rows"])

    # op_join
    assert any("c_attr" in r for r in h["h_join"]["rows"])

    # op_interval_build (dual)
    assert "interval" in h["h_iv_a"] and "interval" in h["h_iv_b"]

    # op_stamp_window (dual)
    assert all(1 <= r["c_ts"] <= 10 for r in h["h_win_a"]["rows"])
    assert all(2 <= r["c_ts"] <= 20 for r in h["h_win_b"]["rows"])

    # EXISTS/ANY
    assert h["h_exists"].get("scalar") is True

    # GroupTopK
    keys = [r["c_key"] for r in h["h_topk"]["rows"]]
    assert len(keys) == len(set(keys))

    # ArgExtremum
    assert h["h_arg"]["rows"]

    # derive
    assert all(r.get("c_mark") == "marked" for r in h["h_derive"]["rows"])

    # one_to_many fan-out
    assert "h_win_a_fan" in h
    assert len(h["h_win_a_fan"]["rows"]) == len(h["h_win_a"]["rows"])

    # projection
    assert set(out["result_table"]["columns"]) == {"c_key", "c_mark"}
    assert out["result_table"]["rows"]
    kinds = {r["call_kind"] for r in sink.records}
    assert "query_plan" in kinds
    assert "query_exec_step" in kinds


def test_qe_step_trace_written_and_fail_not_swallowed():
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    plan = copy.deepcopy(plan)
    plan["steps"][0]["args"] = {"column_id": "c_flag", "cmp": "eq", "value": "no_match_zzz"}
    # Still succeeds mechanically with empty filter — force join key fail
    plan["steps"][1]["args"] = {"left_key": "", "right_key": ""}
    reports = []
    sink = _sink()
    out = _run_qe(plan, sink=sink, reports=reports)
    assert out["status"] == "failed"
    assert out["fail_locus"] == "join_key"
    assert reports
    assert any(r.get("status") == "failed" for r in sink.records if r["call_kind"] == "query_exec_step")


# --- QR ---


def test_qr_reconciliation_missing_row_review_failed():
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    exec_ok = {"status": "ok", "steps_executed": len(plan["steps"])}
    rows = _recon()[:-1]  # drop one demand row
    out = run_query_review(
        plan,
        exec_ok,
        reconciliation_rows=rows,
        verdict_path=_FIX / "verdict_passed.json",
    )
    assert out["verdict"] == VERDICT_FAILED
    assert out["action"] == ACTION_ROLLBACK_QP
    assert out["rollback_anchor"] == ROLLBACK_QUERY_PLANNER_START
    assert out["patch_forbidden"] is True
    assert out["local_override"] is True


def test_std7_still_fails_on_unbacked_projection_column():
    """Projection column_id absent from recon → std7 fail (not self-certified)."""
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    plan = copy.deepcopy(plan)
    plan["result_projection"]["columns"].append(
        {
            "column_id": "c_unbacked_not_in_any_step",
            "source_step_id": "s_derive",
            "provenance": {"utterance_span": "span_unbacked"},
        }
    )
    exec_ok = {"status": "ok", "steps_executed": len(plan["steps"])}
    out = run_query_review(
        plan,
        exec_ok,
        reconciliation_rows=_recon(),
        verdict_path=_FIX / "verdict_passed.json",
    )
    assert out["verdict"] == VERDICT_FAILED
    assert out["local_override"] is True
    assert any(
        c.get("standard_id") == "std7_reconciliation"
        and c.get("ok") is False
        and "c_unbacked_not_in_any_step" in (c.get("missing") or [])
        for c in out["checks"]
    )


def test_std7_passes_with_string_bound_column_or_filter():
    """String bound_column_or_filter that names a real projection/step column → std7 pass."""
    from pipeline.block_cross_table.query_engine.qr import (
        build_reconciliation_table,
        check_standard_7,
    )

    plan = {
        "demand_points": [
            {
                "demand_id": "d1",
                "kind": "filter",
                "utterance_span": "span_x",
                "bound_step_id": "s_filter",
                "bound_column_or_filter": "c_flag",
            }
        ],
        "steps": [{"step_id": "s_filter", "op": "op_filter"}],
        "result_projection": {
            "columns": [
                {
                    "column_id": "c_flag",
                    "source_step_id": "s_filter",
                    "provenance": {"utterance_span": "span_x"},
                }
            ]
        },
    }
    recon = build_reconciliation_table(plan)
    assert any(r.get("column_or_filter") == "c_flag" for r in recon)
    s7 = check_standard_7(plan, recon)
    assert s7.get("ok") is True


def test_std7_fails_on_fabricated_utterance_span():
    """Demand/proj share fake span; steps lack it → fail (fabricated_span)."""
    from pipeline.block_cross_table.query_engine.qr import (
        build_reconciliation_table,
        check_standard_7,
        run_query_review,
        VERDICT_FAILED,
    )

    plan = {
        "utterance": "帮我查一下二次安防设备的数据",
        "demand_points": ["随便什么无关内容"],
        "steps": [
            {
                "step_id": "s_filter",
                "op": "op_filter",
                "provenance": {"utterance_span": "二次安防设备"},
            }
        ],
        "result_projection": {
            "columns": [
                {
                    "column_id": "批次号",
                    "source_step_id": "s_filter",
                    "provenance": {"utterance_span": "随便什么无关内容"},
                }
            ]
        },
    }
    recon = build_reconciliation_table(plan)
    assert recon == []
    s7 = check_standard_7(plan, recon)
    assert s7.get("ok") is False
    assert s7.get("reason") == "fabricated_span"
    out = run_query_review(
        plan,
        {"status": "ok", "steps_executed": 1},
        reconciliation_rows=None,
        verdict_path=_FIX / "verdict_passed.json",
    )
    assert out["verdict"] == VERDICT_FAILED
    assert out["local_override"] is True


def test_std7_fails_on_consistently_fabricated_span():
    """Fake span on demand + projection + steps (internally consistent) still fail."""
    from pipeline.block_cross_table.query_engine.qr import (
        build_reconciliation_table,
        check_standard_7,
        run_query_review,
        VERDICT_FAILED,
    )

    fake = "量子纠缠对账XYZ"
    plan = {
        "utterance": "帮我查一下二次安防设备的数据",
        "demand_points": [fake],
        "steps": [
            {
                "step_id": "s_filter",
                "op": "op_filter",
                "provenance": {"utterance_span": fake},
            }
        ],
        "result_projection": {
            "columns": [
                {
                    "column_id": "批次号",
                    "source_step_id": "s_filter",
                    "provenance": {"utterance_span": fake},
                }
            ]
        },
    }
    recon = build_reconciliation_table(plan)
    assert recon == []
    s7 = check_standard_7(plan, recon)
    assert s7.get("ok") is False
    assert s7.get("reason") == "fabricated_span"
    out = run_query_review(
        plan,
        {"status": "ok", "steps_executed": 1},
        reconciliation_rows=None,
        verdict_path=_FIX / "verdict_passed.json",
    )
    assert out["verdict"] == VERDICT_FAILED
    assert out["local_override"] is True


def test_std7_passes_on_real_utterance_span():
    """Demand label may be arbitrary; span on proj+steps must be utterance fragment."""
    from pipeline.block_cross_table.query_engine.qr import (
        build_reconciliation_table,
        check_standard_7,
    )

    utt = "帮我查一下二次安防设备的数据"
    span = "二次安防设备"
    plan = {
        "utterance": utt,
        "demand_points": ["无关标签可忽略"],
        "steps": [
            {
                "step_id": "s_filter",
                "op": "op_filter",
                "provenance": {"utterance_span": span},
            }
        ],
        "result_projection": {
            "columns": [
                {
                    "column_id": "设备名称",
                    "source_step_id": "s_filter",
                    "provenance": {"utterance_span": span},
                },
                {
                    "column_id": "厂家",
                    "source_step_id": "s_filter",
                    "provenance": {"utterance_span": span},
                },
            ]
        },
    }
    recon = build_reconciliation_table(plan)
    assert {r.get("column_or_filter") for r in recon} >= {"设备名称", "厂家"}
    assert check_standard_7(plan, recon).get("ok") is True


def test_std7_passes_on_utterance_substring_span():
    """Partial fragment of a longer utterance phrase is legal (not fabricated)."""
    from pipeline.block_cross_table.query_engine.qr import (
        build_reconciliation_table,
        check_standard_7,
        span_covered_by_utterance,
    )

    utt = "给我漏扫数据,还有当前漏扫状态,只要成功过"
    assert span_covered_by_utterance("漏扫状态", utt) is True
    assert span_covered_by_utterance("当前漏扫状态", utt) is True
    plan = {
        "utterance": utt,
        "demand_points": ["漏扫状态"],
        "steps": [
            {
                "step_id": "s1",
                "op": "op_filter",
                "provenance": {"utterance_span": "漏扫状态"},
            }
        ],
        "result_projection": {
            "columns": [
                {
                    "column_id": "状态",
                    "source_step_id": "s1",
                    "provenance": {"utterance_span": "漏扫状态"},
                }
            ]
        },
    }
    recon = build_reconciliation_table(plan)
    s7 = check_standard_7(plan, recon)
    assert s7.get("ok") is True, s7


def test_std7_passes_on_punct_normalized_span():
    from pipeline.block_cross_table.query_engine.qr import (
        check_standard_7,
        span_covered_by_utterance,
    )

    utt = "需要 当前漏扫状态 ，输出表格"
    assert span_covered_by_utterance("当前、漏扫、状态", utt) is True
    plan = {
        "utterance": utt,
        "demand_points": ["当前漏扫状态"],
        "steps": [{"step_id": "s1", "op": "op_filter"}],
        "result_projection": {
            "columns": [
                {
                    "column_id": "状态",
                    "source_step_id": "s1",
                    "provenance": {"utterance_span": "当前、漏扫、状态"},
                }
            ]
        },
    }
    s7 = check_standard_7(
        plan,
        [
            {
                "demand_point": "当前漏扫状态",
                "step_id": "s1",
                "column_or_filter": "状态",
                "utterance_span": "当前、漏扫、状态",
            }
        ],
    )
    assert s7.get("ok") is True, s7


def test_std7_fails_on_true_fabricated_quantum_span():
    from pipeline.block_cross_table.query_engine.qr import check_standard_7

    plan = {
        "utterance": "给我漏扫数据,还有当前漏扫状态",
        "demand_points": ["量子纠缠"],
        "steps": [{"step_id": "s1", "op": "op_filter"}],
        "result_projection": {
            "columns": [
                {
                    "column_id": "状态",
                    "source_step_id": "s1",
                    "provenance": {"utterance_span": "量子纠缠"},
                }
            ]
        },
    }
    s7 = check_standard_7(plan, [])
    assert s7.get("ok") is False
    assert s7.get("reason") == "fabricated_span"
    assert s7.get("span") == "量子纠缠"


def test_qr_hcd_unreconciled_review_failed():
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    exec_ok = {"status": "ok", "steps_executed": len(plan["steps"])}
    rows = [r for r in _recon() if r["demand_point"] not in ("d_h", "d_arg")]
    # keep projection cols covered via other rows
    out = run_query_review(
        plan,
        exec_ok,
        reconciliation_rows=rows,
        verdict_path=_FIX / "verdict_passed.json",
    )
    assert out["verdict"] == VERDICT_FAILED
    assert any(
        c.get("standard_id") == "std3_derive_h_exists_projection" and c.get("ok") is False
        for c in out["checks"]
    )


def test_qr_review_passed_branch():
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    sink = _sink()
    exec_out = _run_qe(plan, sink=sink)
    assert exec_out["status"] == "ok"
    out = run_query_review(
        plan,
        exec_out,
        reconciliation_rows=_recon(),
        verdict_path=_FIX / "verdict_passed.json",
        trace_sink=sink,
    )
    assert out["verdict"] == VERDICT_PASSED
    assert out["action"] == ACTION_DELIVER
    assert len(STANDARD_IDS) == 8
    assert len(out["standards"]) == 8


def test_qr_review_failed_rollback_query_planner_start():
    out = run_query_review(
        load_plan_fixture(_FIX / "plan_legal.json"),
        {"status": "ok", "steps_executed": 10},
        reconciliation_rows=_recon(),
        verdict_path=_FIX / "verdict_failed.json",
        force_local_checks=False,
    )
    assert out["verdict"] == VERDICT_FAILED
    assert out["action"] == ACTION_ROLLBACK_QP
    assert out["rollback_anchor"] == ROLLBACK_QUERY_PLANNER_START


def test_qr_needs_user_routes_e_entry_wu():
    sink = _sink()
    out = run_query_review(
        load_plan_fixture(_FIX / "plan_legal.json"),
        {"status": "ok", "steps_executed": 10},
        reconciliation_rows=_recon(),
        verdict_path=_FIX / "verdict_needs_user.json",
        force_local_checks=False,
        trace_sink=sink,
    )
    assert out["verdict"] == VERDICT_NEEDS_USER
    assert out["action"] == ACTION_ROUTE_E
    assert out["e_entry"] == E_ENTRY_WU


# --- trace kinds ---


def test_trace_kinds_each_written():
    sink = _sink()
    plan = load_plan_fixture(_FIX / "plan_legal.json")
    sink.write("query_plan", {"ok": True})
    _run_qe(plan, sink=sink)
    sink.write(
        "query_budget_verdict",
        {"verdict": "continue_ask"},
    )
    run_query_review(
        plan,
        {"status": "ok", "steps_executed": len(plan["steps"])},
        reconciliation_rows=_recon(),
        verdict_path=_FIX / "verdict_passed.json",
        trace_sink=sink,
    )
    sink.write("query_user_reply", {"reply_kind": "confirm"})
    kinds = {r["call_kind"] for r in sink.records}
    assert "query_plan" in kinds
    assert "query_exec_step" in kinds
    assert "query_review" in kinds
    assert "query_budget_verdict" in kinds
    assert "query_user_reply" in kinds
