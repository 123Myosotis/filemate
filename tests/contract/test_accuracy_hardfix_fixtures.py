# -*- coding: utf-8 -*-
"""Fixture execution for the accuracy hard-fix batch. Not a skip of the old structural test."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from pipeline.synth_contract.boot import ensure_contract_library, reset_boot_for_tests
from pipeline.synth_contract.executor import execute_mechanical
from pipeline.synth_contract.registry import load_contract
from tests.contract.plan_helpers import to_ops_plan

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "contract" / "fixtures" / "accuracy_hardfix"


class TestAccuracyHardfixFixtures(unittest.TestCase):
    def setUp(self):
        reset_boot_for_tests()
        ensure_contract_library()
        self.cv = load_contract("v1.1.0")

    def _load(self, name: str):
        data = json.loads((FIX / name).read_text(encoding="utf-8"))
        plan = to_ops_plan(dict(data["plan"]))
        plan["contract_version"] = self.cv.version
        plan["contract_hash"] = self.cv.file_hash
        return data["tables"], plan

    def test_t1_identity_join_gap(self):
        tables, plan = self._load("t1.json")
        out = execute_mechanical(tables, plan, [], self.cv)
        self.assertFalse(out.get("contract_rejected"), out.get("plan_notes"))
        by = {r["v"]: r.get("x", "") for r in out["rows"]}
        self.assertEqual(by.get("a"), "")
        self.assertEqual(by.get("b"), "Y")

    def test_t2_order_dedupe_agg_count(self):
        tables, plan = self._load("t2.json")
        out = execute_mechanical(tables, plan, [], self.cv)
        self.assertFalse(out.get("contract_rejected"), out.get("plan_notes"))
        by = {r["g"]: r["agg_result"] for r in out["rows"]}
        self.assertEqual(by.get("A"), 1)
        self.assertEqual(by.get("B"), 1)

    def test_t3_unclassified_empty_rejects(self):
        tables, plan = self._load("t3.json")
        out = execute_mechanical(tables, plan, [], self.cv)
        self.assertTrue(out.get("contract_rejected"))
        blob = json.dumps(out.get("contract_issues"), ensure_ascii=False)
        blob += " ".join(str(n) for n in (out.get("plan_notes") or []))
        self.assertIn("unclassified", blob.lower())
