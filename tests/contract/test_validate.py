# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from pipeline.synth_contract.registry import load_contract
from pipeline.synth_contract.scope import Scope
from pipeline.synth_contract.validate import (
    pick_state,
    validate_binding,
    validate_dsl_continuity,
    validate_enum,
    validate_global_judgment,
    validate_join_spec,
    validate_plan_envelope,
    validate_region_cover,
    validate_row_cover,
    validate_verifiable_basis,
)


class TestValidate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cv = load_contract("v1.0.0")

    def test_enum(self):
        self.assertTrue(validate_enum(self.cv, "join_type", "left").ok)
        self.assertFalse(validate_enum(self.cv, "join_type", "magic").ok)

    def test_plan_envelope(self):
        plan = {
            "contract_version": "v1.0.0",
            "contract_hash": self.cv.file_hash,
            "status": "ready",
            "required_judgment_types": ["binding", "join"],
        }
        r = validate_plan_envelope(self.cv, plan)
        self.assertTrue(r.ok, r.issues)

        bad = dict(plan, contract_hash="nope")
        self.assertFalse(validate_plan_envelope(self.cv, bad).ok)

    def test_binding_catalog_and_scope(self):
        cat = {"#1"}
        known = {"#1": Scope("f", "S", "r0")}
        binding = {
            "scope": {"file_id": "f", "sheet": "S", "region": "r0"},
            "source_file": "f",
            "source_sheet": "S",
            "orig_col_name": "A",
            "cell_coord_or_range": "A1",
            "target_field_id": "#1",
            "target_field_name": "A",
            "confidence": 0.9,
        }
        r = validate_binding(self.cv, binding, cat, known)
        self.assertTrue(r.ok, r.issues)

        wrong_scope = dict(binding)
        wrong_scope["scope"] = {"file_id": "f", "sheet": "S", "region": "r9"}
        r2 = validate_binding(self.cv, wrong_scope, cat, known)
        self.assertFalse(r2.ok)

        deg = dict(binding, confidence=0.0)
        self.assertFalse(validate_binding(self.cv, deg, cat, known).ok)

    def test_region_cover(self):
        sheet = {"A1", "A2", "B1", "B2"}
        regions = [
            {
                "region_id": "r0",
                "coords": ["A1", "A2"],
                "boundary_continuous": True,
                "scope": {"file_id": "f", "sheet": "S", "region": "r0"},
            },
            {
                "region_id": "r1",
                "coords": ["B1", "B2"],
                "boundary_continuous": True,
                "scope": {"file_id": "f", "sheet": "S", "region": "r1"},
            },
        ]
        self.assertTrue(validate_region_cover(regions, sheet).ok)

        overlap = [
            dict(regions[0]),
            {
                "region_id": "r1",
                "coords": ["A2", "B1"],
                "boundary_continuous": True,
                "scope": {"file_id": "f", "sheet": "S", "region": "r1"},
            },
        ]
        self.assertFalse(validate_region_cover(overlap, sheet).ok)

    def test_dsl_continuity(self):
        steps = [
            {
                "step_id": "1",
                "op": "read_range",
                "in_scope": {"file_id": "f", "sheet": "S", "region": "r0"},
                "out_scope": {"file_id": "f", "sheet": "S", "region": "r0"},
            },
            {
                "step_id": "2",
                "op": "filter",
                "in_scope": {"file_id": "f", "sheet": "S", "region": "r0"},
                "out_scope": {"file_id": "f", "sheet": "S", "region": "r0"},
            },
        ]
        self.assertTrue(validate_dsl_continuity(steps).ok)

        broken = [
            steps[0],
            {
                "step_id": "2",
                "op": "filter",
                "in_scope": {"file_id": "f", "sheet": "S", "region": "r9"},
                "out_scope": {"file_id": "f", "sheet": "S", "region": "r9"},
            },
        ]
        self.assertFalse(validate_dsl_continuity(broken).ok)

    def test_join_spec(self):
        join = {
            "scope": {"file_id": "f", "sheet": "S", "region": "r0"},
            "left_scope": {"file_id": "f", "sheet": "S", "region": "r0"},
            "right_scope": {"file_id": "f", "sheet": "T", "region": "r1"},
            "left_catalog_id": "#1",
            "right_catalog_id": "#2",
            "join_type": "left",
            "expected_cardinality": "1:1",
            "grain": "#1",
        }
        self.assertTrue(validate_join_spec(self.cv, join, {"#1", "#2"}).ok)
        self.assertFalse(validate_join_spec(self.cv, join, {"#1"}).ok)

    def test_judgment_anchor_full(self):
        coords = {"A1", "A2", "B1"}
        j = {
            "judgment_id": "j1",
            "global_judgment_type": "region_cover",
            "based_on": "full",
            "confidence": 0.95,
            "on_low_confidence": "await_user",
            "declared_scope": {"file_id": "f", "sheet": "S", "region": "r0"},
            "anchor": {
                "evidence_coords": ["A1", "A2", "B1"],
                "declared_scope_coords": ["A1", "A2", "B1"],
            },
        }
        r = validate_global_judgment(self.cv, j, coords, {}, {})
        self.assertTrue(r.ok, r.issues)

        bad = dict(j)
        bad["anchor"] = {"evidence_coords": ["A1", "ZZ"]}
        self.assertFalse(
            validate_global_judgment(self.cv, bad, coords, {}, {}).ok
        )

    def test_judgment_anchor_summary(self):
        j = {
            "judgment_id": "j2",
            "global_judgment_type": "binding",
            "based_on": "summary",
            "confidence": 0.9,
            "on_low_confidence": "await_user",
            "declared_scope": {"file_id": "f", "sheet": "S", "region": "r0"},
            "anchor": {"summary_hash": "abc", "scope_key": "f/S/r0"},
        }
        ok = validate_global_judgment(
            self.cv, j, set(), {"f/S/r0": "abc"}, {}
        )
        self.assertTrue(ok.ok, ok.issues)
        bad = validate_global_judgment(
            self.cv, j, set(), {"f/S/r0": "other"}, {}
        )
        self.assertFalse(bad.ok)

    def test_row_cover(self):
        src = {"row1", "row2"}
        records = [
            {
                "scope": {"file_id": "f", "sheet": "S", "region": "r0"},
                "source_row_id": "row1",
                "disposition": "processed",
            },
            {
                "scope": {"file_id": "f", "sheet": "S", "region": "r0"},
                "source_row_id": "row2",
                "disposition": "excluded",
            },
        ]
        self.assertTrue(validate_row_cover(records, src, self.cv).ok)
        self.assertFalse(validate_row_cover(records[:1], src, self.cv).ok)

    def test_verifiable_basis(self):
        objects = {
            "b1": {"object_type": "budget", "limit": 10},
        }
        basis = {
            "source_type": "user_budget",
            "source_ref": "b1",
            "value": 10,
            "compare_field": "limit",
            "compare_op": "eq",
        }
        self.assertTrue(
            validate_verifiable_basis(self.cv, basis, objects).ok
        )
        basis_bad = dict(basis, value=9)
        self.assertFalse(
            validate_verifiable_basis(self.cv, basis_bad, objects).ok
        )

    def test_pick_state(self):
        r = pick_state(self.cv, ["pass", "await_user"])
        # exclusive → fail
        self.assertFalse(r.ok)
        r2 = pick_state(self.cv, ["await_user", "over_budget"])
        self.assertTrue(r2.ok)
        selected = [i for i in r2.issues if i.code == "state_selected"]
        self.assertEqual(selected[0].state_id, "over_budget")


if __name__ == "__main__":
    unittest.main()
