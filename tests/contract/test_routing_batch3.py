# -*- coding: utf-8 -*-
"""后续-2 第三批落地硬测：枚举 / plan_regenerate_max / StagePlanSchema 计划校验拒④。"""
from __future__ import annotations

import copy
import unittest

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.stage_plan import (
    ROLLBACK_OWNERSHIP,
    ROLLBACK_STAGE,
    build_portrait_index,
    read_plan_regenerate_max,
    read_stage_rollback_point_enum,
    read_structure_status_enum,
    validate_stage_plan_against_portrait,
)
from pipeline.synth_contract.registry import load_contract, read_current_pointer


_STRUCTURE_STATUS = {"upstream_absent", "layers_recognized"}
_STAGE_ROLLBACK = {
    "a_prime_recognition",
    "stage_start",
    "upstream_layout",
}


class TestRoutingBatch3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cv = load_contract("v1.6.0")
        cls.ct = cls.cv.data.get("cross_table") or {}

    def test_v160_file_still_loadable(self):
        """第三批文件保留可加载；CURRENT 指针由后续批次推进（见 batch4 测）。"""
        self.assertEqual(self.cv.version, "v1.6.0")
        self.assertIsInstance(self.ct, dict)

    def test_batch3_enums_loadable_exact_members(self):
        """硬测1：structure_status 恰两值；stage_rollback_point 恰三值且无④。"""
        enums = self.ct.get("enums") or {}
        status = list(enums.get("structure_status") or [])
        self.assertEqual(set(status), _STRUCTURE_STATUS)
        self.assertEqual(len(status), 2)

        rb = list(enums.get("stage_rollback_point") or [])
        self.assertEqual(set(rb), _STAGE_ROLLBACK)
        self.assertEqual(len(rb), 3)
        self.assertNotIn("cross_block_ownership", rb)
        self.assertNotIn(ROLLBACK_OWNERSHIP, set(rb))

        # 读取方与契约一致
        self.assertEqual(read_structure_status_enum(self.cv.data), frozenset(status))
        self.assertEqual(read_stage_rollback_point_enum(self.cv.data), frozenset(rb))

    def test_plan_regenerate_max_missing_zero_nonint_contract_invalid(self):
        """硬测2：缺键 / 0 / 非整数 → 各抛 cross_table_contract_invalid。"""
        cross = copy.deepcopy(self.ct)
        # 基线可读
        self.assertGreaterEqual(
            read_plan_regenerate_max({"cross_table": cross}), 1
        )

        del cross["plan_regenerate_max"]
        with self.assertRaises(CrossTableError) as ctx_missing:
            read_plan_regenerate_max({"cross_table": cross})
        self.assertEqual(ctx_missing.exception.code, "cross_table_contract_invalid")
        self.assertIn("plan_regenerate_max", ctx_missing.exception.message)

        cross0 = copy.deepcopy(self.ct)
        cross0["plan_regenerate_max"] = 0
        with self.assertRaises(CrossTableError) as ctx_zero:
            read_plan_regenerate_max({"cross_table": cross0})
        self.assertEqual(ctx_zero.exception.code, "cross_table_contract_invalid")

        cross_f = copy.deepcopy(self.ct)
        cross_f["plan_regenerate_max"] = 2.5
        with self.assertRaises(CrossTableError) as ctx_nonint:
            read_plan_regenerate_max({"cross_table": cross_f})
        self.assertEqual(ctx_nonint.exception.code, "cross_table_contract_invalid")

    def test_stage_plan_instance_rejects_rollback_point_four(self):
        """硬测3：计划实例回滚点填④ → 计划校验拒绝（非 Schema 加载拒绝）。"""
        # Schema 仍可加载
        schema = self.ct.get("StagePlanSchema") or {}
        self.assertIn("stages", schema.get("required") or [])
        self.assertIn("allow_user_ask", (schema.get("stage_item") or {}).get("required") or [])
        desc = str(schema.get("description") or "")
        self.assertIn("allow_user_ask", desc)
        self.assertIn("needs_user_ask", desc)
        self.assertIn("AND", desc)

        portraits = {
            "0": {
                "structure_status": "layers_recognized",
                "block_id": 0,
                "layer_order": ["L0"],
                "subtable_boundaries": [{"subtable_id": 0}],
                "dimension_membership": [
                    {"dimension_field_id": "d0", "layer_ids": ["L0"]}
                ],
            }
        }
        index = build_portrait_index(portraits, contract_data=self.cv.data)
        stage = {
            "stage_id": "s0",
            "parent_table_ids": ["t0"],
            "block_ids": [0],
            "row_range": {"whole": True},
            "col_range": {"whole": True},
            "subtable_ids": [0],
            "dimension_ids": ["d0"],
            "layer_ids": ["L0"],
            "allowed_decision_kinds": ["enter_s"],
            "allow_user_ask": False,
            "rollback_point": ROLLBACK_OWNERSHIP,  # ④
            "dependency_allows_prior": False,
        }
        reasons = validate_stage_plan_against_portrait(
            {"stages": [stage]},
            index,
            allowed_decision_kinds=list(
                (self.ct.get("enums") or {}).get("orchestrate_decision_kind") or []
            ),
            stage_rollback_points=list(
                (self.ct.get("enums") or {}).get("stage_rollback_point") or []
            ),
        )
        self.assertTrue(
            any("ownership_forbidden" in r for r in reasons),
            reasons,
        )
        # 合法三值之一应通过回滚点检查（其它字段齐全时）
        stage_ok = dict(stage)
        stage_ok["rollback_point"] = ROLLBACK_STAGE
        reasons_ok = validate_stage_plan_against_portrait(
            {"stages": [stage_ok]},
            index,
            allowed_decision_kinds=list(
                (self.ct.get("enums") or {}).get("orchestrate_decision_kind") or []
            ),
            stage_rollback_points=list(
                (self.ct.get("enums") or {}).get("stage_rollback_point") or []
            ),
        )
        self.assertFalse(any("ownership_forbidden" in r for r in reasons_ok))
        self.assertFalse(any("rollback_point_invalid" in r for r in reasons_ok))


if __name__ == "__main__":
    unittest.main()
