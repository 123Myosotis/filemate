# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from pipeline.synth_contract.budget import check_over_budget, enforce_budgets, validate_meter
from pipeline.synth_contract.calc import (
    enforce_calc_and_order,
    replay_calc_spec,
    validate_agg_null_policy,
    validate_group_edge,
    validate_order_spec,
)
from pipeline.synth_contract.dag import enforce_dag_and_review, validate_region_dag
from pipeline.synth_contract.registry import load_contract
from pipeline.synth_contract.xref import enforce_xref_and_caliber


class TestCalcXrefBudget(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cv = load_contract("v1.1.0")

    def test_agg_null_matrix(self):
        self.assertTrue(validate_agg_null_policy(self.cv, "sum", "skip").ok)
        self.assertFalse(validate_agg_null_policy(self.cv, "count_distinct", "zero").ok)
        self.assertFalse(validate_agg_null_policy(self.cv, "sum", "nope").ok)

    def test_calc_replay_sum(self):
        spec = {
            "calc_id": "c1",
            "calc_version": "1",
            "result_catalog_id": "#amt",
            "agg_null_policy": "skip",
            "steps": [{"op": "sum", "source_catalog_id": "#v", "column": "v"}],
            "norm_value": "6.0",
            "round_spec": {"mode": "half_even", "scale": 1},
        }
        rows = [{"v": "1"}, {"v": "2"}, {"v": "3"}]
        r = replay_calc_spec(self.cv, spec, rows, {"#v": "v"})
        self.assertTrue(r.ok, r.issues)
        bad = dict(spec, norm_value="99")
        self.assertFalse(replay_calc_spec(self.cv, bad, rows, {"#v": "v"}).ok)

    def test_order_dedupe_agg_executed(self):
        """True execution via execute_mechanical — not structure-only."""
        from pipeline.synth_contract.boot import ensure_contract_library, reset_boot_for_tests
        from pipeline.synth_contract.executor import execute_mechanical
        from tests.contract.plan_helpers import (
            agg_semantics, empty_map, identity_norm, to_ops_plan,
        )

        reset_boot_for_tests()
        ensure_contract_library()
        tables = {
            "t": {
                "name": "t",
                "columns": ["a", "v"],
                "rows": [
                    {"a": "b", "v": "1"},
                    {"a": "a", "v": "2"},
                    {"a": "a", "v": "3"},
                ],
            }
        }
        plan = to_ops_plan({
            "status": "ready",
            "primary_table": "t",
            "output_columns": ["a", "agg_result"],
            "norm_specs": [identity_norm("kn_id")],
            "empty_token_maps": [empty_map("a"), empty_map("v")],
            "order_specs": [{
                "order_spec_id": "o1",
                "keys": [{"column": "a", "dir": "asc"}],
                "tie_break": "row_id",
                "null_order": "last",
            }],
            "dedupe_specs": [{
                "key_norm_id": "kn_id",
                "key_column": "a",
                "keep": "first_in_order",
                "order_spec_id": "o1",
            }],
            "calc_specs": [{
                "calc_id": "c1",
                "calc_version": "1",
                "result_column": "agg_result",
                "steps": [{"op": "count", "column": "v"}],
                "agg_null_policy": "skip",
            }],
            "agg_specs": [{
                "group_columns": ["a"],
                "calc_id": "c1",
                "order_spec_id": "o1",
                "agg_semantics": agg_semantics("count"),
            }],
        })
        plan["contract_version"] = self.cv.version
        plan["contract_hash"] = self.cv.file_hash
        out = execute_mechanical(tables, plan, [], self.cv)
        self.assertFalse(out.get("contract_rejected"), out.get("plan_notes"))
        by = {r["a"]: r["agg_result"] for r in out["rows"]}
        # after order+dedupe: first a, first b (one each); agg count per group = 1
        self.assertEqual(by.get("a"), 1)
        self.assertEqual(by.get("b"), 1)

    def test_group_edge_miss(self):
        r = validate_group_edge(
            {"op_id": "aggregate", "input_coords": ["Z9"], "group_key": "g"},
            {"A1"}, {"aggregate"})
        self.assertFalse(r.ok)

    def test_entity_alias_xref(self):
        plan = {
            "batch_id": "b1",
            "entity_reg": [
                {"entity_id": "e1", "scope": {"file_id": "f", "sheet": "S", "region": "r0",
                                              "txn": "t1"},
                 "key_norm_id": "n1", "member_coords": ["A1"], "norm_value": "X"},
                {"entity_id": "e2", "scope": {"file_id": "f", "sheet": "S", "region": "r0",
                                              "txn": "t2"},
                 "key_norm_id": "n1", "member_coords": ["B1"], "norm_value": "X"},
            ],
            "alias_map": [{
                "from_entity_id": "e1", "to_entity_id": "e2",
                "evidence_coords": ["A1", "B1"],
            }],
            "xref": [{
                "from_txn": "t1", "to_txn": "t2",
                "from_catalog_id": "#1", "to_catalog_id": "#2",
                "entity_id": "e1",
            }],
            "catalog": [
                {"catalog_id": "#1", "scope": {"file_id": "f", "sheet": "S", "region": "r0",
                                               "txn": "t1"}},
                {"catalog_id": "#2", "scope": {"file_id": "f", "sheet": "S", "region": "r0",
                                               "txn": "t2"}},
            ],
            "normalize_specs": [{"spec_id": "n1"}],
        }
        # without alias would fail multi-owner — with alias ok except xref entity in both txns
        # e1 only in t1 — xref requires e1 in both → fail await_user
        r = enforce_xref_and_caliber(plan)
        self.assertFalse(r.ok)
        plan["entity_reg"].append({
            "entity_id": "e1", "scope": {"file_id": "f", "sheet": "S", "region": "r0",
                                         "txn": "t2"},
            "key_norm_id": "n1", "member_coords": ["C1"], "norm_value": "X",
        })
        r2 = enforce_xref_and_caliber(plan)
        self.assertTrue(r2.ok, [i.message for i in r2.issues])

    def test_caliber_conflict(self):
        plan = {
            "calc_specs": [{"calc_id": "c1"}],
            "norm_specs": [{"norm_id": "n1"}],
            "calibers": [{
                "caliber_id": "cal1", "calc_id": "c1", "norm_id": "n1", "text_id": "t1",
            }],
            "metric_calibers": [
                {"catalog_id": "#m", "caliber_id": "cal1"},
                {"catalog_id": "#m", "caliber_id": "cal1"},
            ],
        }
        self.assertTrue(enforce_xref_and_caliber(plan).ok)
        plan["calibers"].append({
            "caliber_id": "cal2", "calc_id": "c1", "norm_id": "n1", "text_id": "t2",
        })
        plan["metric_calibers"][1]["caliber_id"] = "cal2"
        self.assertFalse(enforce_xref_and_caliber(plan).ok)

    def test_budget_over_and_meter(self):
        meter = {"meter_version": "m-1", "meter_includes": ["elapsed_ms", "call_count"]}
        self.assertTrue(validate_meter(self.cv, meter).ok)
        bad_m = {"meter_version": "other", "meter_includes": ["elapsed_ms"]}
        self.assertFalse(validate_meter(self.cv, bad_m).ok)
        plan = {
            "meter": meter,
            "budgets": [{
                "budget_id": "b1",
                "budget_scope": {"file_id": "f", "sheet": "S", "region": "r0"},
                "limits": {"elapsed_ms": 100},
                "on_over_budget_strategy": "ask",
                "actual": {"elapsed_ms": 200},
            }],
        }
        r = enforce_budgets(plan, self.cv)
        self.assertFalse(r.ok)
        self.assertTrue(any(i.state_id == "over_budget" for i in r.issues))
        self.assertTrue(check_over_budget(
            {"limits": {"call_count": 5}}, {"call_count": 3}).ok)

    def test_region_dag_cycle(self):
        regs = [
            {"region_id": "r0", "depends_on": ["r1"]},
            {"region_id": "r1", "depends_on": ["r0"]},
        ]
        self.assertFalse(validate_region_dag(regs).ok)
        ok = [
            {"region_id": "r0", "depends_on": []},
            {"region_id": "r1", "depends_on": ["r0"]},
        ]
        self.assertTrue(validate_region_dag(ok).ok)

    def test_review_batch_independence(self):
        plan = {
            "review_batches": [
                {"sample_manifest_id": "m1", "coords": ["A1"]},
                {"sample_manifest_id": "m1", "coords": ["B1"]},
            ]
        }
        self.assertFalse(enforce_dag_and_review(plan).ok)
        plan["review_batches"][1]["sample_manifest_id"] = "m2"
        self.assertTrue(enforce_dag_and_review(plan).ok)

    def test_order_spec_requires_tie_break(self):
        spec = {
            "keys": [{"catalog_id": "#a", "dir": "asc"}],
            "null_order": "first",
        }
        self.assertFalse(validate_order_spec(self.cv, spec).ok)


if __name__ == "__main__":
    unittest.main()
