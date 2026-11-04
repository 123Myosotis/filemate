# -*- coding: utf-8 -*-
"""后续-2 第五批：提案分片 / 合并 schema（v1.8.0）。"""
from __future__ import annotations

import copy
import unittest

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.layer_recognize_limits import (
    PROPOSE_INPUT_MAX_CHARS_KEY,
    read_layer_recognize_propose_input_max_chars,
)
from pipeline.synth_contract.registry import load_contract, read_current_pointer

_KEEP = {"contains", "overlap", "equal"}
_NEW_KINDS = {"layer_propose_slice", "layer_merge_layers"}
_NEW_REASONS = {
    "cross_slice_layer_unresolved",
    "cross_slice_merge_invalid",
    "cross_slice_overall_sanity_failed",
    "cross_slice_merge_input_over_capacity",
}
_MERGE_IN_REQ = ["slices", "cross_slice_contains_candidates", "instructions"]
_MERGE_OUT_REQ = [
    "passed",
    "reason",
    "global_rectangle_layer_map",
    "global_layer_order",
    "group_label_decls",
    "dimension_membership",
    "header_completeness",
    "overall_sanity",
]


class TestRoutingBatch5(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cv = load_contract("v1.8.0")
        cls.ct = cls.cv.data.get("cross_table") or {}

    def test_current_points_to_v190(self):
        """CURRENT advanced to v1.9.0 (query-engine merge); v1.8.0 file still loadable."""
        self.assertEqual(read_current_pointer(), "v1.9.0")
        boot = load_contract()
        self.assertEqual(boot.version, "v1.9.0")
        self.assertEqual(load_contract("v1.8.0").version, "v1.8.0")

    def test_propose_input_max_chars_loadable_65536(self):
        self.assertEqual(self.ct.get(PROPOSE_INPUT_MAX_CHARS_KEY), 65536)
        self.assertEqual(
            read_layer_recognize_propose_input_max_chars(self.cv.data), 65536
        )

    def test_propose_input_max_chars_missing_contract_invalid(self):
        cross = copy.deepcopy(self.ct)
        del cross[PROPOSE_INPUT_MAX_CHARS_KEY]
        with self.assertRaises(CrossTableError) as ctx:
            read_layer_recognize_propose_input_max_chars({"cross_table": cross})
        self.assertEqual(ctx.exception.code, "cross_table_contract_invalid")

    def test_new_kinds_in_list_and_subset_routing(self):
        kinds = set(self.ct.get("layer_recognize_kinds") or [])
        routing = set((self.ct.get("enums") or {}).get("routing_trace_kind") or [])
        self.assertTrue(_NEW_KINDS.issubset(kinds))
        self.assertTrue(kinds.issubset(routing))

    def test_propose_slice_relation_keep_exactly_three(self):
        keep = list((self.ct.get("enums") or {}).get("propose_slice_relation_keep") or [])
        self.assertEqual(set(keep), _KEEP)
        self.assertEqual(len(keep), 3)

    def test_new_reason_codes_in_error_codes(self):
        codes = set(self.ct.get("error_codes") or [])
        self.assertTrue(_NEW_REASONS.issubset(codes))

    def test_merge_input_result_required_complete(self):
        mi = self.ct.get("LayerRecognizeMergeInput") or {}
        mr = self.ct.get("LayerRecognizeMergeResult") or {}
        self.assertEqual(list(mi.get("required") or []), _MERGE_IN_REQ)
        self.assertEqual(list(mr.get("required") or []), _MERGE_OUT_REQ)
        sanity = (mr.get("properties") or {}).get("overall_sanity") or {}
        self.assertEqual(set(sanity.get("required") or []), {"passed", "reason"})
        ms = self.ct.get("MergeSliceSummary") or {}
        self.assertIn("layer_summaries", ms.get("required") or [])
        self.assertIn("header_band_texts", ms.get("required") or [])


if __name__ == "__main__":
    unittest.main()
