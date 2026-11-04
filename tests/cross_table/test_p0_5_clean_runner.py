# -*- coding: utf-8 -*-
"""P0-5: clean runner reads case inputs only; no expected business-field asserts."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.m10_clean_runner_lib import (
    CASES_DIR,
    load_case,
    run_qp_case,
    structural_checks,
)


class TestP05CleanRunner(unittest.TestCase):
    def test_cases_have_no_expected_business_fields(self):
        """cases/*.json must not carry expected_columns / expected_cell_values."""
        forbid = {"expected_columns", "expected_cell_values", "expected_column"}
        n = 0
        for p in sorted(CASES_DIR.glob("*.json")):
            n += 1
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertTrue(forbid.isdisjoint(data.keys()), p.name)
        self.assertGreaterEqual(n, 7)

    def test_runner_reads_case_input_not_expected_biz(self):
        """run_qp_case uses case utterance/expected_status only; checks are structural."""
        case = load_case("m10_1_c.json")
        self.assertIn("utterance", case)
        self.assertNotIn("expected_columns", case)
        self.assertNotIn("expected_cell_values", case)

        def fake_qp(**kwargs):
            self.assertEqual(kwargs.get("utterance"), case["utterance"])
            return {
                "status": "needs_user",
                "plan": {
                    "status": "needs_user",
                    "fail_locus": "other",
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
                },
                "ai_calls": 1,
                "five_part": {
                    "speculation": "s",
                    "evidence": "e",
                    "what_might_be_wrong": "w",
                    "next_step": "n",
                    "please_confirm_or_correct": "p",
                    "fifth_segment_separate": True,
                    "prompt_parts_for_callback": ["s", "e", "w", "n"],
                },
            }

        out = run_qp_case(
            case=case,
            portraits=[],
            catalog={"operators": {}},
            tables={"t": {"columns": ["c"], "rows": []}},
            api_key="k",
            memories=[],
            qp_call_ai=fake_qp,
        )
        checks = out["checks"]
        # only structural keys
        for k in checks:
            self.assertNotIn("column", k.lower())
            self.assertNotIn("cell", k.lower())
        self.assertIn("status", checks)
        self.assertIn("ops", checks)
        self.assertIn("five_part_keys_present", checks)
        sc = structural_checks(
            status="needs_user",
            expected_status=case.get("expected_status"),
            plan=out["plan"],
            ana=out["analysis"],
            ai_calls=1,
        )
        self.assertTrue(sc["status_matches_expected"])

    def test_b6a_literals_are_input_params_not_expected(self):
        """b6a/class5 case files provide input literals, not expected assert fields."""
        for name in ("b6a_literals.json", "b6_class5_literals.json"):
            data = load_case(name)
            self.assertIsNone(data.get("expected_status"))
            self.assertNotIn("expected_columns", data)
            # input-side keys may exist
            self.assertTrue(
                data.get("filter_column")
                or data.get("col_guess_by_role")
                or data.get("table_paths")
            )


if __name__ == "__main__":
    unittest.main()
