# -*- coding: utf-8 -*-
"""End-to-end mechanical executor + gate path (v1.1.0 ops[] only)."""
from __future__ import annotations

import unittest

from pipeline.synth_contract.boot import ensure_contract_library, reset_boot_for_tests
from pipeline.synth_contract.executor import execute_mechanical
from pipeline.synth_contract.registry import load_contract
from pipeline.stage3_processor.table_synth import execute_plan
from tests.contract.plan_helpers import attach_force_layer, to_ops_plan


def _base(cv, **extra):
    plan = {
        "status": "ready",
        "primary_table": "left",
        "output_columns": ["站点", "状态"],
    }
    plan.update(extra)
    cols = ["站点", "状态", "备注", "电压"]
    if any(plan.get(k) for k in (
        "joins", "contract_joins", "keep_row_when", "filters",
        "derived_columns", "derives", "order_specs", "dedupe_specs", "agg_specs",
    )):
        plan = attach_force_layer(plan, cols)
    plan = to_ops_plan(plan)
    plan["contract_version"] = cv.version
    plan["contract_hash"] = cv.file_hash
    return plan


TABLES = {
    "left": {
        "name": "left",
        "columns": ["站点", "状态", "备注"],
        "rows": [
            {"站点": "A站", "状态": "成功", "备注": ""},
            {"站点": "B站", "状态": "失败", "备注": ""},
            {"站点": "C站", "状态": "", "备注": ""},
        ],
    },
    "right": {
        "name": "right",
        "columns": ["站点", "电压"],
        "rows": [
            {"站点": "A站", "电压": "110"},
            {"站点": "B站", "电压": "220"},
        ],
    },
}


class TestExecutor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_boot_for_tests()
        ensure_contract_library()
        cls.cv = load_contract("v1.1.0")

    def test_filter_eq(self):
        plan = _base(
            self.cv,
            keep_row_when=[{"column": "状态", "op": "eq", "value": "成功"}],
        )
        out = execute_plan(TABLES, plan, notes=[])
        self.assertFalse(out.get("contract_rejected"), out.get("plan_notes"))
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["rows"][0]["站点"], "A站")
        self.assertTrue(out.get("_mechanical"))

    def test_project_only(self):
        plan = _base(self.cv)
        out = execute_mechanical(TABLES, plan, [], self.cv)
        self.assertFalse(out.get("contract_rejected"), out.get("plan_notes"))
        self.assertEqual(len(out["rows"]), 3)
        c = [r for r in out["rows"] if r["站点"] == "C站"][0]
        self.assertEqual(c["状态"], "")

    def test_join_exact(self):
        plan = _base(
            self.cv,
            output_columns=["站点", "电压"],
            joins=[{
                "right_table": "right",
                "left_column": "站点",
                "right_column": "站点",
                "how": "left",
                "key_norm_id": "kn_id",
            }],
        )
        out = execute_mechanical(TABLES, plan, [], self.cv)
        self.assertFalse(out.get("contract_rejected"), out.get("plan_notes"))
        by = {r["站点"]: r["电压"] for r in out["rows"]}
        self.assertEqual(by["A站"], "110")
        self.assertEqual(by["B站"], "220")
        self.assertEqual(by["C站"], "")

    def test_derive_eq(self):
        plan = _base(
            self.cv,
            output_columns=["站点", "是否成功"],
            derived_columns=[{
                "set_column": "是否成功",
                "true_value": "是",
                "false_value": "否",
                "logic": "and",
                "conditions": [{"column": "状态", "op": "eq", "values": ["成功"]}],
            }],
        )
        out = execute_mechanical(TABLES, plan, [], self.cv)
        self.assertFalse(out.get("contract_rejected"), out.get("plan_notes"))
        by = {r["站点"]: r["是否成功"] for r in out["rows"]}
        self.assertEqual(by["A站"], "是")
        self.assertEqual(by["B站"], "否")

    def test_missing_primary_rejects(self):
        plan = _base(self.cv, primary_table="nope", output_columns=["站点"])
        out = execute_plan(TABLES, plan, notes=[])
        self.assertTrue(out.get("contract_rejected"))
        self.assertEqual(out["rows"], [])

    def test_unknown_filter_op_rejects(self):
        plan = _base(
            self.cv,
            output_columns=["站点"],
            keep_row_when=[{"column": "状态", "op": "fuzzy", "value": "成功"}],
        )
        out = execute_plan(TABLES, plan, notes=[])
        self.assertTrue(out.get("contract_rejected"))
        self.assertEqual(out["rows"], [])

    def test_v1_0_0_plan_rejected(self):
        cv0 = load_contract("v1.0.0")
        plan = {
            "contract_version": cv0.version,
            "contract_hash": cv0.file_hash,
            "status": "ready",
            "primary_table": "left",
            "output_columns": ["站点"],
            "joins": [{
                "right_table": "right",
                "left_column": "站点",
                "right_column": "站点",
                "how": "left",
            }],
            "keep_row_when": [{"column": "状态", "op": "eq", "value": "成功"}],
        }
        out = execute_mechanical(TABLES, plan, [], cv0)
        self.assertTrue(out.get("contract_rejected"))
        self.assertEqual(out.get("contract_status"), "version_rejected")
        self.assertEqual(out["rows"], [])

    def test_traces_present(self):
        plan = _base(self.cv, output_columns=["站点"])
        out = execute_mechanical(TABLES, plan, [], self.cv)
        self.assertFalse(out.get("contract_rejected"), out.get("plan_notes"))
        self.assertTrue(out.get("traces"))
        self.assertTrue(all(t.get("op_id") == "project" for t in out["traces"]))
        self.assertTrue(out.get("trace_ok"))


if __name__ == "__main__":
    unittest.main()
