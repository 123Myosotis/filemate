# -*- coding: utf-8 -*-
"""后续-2 第四批：trace 审计容量键 + LayerRecognizeTraceBody（v1.7.0）。"""
from __future__ import annotations

import copy
import unittest

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.layer_recognize_trace import (
    LAYER_RECOGNIZE_TRACE_BODY_REQUIRED,
    TRACE_RAW_MAX_CHARS_KEY,
    read_layer_recognize_trace_raw_max_chars,
)
from pipeline.synth_contract.registry import load_contract

_BODY_REQUIRED = [
    "ask_usage_after",
    "prompt_hash",
    "prompt_chars",
    "prompt_bytes",
    "prompt_shape",
    "prompt_full",
    "prompt_truncated",
    "raw_full",
    "raw_chars",
    "raw_truncated",
    "validate_reasons",
]


class TestRoutingBatch4(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cv = load_contract("v1.7.0")
        cls.ct = cls.cv.data.get("cross_table") or {}

    def test_v170_file_still_loadable(self):
        """第四批文件保留可加载；CURRENT 由 batch5 推进。"""
        self.assertEqual(self.cv.version, "v1.7.0")
        self.assertIsInstance(self.ct, dict)

    def test_trace_raw_max_chars_loadable_is_65536(self):
        self.assertEqual(self.ct.get(TRACE_RAW_MAX_CHARS_KEY), 65536)
        self.assertEqual(
            read_layer_recognize_trace_raw_max_chars(self.cv.data), 65536
        )

    def test_trace_raw_max_chars_missing_contract_invalid(self):
        cross = copy.deepcopy(self.ct)
        del cross[TRACE_RAW_MAX_CHARS_KEY]
        with self.assertRaises(CrossTableError) as ctx:
            read_layer_recognize_trace_raw_max_chars({"cross_table": cross})
        self.assertEqual(ctx.exception.code, "cross_table_contract_invalid")
        self.assertIn(TRACE_RAW_MAX_CHARS_KEY, ctx.exception.message)

    def test_layer_recognize_trace_body_required_exactly_11(self):
        body = self.ct.get("LayerRecognizeTraceBody") or {}
        required = list(body.get("required") or [])
        self.assertEqual(len(required), 11)
        self.assertEqual(required, _BODY_REQUIRED)
        self.assertEqual(
            tuple(required), LAYER_RECOGNIZE_TRACE_BODY_REQUIRED
        )


if __name__ == "__main__":
    unittest.main()
