# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from pipeline.block_cross_table.query_engine.run_query_tool import (
    _association_quality_feedback,
)


class TestAssocQualityGate(unittest.TestCase):
    def test_join_zero_rows_triggers(self):
        plan = {
            "steps": [
                {"op": "op_filter", "step_id": "s1", "output": "a"},
                {
                    "op": "op_join",
                    "step_id": "s2",
                    "output": "b",
                    "args": {"left_on": "x", "right_on": "y", "mode": "contains"},
                },
            ]
        }
        qe = {
            "step_traces": [
                {"op": "op_filter", "status": "ok", "nrows": 100},
                {"op": "op_join", "status": "ok", "nrows": 0},
            ],
            "result_table": {"columns": ["x"], "rows": []},
        }
        fb = _association_quality_feedback(plan, qe)
        self.assertIsNotNone(fb)
        self.assertIn("op_join", fb or "")
        self.assertIn("0", fb or "")

    def test_interval_zero_triggers(self):
        plan = {
            "steps": [
                {"op": "op_interval_build", "step_id": "s1", "output": "iv", "args": {}},
            ]
        }
        qe = {
            "step_traces": [
                {"op": "op_interval_build", "status": "ok", "nrows": 0},
            ],
            "result_table": {"columns": [], "rows": []},
        }
        fb = _association_quality_feedback(plan, qe)
        self.assertIsNotNone(fb)
        self.assertIn("interval", (fb or "").lower())

    def test_ok_join_with_rows_no_feedback(self):
        plan = {
            "steps": [
                {"op": "op_join", "step_id": "s1", "output": "b", "args": {}},
            ]
        }
        qe = {
            "step_traces": [{"op": "op_join", "status": "ok", "nrows": 10}],
            "result_table": {
                "columns": ["a", "负责人"],
                "rows": [{"a": 1, "负责人": "张三"}],
            },
        }
        self.assertIsNone(_association_quality_feedback(plan, qe))

    def test_sample_overlap_surfaces_better_key(self):
        plan = {
            "steps": [
                {
                    "op": "op_filter",
                    "inputs": ["src:fact"],
                    "output": "f",
                    "args": {},
                },
                {
                    "op": "op_join",
                    "inputs": ["f"],
                    "output": "j",
                    "args": {
                        "right_input": "src:owner",
                        "left_on": "站点",
                        "right_on": "站点简称",
                        "mode": "contains",
                    },
                },
            ]
        }
        qe = {
            "step_traces": [
                {"op": "op_filter", "status": "ok", "nrows": 2},
                {"op": "op_join", "status": "ok", "nrows": 0},
            ],
            "result_table": {"columns": ["站点"], "rows": []},
        }
        tables = {
            "fact": {
                "columns": ["站点"],
                "rows": [{"站点": "110kV西河变电站"}, {"站点": "220kV阳朔变电站"}],
            },
            "owner": {
                "columns": ["站点简称", "匹配核", "负责人"],
                "rows": [
                    {"站点简称": "西河站", "匹配核": "西河", "负责人": "甲"},
                    {"站点简称": "阳朔站", "匹配核": "阳朔", "负责人": "乙"},
                ],
            },
        }
        fb = _association_quality_feedback(plan, qe, tables_by_id=tables)
        self.assertIsNotNone(fb)
        self.assertIn("SAMPLE_OVERLAP", fb or "")
        self.assertIn("匹配核", fb or "")


if __name__ == "__main__":
    unittest.main()
