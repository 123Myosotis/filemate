# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from pipeline.synth_contract.triage import triage_after_execute, triage_plan
from pipeline.synth_contract.triage_confirm import ACTIONS_LATE, build_complex_confirmation


def _ready():
    return {"status": "ready", "primary_table": "t", "bindings": [], "ops": []}


class TestTriage(unittest.TestCase):
    def test_all_clear_simple(self):
        got = triage_plan(_ready())
        self.assertEqual(set(got), {"triage", "fired_signals"})
        self.assertEqual(got["triage"], "simple")
        self.assertEqual(got["fired_signals"], [])

    def test_ambiguous_reason(self):
        plan = _ready()
        plan["bindings"] = [{"ambiguous_reason": "  two headers  ", "alternatives": []}]
        got = triage_plan(plan)
        self.assertEqual(got["triage"], "complex")
        self.assertIn("binding_ambiguous_reason", got["fired_signals"])

    def test_alternatives(self):
        plan = _ready()
        plan["bindings"] = [{"ambiguous_reason": "", "alternatives": ["b"]}]
        self.assertIn("binding_alternatives", triage_plan(plan)["fired_signals"])

    def test_each_status(self):
        tokens = {
            "status_await_user": "await_user",
            "status_contract_incomplete": "contract_incomplete",
            "status_version_rejected": "version_rejected",
            "status_over_budget": "over_budget",
            "status_unanchored": "unanchored",
            "status_fail": "fail",
            "status_rollback": "rollback",
            "status_non_idempotent_rerun": "non_idempotent_rerun",
            "status_review_reread": "review_reread",
            "status_stop": "stop",
        }
        for signal, token in tokens.items():
            plan = _ready()
            plan["status"] = token
            fired = triage_plan(plan)["fired_signals"]
            self.assertIn(signal, fired, signal)

    def test_budget_over(self):
        plan = _ready()
        plan["budgets"] = [{"limits": {"call_count": 1}, "actual": {"call_count": 2}}]
        self.assertIn("budget_actual_over", triage_plan(plan)["fired_signals"])

    def test_row_cover(self):
        plan = _ready()
        plan["source_row_ids"] = ["1", "2"]
        plan["row_cover"] = [{"source_row_id": "1"}]
        self.assertIn("row_cover_incomplete", triage_plan(plan)["fired_signals"])

    def test_mismatch_note(self):
        plan = _ready()
        plan["runtime_notes"] = ["shared_map_mismatch map_id=a shared_map_id=b"]
        self.assertIn("shared_map_mismatch_note", triage_plan(plan)["fired_signals"])

    def test_after_execute_issues(self):
        plan = _ready()
        self.assertEqual(
            triage_after_execute(plan, {"notes": [], "contract_issues": []})["triage"],
            "simple",
        )
        got = triage_after_execute(plan, {"contract_issues": [{"code": "x"}]})
        self.assertEqual(got["triage"], "complex")
        self.assertIn("execution_contract_issues", got["fired_signals"])

    def test_blank_reason_not_signal(self):
        plan = _ready()
        plan["bindings"] = [{"ambiguous_reason": "   ", "alternatives": []}]
        self.assertEqual(triage_plan(plan)["triage"], "simple")

    def test_confirm_illegal_ai_aborts(self):
        plan = _ready()
        plan["bindings"] = [{"ambiguous_reason": "x", "alternatives": []}]
        pre = triage_plan(plan)
        bad = build_complex_confirmation(
            plan, pre, ai_complete=lambda _p: {"user_prompt": "?", "conflicts": [
                {"signal_id": "binding_ambiguous_reason", "suggested_action": "confirm_execute"},
            ]},
            action_set=ACTIONS_LATE,
        )
        self.assertEqual(bad["conflicts"][0]["suggested_action"], "abort")
        self.assertEqual(bad.get("confirmation_fallback"), "abort")

    def test_plan_returns_no_action_key(self):
        self.assertNotIn("suggested_action", triage_plan(_ready()))

    def test_plan_id12_ignores_gated_audit_keys(self):
        from pipeline.synth_contract.evidence_path import plan_id12
        plan = _ready()
        before = plan_id12(plan)
        plan["_gated_contract_version"] = "v1.1.0"
        plan["_gated_contract_hash"] = "abc"
        self.assertEqual(plan_id12(plan), before)
