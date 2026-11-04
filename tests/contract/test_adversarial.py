# -*- coding: utf-8 -*-
"""Adversarial cases: structurally legal inputs that must still fail."""
from __future__ import annotations

import unittest

from pipeline.synth_contract.boot import ensure_contract_library, reset_boot_for_tests
from pipeline.synth_contract.accuracy import (
    classify_empty,
    compare_empty,
    duplicate_keys,
    replay_norm_steps,
    validate_block_rows,
    validate_deriv_edges,
    validate_key_norm_pair,
    validate_norm_spec,
    validate_review_anchor,
    validate_row_accounting,
)
from pipeline.synth_contract.registry import load_contract
from pipeline.stage3_processor.table_synth import execute_plan


class TestAdversarial(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_boot_for_tests()
        ensure_contract_library()
        cls.cv = load_contract("v1.1.0")

    def test_grouping_sep_replay(self):
        steps = [{"op": "strip_grouping_sep", "param": ","}, {"op": "trim"}]
        allowed = set(self.cv.data["norm_ops"])
        self.assertEqual(replay_norm_steps("1,200", steps, allowed), "1200")
        spec = {
            "idempotent": True,
            "steps": steps,
        }
        self.assertTrue(validate_norm_spec(self.cv, spec, "1,200", "1200").ok)
        bad = validate_norm_spec(self.cv, spec, "1,200", "1,200")
        self.assertFalse(bad.ok)

    def test_norm_not_idempotent_rejected(self):
        # scale is idempotent on already-scaled text only if float round-trips;
        # a spec that is not flagged idempotent is rejected before replay.
        spec = {"idempotent": False, "steps": [{"op": "trim"}]}
        self.assertFalse(validate_norm_spec(self.cv, spec, " a ", "a").ok)

    def test_empty_classes_do_not_cross_equal(self):
        self.assertEqual(compare_empty("vacant", "unknown", "eq"), "false")
        self.assertEqual(compare_empty("vacant", "vacant", "eq"), "true")
        self.assertEqual(compare_empty("na", "na", "le"), "incomparable")
        mapping = [{"raw": "", "enum": "vacant"}, {"raw": "未知", "enum": "unknown"},
                   {"raw": "不适用", "enum": "na"}]
        self.assertEqual(classify_empty("", mapping), "vacant")
        self.assertEqual(
            classify_empty("N/A", mapping, candidates=["", "未知", "不适用", "N/A"]),
            "unclassified_empty",
        )
        self.assertEqual(
            compare_empty("unclassified_empty", "value", "eq"), "incomparable")

    def test_missing_row_rejected(self):
        acc = {
            "scope": {"file_id": "f", "sheet": "S", "region": "r0"},
            "input_row_coords": ["1", "2", "3"],
            "processed": ["1"],
            "excluded": ["2"],
            "discarded": [],
        }
        self.assertFalse(validate_row_accounting(acc).ok)
        acc["discarded"] = ["3"]
        self.assertTrue(validate_row_accounting(acc).ok)

    def test_overlap_rows_rejected(self):
        acc = {
            "scope": {"region": "r0"},
            "input_row_coords": ["1", "2"],
            "processed": ["1", "2"],
            "excluded": ["2"],
            "discarded": [],
        }
        self.assertFalse(validate_row_accounting(acc).ok)

    def test_block_boundary(self):
        region = {"1", "2", "3", "4"}
        ok = validate_block_rows(
            [{"row_coords": ["1", "2"]}, {"row_coords": ["3", "4"]}], region)
        self.assertTrue(ok.ok)
        bad = validate_block_rows(
            [{"row_coords": ["1", "2"]}, {"row_coords": ["2", "3"]}], region)
        self.assertFalse(bad.ok)
        gap = validate_block_rows(
            [{"row_coords": ["1"]}, {"row_coords": ["2"]}], region)
        self.assertFalse(gap.ok)

    def test_deriv_edge_must_land_in_snapshot(self):
        edges = [{"kind": "cell_edge", "op_id": "project", "input_coord": "Z9"}]
        r = validate_deriv_edges(edges, {"A1"}, {"project"})
        self.assertFalse(r.ok)
        ok = validate_deriv_edges(
            [{"kind": "cell_edge", "op_id": "project", "input_coord": "A1"}],
            {"A1"}, {"project"})
        self.assertTrue(ok.ok)

    def test_key_norm_steps_must_match(self):
        left = [{"op": "trim"}, {"op": "casefold"}]
        right = [{"op": "trim"}]
        self.assertFalse(validate_key_norm_pair(left, right).ok)
        self.assertTrue(validate_key_norm_pair(left, list(left)).ok)

    def test_review_reread_cannot_pass(self):
        plan = {"sample_manifest_id": "m1", "coords": ["A1"], "based_on": "summary"}
        review = {"sample_manifest_id": "m1", "coords": ["A1"], "based_on": "summary"}
        r = validate_review_anchor("disjoint_sample", plan, review)
        self.assertFalse(r.ok)
        self.assertTrue(any(i.state_id == "review_reread" for i in r.issues))
        review2 = {"sample_manifest_id": "m2", "coords": ["B1"], "based_on": "sample"}
        self.assertTrue(validate_review_anchor("disjoint_sample", plan, review2).ok)
        self.assertTrue(validate_review_anchor("cross_basis", plan, review2).ok)
        self.assertFalse(validate_review_anchor("cross_basis", plan, plan).ok)

    def test_duplicate_key_flag(self):
        self.assertTrue(duplicate_keys(["a", "a"], "1:1"))
        self.assertFalse(duplicate_keys(["a", "b"], "1:1"))

    def test_execute_without_boot_rejects(self):
        reset_boot_for_tests()
        try:
            tables = {"left": {"name": "left", "columns": ["A"], "rows": [{"A": "1"}]}}
            out = execute_plan(tables, {"output_columns": ["A"], "primary_table": "left"}, [])
            self.assertTrue(out.get("contract_rejected"))
            self.assertEqual(out.get("rows"), [])
            self.assertTrue(
                any("boot" in str(i.get("path") or "") or "boot" in str(i.get("code") or "")
                    for i in (out.get("contract_issues") or []))
                or any("契约库" in str(n) for n in (out.get("plan_notes") or []))
            )
        finally:
            ensure_contract_library()

    def test_declared_bad_accounting_rejects_execute(self):
        from tests.contract.plan_helpers import to_ops_plan
        plan = to_ops_plan({
            "status": "ready",
            "primary_table": "left",
            "output_columns": ["A"],
            "row_accounting": {
                "scope": {"file_id": "f", "sheet": "S", "region": "r0"},
                "input_row_coords": ["1", "2"],
                "processed": ["1"],
                "excluded": [],
                "discarded": [],
            },
        })
        plan["contract_version"] = self.cv.version
        plan["contract_hash"] = self.cv.file_hash
        tables = {"left": {"name": "left", "columns": ["A"], "rows": [{"A": "1"}, {"A": "2"}]}}
        out = execute_plan(tables, plan, [])
        self.assertTrue(out.get("contract_rejected"))
        self.assertEqual(out.get("rows"), [])

    def test_unknown_enum_rejected_not_ignored(self):
        from pipeline.synth_contract.validate import validate_enum
        r = validate_enum(self.cv, "based_on", "telepathy")
        self.assertFalse(r.ok)
        self.assertTrue(any(i.code == "enum_unknown" for i in r.issues))


if __name__ == "__main__":
    unittest.main()
