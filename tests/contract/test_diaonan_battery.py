# -*- coding: utf-8 -*-
"""
刁难抽查: structurally legal plans that must still be rejected mechanically.
E2E through execute_plan / gate — no fixture-only stubs.
"""
from __future__ import annotations

import unittest

from pipeline.synth_contract.accuracy import enforce_declared_accuracy
from pipeline.synth_contract.boot import ensure_contract_library, reset_boot_for_tests
from pipeline.synth_contract.gate import gate_plan
from pipeline.synth_contract.registry import load_contract
from pipeline.stage3_processor.table_synth import execute_plan
from tests.contract.plan_helpers import to_ops_plan


def _base(cv):
    plan = to_ops_plan({
        "status": "ready",
        "primary_table": "left",
        "output_columns": ["A"],
        "catalog": [{"catalog_id": "#1", "field_name": "A",
                     "scope": {"file_id": "left", "sheet": "S", "region": "r0"}}],
        "bindings": [{
            "scope": {"file_id": "left", "sheet": "S", "region": "r0"},
            "source_file": "left", "source_sheet": "S",
            "header_path": [], "orig_col_name": "A",
            "cell_coord_or_range": "A1",
            "target_field_id": "#1", "target_field_name": "A",
            "confidence": 0.9, "ambiguous_reason": "", "alternatives": [],
        }],
    })
    plan["contract_version"] = cv.version
    plan["contract_hash"] = cv.file_hash
    return plan


class TestDiaonanBattery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_boot_for_tests()
        ensure_contract_library()
        cls.cv = load_contract("v1.1.0")
        cls.tables = {
            "left": {
                "name": "left", "columns": ["A", "v"],
                "rows": [{"A": "1", "v": "10"}, {"A": "2", "v": "20"}],
                "file_id": "left", "sheet": "S",
                "data_coords": ["A1", "A2", "B1", "B2"],
            }
        }

    def test_calc_replay_mismatch_rejects_execute(self):
        plan = _base(self.cv)
        plan["sheet_data_coords"] = ["A1", "A2"]
        plan["calc_specs"] = [{
            "calc_id": "c1", "calc_version": "1", "result_catalog_id": "#1",
            "agg_null_policy": "skip",
            "group_key": "g1",
            "steps": [{"op": "sum", "column": "v"}],
            "norm_value": "999",
            "input_rows": self.tables["left"]["rows"],
        }]
        out = execute_plan(self.tables, plan, [])
        self.assertTrue(out.get("contract_rejected"))

    def test_unknown_trigger_version_rejected(self):
        plan = _base(self.cv)
        plan["triggers"] = ["not_a_real_trigger"]
        r, _ = gate_plan(plan, self.tables)
        self.assertFalse(r.ok)
        self.assertTrue(any(i.state_id == "version_rejected" for i in r.issues))

    def test_alias_required_for_shared_norm(self):
        plan = _base(self.cv)
        plan["batch_id"] = "b1"
        plan["entity_reg"] = [
            {"entity_id": "e1", "scope": {"file_id": "left", "sheet": "S", "region": "r0"},
             "key_norm_id": "n1", "member_coords": ["A1"], "norm_value": "K"},
            {"entity_id": "e2", "scope": {"file_id": "left", "sheet": "S", "region": "r0"},
             "key_norm_id": "n1", "member_coords": ["A2"], "norm_value": "K"},
        ]
        plan["normalize_specs"] = [{"spec_id": "n1"}]
        r = enforce_declared_accuracy(plan, self.cv)
        self.assertFalse(r.ok)
        self.assertTrue(any(i.code == "alias_required" for i in r.issues))

    def test_region_cycle_rejects(self):
        plan = _base(self.cv)
        plan["sheet_data_coords"] = ["A1", "A2"]
        plan["regions"] = [
            {"region_id": "r0", "scope": {"file_id": "left", "sheet": "S", "region": "r0"},
             "depends_on": ["r1"], "coords": ["A1"]},
            {"region_id": "r1", "scope": {"file_id": "left", "sheet": "S", "region": "r1"},
             "depends_on": ["r0"], "coords": ["A2"]},
        ]
        r, _ = gate_plan(plan, self.tables)
        self.assertFalse(r.ok)

    def test_budget_over_rejects_gate(self):
        plan = _base(self.cv)
        plan["meter"] = {"meter_version": "m-1",
                         "meter_includes": ["elapsed_ms", "call_count"]}
        plan["budgets"] = [{
            "budget_id": "b1",
            "budget_scope": {"file_id": "left", "sheet": "S", "region": "r0"},
            "limits": {"call_count": 1},
            "on_over_budget_strategy": "rollback",
            "actual": {"call_count": 9},
        }]
        r, _ = gate_plan(plan, self.tables)
        self.assertFalse(r.ok)
        self.assertTrue(any(i.state_id == "over_budget" for i in r.issues))

    def test_review_reread_rejects(self):
        plan = _base(self.cv)
        plan["review_anchor_mode"] = "disjoint_sample"
        plan["plan_anchor"] = {"sample_manifest_id": "m1", "coords": ["A1"]}
        plan["review_anchor"] = {"sample_manifest_id": "m1", "coords": ["A1"]}
        r, _ = gate_plan(plan, self.tables)
        self.assertFalse(r.ok)

    def test_key_norm_mismatch_on_join(self):
        plan = _base(self.cv)
        plan["contract_joins"] = [{
            "left_scope": {"file_id": "left", "sheet": "S", "region": "r0"},
            "right_scope": {"file_id": "left", "sheet": "S", "region": "r0"},
            "left_catalog_id": "#1", "right_catalog_id": "#1",
            "join_type": "left", "expected_cardinality": "1:1", "grain": "#1",
            "left_norm_steps": [{"op": "trim"}],
            "right_norm_steps": [{"op": "trim"}, {"op": "casefold"}],
        }]
        r = enforce_declared_accuracy(plan, self.cv)
        self.assertFalse(r.ok)

    def test_boot_required_before_execute(self):
        reset_boot_for_tests()
        try:
            out = execute_plan(self.tables, _base(self.cv), [])
            self.assertTrue(out.get("contract_rejected"))
            self.assertEqual(out.get("rows"), [])
            self.assertEqual(out.get("_mechanical"), False)
        finally:
            ensure_contract_library()


if __name__ == "__main__":
    unittest.main()
