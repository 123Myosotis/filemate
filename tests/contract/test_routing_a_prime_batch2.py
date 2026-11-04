# -*- coding: utf-8 -*-
"""后续-2 第二批落地最小测：trace 种类引用列表、审计码、schema、CURRENT。"""
from __future__ import annotations

import unittest

from pipeline.synth_contract.registry import load_contract


_A_PRIME_KINDS = {
    "layer_propose",
    "layer_ask_subset",
    "layer_formal_review",
}


class TestRoutingAPrimeBatch2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cv = load_contract("v1.5.0")
        cls.ct = cls.cv.data.get("cross_table") or {}

    def test_v150_file_still_loadable(self):
        """第二批文件保留可加载；CURRENT 指针由后续批次推进（见 batch3 测）。"""
        boot = load_contract("v1.5.0")
        self.assertEqual(boot.version, "v1.5.0")
        self.assertIn(
            "LayerRecognizeTraceMeta",
            (boot.data.get("cross_table") or {}),
        )

    def test_routing_trace_kind_and_layer_recognize_kinds_subset(self):
        """方案甲：layer_recognize_kinds 为 routing_trace_kind 的引用子集，非第二枚举。"""
        enums = self.ct.get("enums") or {}
        routing = set(enums.get("routing_trace_kind") or [])
        self.assertTrue(_A_PRIME_KINDS.issubset(routing))
        for extra in (
            "stage_plan_generate",
            "stage_plan_review",
            "stage_exec_model",
            "conflict_four_part_ask",
            "conflict_user_reply",
            "orchestrate_decision",
        ):
            self.assertIn(extra, routing)

        # 不得存在第二枚举键
        self.assertNotIn("layer_recognize_trace_kind", enums)

        kinds = self.ct.get("layer_recognize_kinds")
        self.assertIsInstance(kinds, list)
        self.assertEqual(set(kinds), _A_PRIME_KINDS)
        self.assertTrue(set(kinds).issubset(routing))

    def test_layer_recognize_kinds_subset_of_routing_trace_kind(self):
        """专用一致性：set(layer_recognize_kinds) ⊂ set(enums.routing_trace_kind)；无未知成员。"""
        enums = self.ct.get("enums") or {}
        routing = set(enums.get("routing_trace_kind") or [])
        kinds = list(self.ct.get("layer_recognize_kinds") or [])
        self.assertTrue(kinds, "layer_recognize_kinds must be non-empty")
        kind_set = set(kinds)
        self.assertTrue(
            kind_set.issubset(routing),
            "layer_recognize_kinds must be subset of enums.routing_trace_kind",
        )
        unknown = kind_set - routing
        self.assertEqual(unknown, set(), "unknown members in layer_recognize_kinds: %r" % unknown)
        for member in kinds:
            self.assertIn(member, routing)

    def test_audit_structure_invalid_is_standalone_error_code(self):
        codes = self.ct.get("error_codes") or []
        self.assertIn("cross_table_audit_structure_invalid", codes)
        self.assertIn("cross_table_contract_invalid", codes)
        self.assertIn("trace_io_failed", codes)
        bounds = self.ct.get("error_code_boundaries") or {}
        for key in (
            "cross_table_contract_invalid",
            "cross_table_audit_structure_invalid",
            "trace_io_failed",
        ):
            self.assertIn(key, bounds)
            self.assertIsInstance(bounds[key], str)
            self.assertGreater(len(bounds[key]), 10)
        # 分界文案不得把 audit 码说成包装进契约无效
        audit_note = bounds["cross_table_audit_structure_invalid"]
        self.assertIn("cross_table_audit_structure_invalid", audit_note)
        self.assertIn("standalone", audit_note.lower() + audit_note)

    def test_layer_recognize_trace_meta_and_block_id_collision_rule(self):
        meta = self.ct.get("LayerRecognizeTraceMeta") or {}
        for key in (
            "run_id",
            "block_id",
            "entry_seq",
            "call_kind",
            "kind_seq",
            "counts_toward_ask",
        ):
            self.assertIn(key, meta.get("required") or [])
        rules = meta.get("block_id_normalize_rules") or {}
        self.assertIn("collision", rules)
        self.assertIn("cross_table_audit_structure_invalid", rules["collision"])

    def test_decision_params_rules_present(self):
        meta = self.ct.get("OrchestrateDecisionTraceMeta") or {}
        rules = meta.get("decision_params_rules") or {}
        invalid = rules.get("invalid_cases") or []
        joined = " | ".join(invalid)
        self.assertIn("rationale_text", joined)
        self.assertIn("mechanical", joined)
        self.assertIn("cross_table_audit_structure_invalid", joined)
        self.assertEqual(len(invalid), 3)

    def test_stage_exec_subsequent_12_hook(self):
        meta = self.ct.get("StageExecTraceMeta") or {}
        hook = meta.get("subsequent_12_hook") or ""
        self.assertIn("input_block_ids", hook)
        self.assertIn("后续-12", hook)
        prop = (meta.get("properties") or {}).get("input_block_ids") or {}
        self.assertIn("input_block_ids", str(prop.get("note") or "") + hook)

    def test_other_trace_metas_present(self):
        for key in (
            "ConflictSessionTraceMeta",
            "StagePlanTraceMeta",
            "OrchestrateDecisionTraceMeta",
        ):
            self.assertIsInstance(self.ct.get(key), dict)
            self.assertIn("run_id", (self.ct[key].get("required") or []))


if __name__ == "__main__":
    unittest.main()
