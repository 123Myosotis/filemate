# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from pipeline.synth_contract.boot import ensure_contract_library, reset_boot_for_tests
from pipeline.synth_contract.gate import gate_plan, reject_payload
from pipeline.synth_contract.registry import load_contract
from pipeline.stage3_processor.table_synth import execute_plan
from tests.contract.plan_helpers import to_ops_plan


def _stamp_ops(cv, **extra):
    plan = {"status": "ready", "primary_table": "t1", "output_columns": ["A"]}
    plan.update(extra)
    plan = to_ops_plan(plan)
    plan["contract_version"] = cv.version
    plan["contract_hash"] = cv.file_hash
    return plan


class TestGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_boot_for_tests()
        ensure_contract_library()
        cls.cv = load_contract("v1.1.0")

    def test_reject_missing_version(self):
        r, cv = gate_plan({"output_columns": ["A"]}, {})
        self.assertFalse(r.ok)
        self.assertIsNone(cv)
        self.assertTrue(any(i.code == "contract_version_missing" for i in r.issues))

    def test_accept_minimal_ops_envelope(self):
        plan = _stamp_ops(self.cv)
        r, cv = gate_plan(plan, {})
        self.assertTrue(r.ok, [(i.code, i.message) for i in r.issues])
        self.assertEqual(cv.version, self.cv.version)

    def test_reject_v1_0_0(self):
        cv0 = load_contract("v1.0.0")
        plan = {
            "contract_version": cv0.version,
            "contract_hash": cv0.file_hash,
            "status": "ready",
            "primary_table": "t1",
            "output_columns": ["A"],
            "keep_row_when": [],
            "joins": [],
        }
        r, _ = gate_plan(plan, {})
        self.assertFalse(r.ok)
        self.assertTrue(any(i.state_id == "version_rejected" for i in r.issues))

    def test_reject_await_user(self):
        plan = _stamp_ops(self.cv, status="await_user")
        r, _ = gate_plan(plan, {})
        self.assertFalse(r.ok)
        self.assertTrue(any(i.state_id == "await_user" for i in r.issues))

    def test_reject_bad_hash(self):
        plan = {
            "contract_version": self.cv.version,
            "contract_hash": "0" * 64,
            "status": "ready",
            "ops": [{"op": "project", "project_ref": "p1"}],
            "project_specs": [{"id": "p1", "output_columns": ["A"]}],
        }
        r, cv = gate_plan(plan, {})
        self.assertFalse(r.ok)
        self.assertIsNone(cv)

    def test_bindings_scope_check(self):
        plan = _stamp_ops(self.cv)
        plan["catalog"] = [
            {
                "catalog_id": "#1",
                "scope": {"file_id": "f", "sheet": "S", "region": "r0"},
            }
        ]
        plan["bindings"] = [
            {
                "scope": {"file_id": "f", "sheet": "S", "region": "r0"},
                "source_file": "f",
                "source_sheet": "S",
                "orig_col_name": "A",
                "cell_coord_or_range": "A1",
                "target_field_id": "#1",
                "target_field_name": "A",
                "confidence": 0.9,
            }
        ]
        r, _ = gate_plan(plan, {})
        self.assertTrue(r.ok, [(i.code, i.message) for i in r.issues])

        plan["bindings"][0]["scope"]["region"] = "r9"
        r2, _ = gate_plan(plan, {})
        self.assertFalse(r2.ok)

    def test_execute_plan_rejects_without_contract(self):
        tables = {
            "t1": {
                "name": "t1",
                "columns": ["A"],
                "rows": [{"A": "1"}],
            }
        }
        out = execute_plan(tables, {"output_columns": ["A"]}, notes=[])
        self.assertTrue(out.get("contract_rejected"))
        self.assertEqual(out.get("rows"), [])
        self.assertEqual(out.get("_mechanical"), False)
        self.assertEqual(out.get("traces"), [])

    def test_execute_plan_passes_ops(self):
        plan = _stamp_ops(self.cv)
        tables = {
            "t1": {
                "name": "t1",
                "columns": ["A"],
                "rows": [{"A": "1"}],
                "role": "record",
                "matchable": True,
            }
        }
        out = execute_plan(tables, plan, notes=[], api_key="")
        self.assertFalse(out.get("contract_rejected"), out.get("plan_notes"))

    def test_reject_payload_shape(self):
        r, _ = gate_plan({}, {})
        payload = reject_payload([], r, {})
        self.assertEqual(payload["columns"], [])
        self.assertEqual(payload["rows"], [])
        self.assertTrue(payload["contract_rejected"])
        self.assertEqual(payload.get("_mechanical"), False)
        self.assertEqual(payload.get("traces"), [])
        self.assertIn("contract_status", payload)


if __name__ == "__main__":
    unittest.main()
