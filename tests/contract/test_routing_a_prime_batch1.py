# -*- coding: utf-8 -*-
"""后续-2 第一批落地最小测：新枚举可加载；缺上限键 → 契约无效类。"""
from __future__ import annotations

import copy
import unittest

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.layer_recognize_limits import (
    ASK_MAX_ROUNDS_KEY,
    ENTRY_RETRY_MAX_KEY,
    PORTRAIT_WRITE_RETRY_MAX_KEY,
    read_all_batch1_limits,
    read_layer_recognize_ask_max_rounds,
    require_positive_int_limit,
)
from pipeline.synth_contract.registry import load_contract


class TestRoutingAPrimeBatch1(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cv = load_contract("v1.4.0")

    def test_batch1_enums_loadable(self):
        """新枚举可从 v1.4.0 cross_table.enums 加载且成员齐全。"""
        enums = (self.cv.data.get("cross_table") or {}).get("enums") or {}
        self.assertEqual(
            set(enums.get("layer_recognize_status") or []),
            {"no_structure", "ok", "review_failed", "conflict"},
        )
        self.assertEqual(
            set(enums.get("header_completeness") or []),
            {"complete", "incomplete", "not_header"},
        )
        self.assertEqual(
            set(enums.get("portrait_write_failure_code") or []),
            {"field_missing", "mapping_undefined", "write_rejected"},
        )
        reasons = set(enums.get("layer_recognize_conflict_reason") or [])
        for code in (
            "proposal_retries_exhausted",
            "ask_budget_exhausted_proposal",
            "ask_budget_exhausted_subset",
            "ask_budget_exhausted_review",
            "success_payload_incomplete",
        ):
            self.assertIn(code, reasons)
        # StageReviewResult.needs_user_ask 形状
        stage = (self.cv.data.get("cross_table") or {}).get("StageReviewResult") or {}
        self.assertIn("needs_user_ask", stage.get("required") or [])
        self.assertEqual(
            (stage.get("properties") or {}).get("needs_user_ask", {}).get("type"),
            "boolean",
        )
        # 写失败码 → 外层错误类（Q3）
        mapping = (self.cv.data.get("cross_table") or {}).get(
            "portrait_write_failure_error_class"
        ) or {}
        self.assertEqual(mapping.get("write_rejected"), "cross_table_input_invalid")
        self.assertEqual(mapping.get("field_missing"), "cross_table_contract_invalid")

    def test_missing_limit_key_raises_contract_invalid(self):
        """缺上限键 → 读取方抛 cross_table_contract_invalid；不降级默认值。"""
        cross = copy.deepcopy(self.cv.data.get("cross_table") or {})
        # 基线：齐全时可读
        limits = read_all_batch1_limits({"cross_table": cross})
        self.assertGreaterEqual(limits[ASK_MAX_ROUNDS_KEY], 1)
        self.assertGreaterEqual(limits[ENTRY_RETRY_MAX_KEY], 1)
        self.assertGreaterEqual(limits[PORTRAIT_WRITE_RETRY_MAX_KEY], 1)

        del cross[ASK_MAX_ROUNDS_KEY]
        with self.assertRaises(CrossTableError) as ctx:
            read_layer_recognize_ask_max_rounds({"cross_table": cross})
        self.assertEqual(ctx.exception.code, "cross_table_contract_invalid")
        self.assertIn(ASK_MAX_ROUNDS_KEY, ctx.exception.message)

        # 非法值（0）同样契约无效，不降级
        cross2 = copy.deepcopy(self.cv.data.get("cross_table") or {})
        cross2[ASK_MAX_ROUNDS_KEY] = 0
        with self.assertRaises(CrossTableError) as ctx2:
            require_positive_int_limit(cross2, ASK_MAX_ROUNDS_KEY)
        self.assertEqual(ctx2.exception.code, "cross_table_contract_invalid")

    def test_v140_file_still_loadable(self):
        """第一批文件保留可加载；CURRENT 指针由后续批次推进（见 batch2 测）。"""
        boot = load_contract("v1.4.0")
        self.assertEqual(boot.version, "v1.4.0")
        self.assertIn(
            "layer_recognize_ask_max_rounds",
            (boot.data.get("cross_table") or {}),
        )


if __name__ == "__main__":
    unittest.main()
