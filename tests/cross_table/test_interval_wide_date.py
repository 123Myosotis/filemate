# -*- coding: utf-8 -*-
"""op_interval_build wide_date_headers: auto-pick coercible date row."""
from __future__ import annotations

import unittest
from datetime import datetime

from pipeline.block_cross_table.query_engine.qe import _exec_interval_build


class TestWideDateHeadersAutoRow(unittest.TestCase):
    def test_auto_skips_title_and_day_index_rows(self):
        # Row 0: day-of-month ints (not Excel serials) — not coercible as dates
        # Row 1: Excel serials for consecutive days
        # Row 2+: values under those day columns
        cols = ["label", "d0", "d1", "d2"]
        rows = [
            {"label": "day_index", "d0": 1, "d1": 2, "d2": 3},
            {"label": "excel_serial", "d0": 45937, "d1": 45938, "d2": 45939},
            {"label": "person_a", "d0": "Alice", "d1": "Bob", "d2": ""},
        ]
        src = {"columns": cols, "rows": rows}
        out = _exec_interval_build(
            {
                "mode": "wide_date_headers",
                "date_row_index": 0,  # wrong on purpose
                "value_column_id": "on_duty",
                "keep_columns": ["label"],
            },
            source_table=src,
        )
        self.assertGreaterEqual(len(out["rows"]), 2)
        starts = [r["interval_start"] for r in out["rows"]]
        self.assertTrue(all(isinstance(s, datetime) for s in starts))
        values = {r["on_duty"] for r in out["rows"]}
        self.assertIn("Alice", values)
        self.assertIn("Bob", values)
        # day-index ints must not be emitted as on-duty values
        self.assertNotIn(1, values)
        self.assertNotIn(2, values)
        self.assertNotIn(3, values)


if __name__ == "__main__":
    unittest.main()
