# -*- coding: utf-8 -*-
"""v1.9.0 CURRENT merge: query-engine enums/schemas present; numerics null."""
from __future__ import annotations

import unittest

from pipeline.synth_contract.registry import load_contract, read_current_pointer


class TestQueryEngineV190(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cv = load_contract()
        cls.ct = cls.cv.data.get("cross_table") or {}
        cls.enums = cls.ct.get("enums") or {}

    def test_current_is_v190(self):
        self.assertEqual(read_current_pointer(), "v1.9.0")
        self.assertEqual(self.cv.version, "v1.9.0")

    def test_structure_status_queryable(self):
        self.assertEqual(
            self.enums.get("structure_status_queryable"),
            ["layers_recognized"],
        )

    def test_domain_cap_needs_user_statuses(self):
        self.assertEqual(
            set(self.enums.get("domain_cap_needs_user_statuses") or []),
            {"over_cap", "unavailable"},
        )

    def test_query_fail_locus_and_portrait_gate_subreason(self):
        loci = set(self.enums.get("QueryFailLocus") or [])
        self.assertIn("portrait_gate", loci)
        self.assertIn("budget", loci)
        self.assertIn("group_latest", loci)
        subs = set(self.enums.get("PortraitGateSubreason") or [])
        self.assertIn("structure_status", subs)
        self.assertIn("unknown", subs)

    def test_query_trace_kind_and_routing_extension(self):
        qtk = set(self.enums.get("query_trace_kind") or [])
        for k in (
            "query_plan",
            "query_exec_step",
            "query_review",
            "query_user_reply",
            "query_budget_verdict",
        ):
            self.assertIn(k, qtk)
            self.assertIn(k, self.enums.get("routing_trace_kind") or [])

    def test_result_projection_not_executable_op(self):
        note = self.ct.get("query_operator_catalog_notes") or {}
        self.assertTrue(note.get("result_projection_is_not_an_executable_op"))
        cat = self.ct.get("query_operator_catalog") or {}
        self.assertNotIn("op_result_projection", cat)
        self.assertIn("ResultProjection", self.ct)

    def test_numeric_slots_null(self):
        coeffs = self.ct.get("complexity_bound_coeffs") or {}
        self.assertTrue(coeffs)
        self.assertTrue(all(v is None for v in coeffs.values()))
        budget = self.ct.get("query_exec_budget") or {}
        self.assertIsNone(budget.get("max_cost_per_step"))
        self.assertIsNone(self.ct.get("query_ask_budget"))
        self.assertIsNone(self.ct.get("term_expand_domain_enum_cap"))

    def test_no_memory_or_embedding_keys(self):
        for k in self.ct:
            self.assertFalse(str(k).startswith("memory_"))
            self.assertNotIn("embedding", str(k).lower())
        for k in self.enums:
            self.assertFalse(str(k).startswith("memory_"))
            self.assertNotIn("embedding", str(k).lower())


if __name__ == "__main__":
    unittest.main()
