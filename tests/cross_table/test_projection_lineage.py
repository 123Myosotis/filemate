# -*- coding: utf-8 -*-
"""Projection allows any loaded-table column (no lineage whitelist)."""
from __future__ import annotations

import unittest

from pipeline.block_cross_table.query_engine.qp_ai import (
    _projection_allowed_ids,
    repair_projection_drop_unknown,
)


class TestProjectionAllowsAllLoadedTables(unittest.TestCase):
    def test_unused_table_column_still_allowed(self):
        tables = {
            "fact": {"columns": ["站点", "设备名称"], "rows": []},
            "other": {"columns": ["资产名称"], "rows": []},
        }
        plan = {
            "status": "plan_ready",
            "steps": [
                {
                    "step_id": "s1",
                    "op": "op_filter",
                    "inputs": ["src:fact"],
                    "output": "f1",
                    "args": {},
                }
            ],
            "result_projection": {
                "columns": [
                    {"column_id": "资产名称", "provenance": {"utterance_span": "设备"}},
                    {"column_id": "站点", "provenance": {"utterance_span": "站点"}},
                ]
            },
        }
        allowed = _projection_allowed_ids(plan, tables)
        self.assertIn("资产名称", allowed)
        self.assertIn("设备名称", allowed)
        dropped = repair_projection_drop_unknown(plan, tables)
        self.assertEqual(dropped, [])
        ids = [c["column_id"] for c in plan["result_projection"]["columns"]]
        self.assertIn("资产名称", ids)


if __name__ == "__main__":
    unittest.main()
