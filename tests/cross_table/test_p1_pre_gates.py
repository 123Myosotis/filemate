# -*- coding: utf-8 -*-
"""P1 pre-gates: A-tree check, fail_locus map, provenance_weak (no business hardcoding)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from pipeline.block_cross_table.query_engine.errors import QueryEngineError
from pipeline.block_cross_table.query_engine.ie_query import (
    FIVE_PART_FIELD_KEYS,
    map_fail_locus,
)
from pipeline.block_cross_table.query_engine.plan_structural_markers import (
    PROVENANCE_PLACEHOLDER_SPANS,
    is_placeholder_utterance_span,
)
from pipeline.block_cross_table.query_engine.qp_ai import (
    QP_SYSTEM,
    annotate_projection_provenance_weak,
    check_plan_ready_condition_trees,
    qp_call_ai,
)
from scripts.m10_clean_runner_lib import run_qp_case


def _catalog():
    return {
        "operators": {
            "op_filter": {
                "args_schema": {},
                "complexity_bound": {"class": "C1"},
            }
        }
    }


def _tables():
    return {"t1": {"columns": ["col_a", "col_b"], "rows": [{"col_a": "1"}]}}


def _base_plan(**extra):
    plan = {
        "status": "plan_ready",
        "query_time_basis": "data_max_time",
        "rollback_anchors": ["query_planner_start"],
        "demand_points": ["d"],
        "condition_trees": {},
        "result_projection": {
            "columns": [
                {
                    "column_id": "col_a",
                    "display_name": "col_a",
                    "column_name_source": "from_ai_runtime",
                    "source_step_id": "s0",
                    "derive_rule_ref": None,
                    "provenance": {"utterance_span": "hello"},
                }
            ]
        },
        "steps": [
            {
                "step_id": "s0",
                "op": "op_filter",
                "args": {},
                "inputs": ["src:t1"],
                "output": "filtered",
                "provenance": {"utterance_span": "hello"},
            }
        ],
        "edges": [],
    }
    plan.update(extra)
    return plan


def _needs_user(locus="predicate", **extra):
    base = {
        "status": "needs_user",
        "fail_locus": locus,
        "speculation": "s",
        "evidence": "e",
        "what_might_be_wrong": "w",
        "next_step": "n",
        "please_confirm_or_correct": "p",
        "rollback_anchors": ["query_planner_start"],
        "demand_points": ["d"],
        "condition_trees": {},
        "result_projection": {"columns": []},
        "steps": [],
        "edges": [],
        "query_time_basis": "utterance",
    }
    base.update(extra)
    return base


class TestPlaceholderSpansConstant(unittest.TestCase):
    def test_placeholder_set_frozen(self):
        self.assertEqual(
            PROVENANCE_PLACEHOLDER_SPANS,
            frozenset({"span_proj", "span", "proj", ""}),
        )
        self.assertTrue(is_placeholder_utterance_span("span_proj"))
        self.assertTrue(is_placeholder_utterance_span("  "))
        self.assertFalse(is_placeholder_utterance_span("real utterance fragment"))


class TestConditionTreeGate(unittest.TestCase):
    def test_dangling_tree_rejected(self):
        plan = _base_plan()
        plan["steps"][0]["args"] = {"condition_tree_id": "missing_tree"}
        with self.assertRaises(QueryEngineError) as ctx:
            check_plan_ready_condition_trees(plan)
        self.assertEqual(ctx.exception.fail_locus, "predicate")
        self.assertEqual(ctx.exception.subreason, "missing_condition_tree")

    def test_inline_conditions_ok(self):
        plan = _base_plan()
        plan["steps"][0]["args"] = {
            "condition_tree_id": "missing_tree",
            "conditions": [{"column": "col_a", "op": "eq", "value": "1"}],
        }
        check_plan_ready_condition_trees(plan)

    def test_tree_present_ok(self):
        plan = _base_plan()
        plan["condition_trees"] = {"t1": {"conditions": []}}
        plan["steps"][0]["args"] = {"condition_tree_id": "t1"}
        check_plan_ready_condition_trees(plan)

    def test_qp_retry_heals_dangling_tree(self):
        calls = {"n": 0}

        def fake_chat(prompt, api_key, system=None, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                p = _base_plan()
                p["steps"][0]["args"] = {"condition_tree_id": "ghost"}
                return p
            p = _base_plan()
            p["condition_trees"] = {"ghost": {"id": "ghost"}}
            p["steps"][0]["args"] = {"condition_tree_id": "ghost"}
            return p

        with patch(
            "pipeline.block_cross_table.query_engine.qp_ai.chat_json",
            side_effect=fake_chat,
        ), patch(
            "pipeline.block_cross_table.query_engine.qp_ai.validate_query_plan_ast",
            return_value={"status": "plan_ready"},
        ):
            out = qp_call_ai(
                utterance="hello",
                portraits=[],
                catalog=_catalog(),
                tables_by_id=_tables(),
                api_key="k",
                recalled_memories=[],
                require_full_capability_cover=False,
                apply_column_repair=False,
                max_retries=2,
            )
        self.assertEqual(out["status"], "plan_ready")
        self.assertEqual(calls["n"], 2)

    def test_qp_exhausted_dangling_to_needs_user(self):
        calls = {"n": 0}

        def fake_chat(prompt, api_key, system=None, **_k):
            calls["n"] += 1
            if calls["n"] <= 2:
                p = _base_plan()
                p["steps"][0]["args"] = {"condition_tree_id": "ghost"}
                return p
            return _needs_user(locus="predicate")

        with patch(
            "pipeline.block_cross_table.query_engine.qp_ai.chat_json",
            side_effect=fake_chat,
        ):
            out = qp_call_ai(
                utterance="hello",
                portraits=[],
                catalog=_catalog(),
                tables_by_id=_tables(),
                api_key="k",
                recalled_memories=[],
                require_full_capability_cover=False,
                apply_column_repair=False,
                max_retries=2,
            )
        self.assertEqual(out["status"], "needs_user")
        self.assertEqual(out["fail_locus"], "predicate")
        for k in FIVE_PART_FIELD_KEYS:
            self.assertTrue(str(out["five_part"].get(k) or "").strip())

    def test_qp_system_tree_rules_no_business_cols(self):
        self.assertIn("condition_tree_id", QP_SYSTEM)
        self.assertIn("fail_locus=predicate", QP_SYSTEM)
        # Column-name shortcuts remain forbidden outside/inside few-shot.
        for w in ("设备子类型", "设备名称"):
            self.assertNotIn(w, QP_SYSTEM)
        # Few-shot must stay abstract placeholders — not concrete jargon literals.
        self.assertIn("FEW-SHOT", QP_SYSTEM)
        core = QP_SYSTEM.split("=== FEW-SHOT", 1)[0]
        few = QP_SYSTEM.split("=== FEW-SHOT", 1)[1]
        for w in ("二次安防", "站控层", "A平面", "top50", "设备"):
            self.assertNotIn(w, core)
        for w in ("二次安防", "A平面", "top50", "防火墙"):
            self.assertNotIn(w, few)
        self.assertIn("<实体类别>", few)


class TestFailLocusMapAndRunner(unittest.TestCase):
    def test_map_projection_time_window_other(self):
        self.assertEqual(map_fail_locus("projection"), "projection")
        self.assertEqual(map_fail_locus("time_window"), "time_window")
        self.assertEqual(map_fail_locus(None), "other")
        self.assertEqual(map_fail_locus(""), "other")
        self.assertEqual(map_fail_locus("not_an_enum"), "other")

    def test_runner_maps_projection(self):
        def boom(*_a, **_k):
            raise QueryEngineError("p", fail_locus="projection")

        out = run_qp_case(
            case={"case_id": "x", "utterance": "u", "expected_status": "failed"},
            portraits=[],
            catalog=_catalog(),
            tables=_tables(),
            api_key="k",
            memories=[],
            qp_call_ai=boom,
        )
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["fail_locus"], "projection")
        self.assertEqual((out.get("audit") or {}).get("replan"), False)
        self.assertEqual((out.get("audit") or {}).get("persist_binding"), False)

    def test_runner_maps_time_window(self):
        def boom(*_a, **_k):
            raise QueryEngineError("t", fail_locus="time_window")

        out = run_qp_case(
            case={"case_id": "x", "utterance": "u"},
            portraits=[],
            catalog=_catalog(),
            tables=_tables(),
            api_key="k",
            memories=[],
            qp_call_ai=boom,
        )
        self.assertEqual(out["fail_locus"], "time_window")

    def test_runner_maps_illegal_to_other(self):
        def boom(*_a, **_k):
            raise QueryEngineError("bad", fail_locus="still_not_enum")

        out = run_qp_case(
            case={"case_id": "x", "utterance": "u"},
            portraits=[],
            catalog=_catalog(),
            tables=_tables(),
            api_key="k",
            memories=[],
            qp_call_ai=boom,
        )
        self.assertEqual(out["fail_locus"], "other")
        self.assertNotEqual(out["fail_locus"], "projection")

    def test_qp_call_ai_still_raises_not_swallowed(self):
        """qp_call_ai 耗尽后仍抛 QueryEngineError，不在内部吞成 status=failed。"""

        def fake_chat(prompt, api_key, system=None, **_k):
            p = _base_plan()
            p["steps"][0]["args"] = {"condition_tree_id": "ghost"}
            return p

        with patch(
            "pipeline.block_cross_table.query_engine.qp_ai.chat_json",
            side_effect=fake_chat,
        ):
            with self.assertRaises(QueryEngineError) as ctx:
                qp_call_ai(
                    utterance="hello",
                    portraits=[],
                    catalog=_catalog(),
                    tables_by_id=_tables(),
                    api_key="k",
                    recalled_memories=[],
                    require_full_capability_cover=False,
                    apply_column_repair=False,
                    max_retries=1,
                )
        self.assertEqual(ctx.exception.fail_locus, "predicate")


class TestProvenanceWeak(unittest.TestCase):
    def test_placeholder_span_marked_weak(self):
        plan = _base_plan()
        plan["result_projection"]["columns"][0]["provenance"] = {
            "utterance_span": "span_proj"
        }
        weak = annotate_projection_provenance_weak(plan)
        self.assertTrue(weak)
        self.assertTrue(
            plan["result_projection"]["columns"][0]["provenance"].get("weak")
        )

    def test_real_span_not_weak(self):
        plan = _base_plan()
        plan["result_projection"]["columns"][0]["provenance"] = {
            "utterance_span": "hello world"
        }
        weak = annotate_projection_provenance_weak(plan)
        self.assertFalse(weak)
        self.assertFalse(
            plan["result_projection"]["columns"][0]["provenance"].get("weak")
        )

    def test_memory_ref_not_weak_even_placeholder_span(self):
        plan = _base_plan()
        plan["result_projection"]["columns"][0]["provenance"] = {
            "utterance_span": "span_proj",
            "memory_ref": "bind-1",
        }
        weak = annotate_projection_provenance_weak(plan)
        self.assertFalse(weak)

    def test_qp_system_provenance_rules_no_business_cols(self):
        self.assertIn("portrait_path", QP_SYSTEM)
        self.assertIn("memory_ref", QP_SYSTEM)
        self.assertIn("utterance_span", QP_SYSTEM)
        for w in ("设备子类型", "设备名称"):
            self.assertNotIn(w, QP_SYSTEM)
        core = QP_SYSTEM.split("=== FEW-SHOT", 1)[0]
        self.assertNotIn("二次安防", core)
        self.assertNotIn("设备", core)


if __name__ == "__main__":
    unittest.main()
