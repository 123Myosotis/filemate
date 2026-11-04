# -*- coding: utf-8 -*-
"""A/B projection fix: normalize shape-only, plan_ready gate, QP_SYSTEM, ok_ trace."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai.client import _write_chat_json_ok_trace, chat_json
from pipeline.block_cross_table.query_engine.errors import QueryEngineError
from pipeline.block_cross_table.query_engine.ie_query import FIVE_PART_FIELD_KEYS
from pipeline.block_cross_table.query_engine.qp_ai import (
    QP_SYSTEM,
    _normalize_plan,
    check_plan_ready_projection_columns,
    qp_call_ai,
)
from pipeline.block_cross_table.query_engine.result_projection import (
    ResultProjectionError,
)


def _tables():
    return {"t1": {"columns": ["col_a", "col_b"], "rows": [{"col_a": "1"}]}}


def _catalog():
    return {
        "operators": {
            "op_filter": {
                "args_schema": {},
                "complexity_bound": {"class": "C1"},
            }
        }
    }


def _plan_ready_proj(column_id: str, **extra):
    plan = {
        "status": "plan_ready",
        "query_time_basis": "data_max_time",
        "rollback_anchors": ["query_planner_start"],
        "demand_points": ["d"],
        "condition_trees": {},
        "result_projection": {
            "columns": [
                {
                    "column_id": column_id,
                    "display_name": column_id,
                    "column_name_source": "from_ai_runtime",
                    "source_step_id": "s0",
                    "derive_rule_ref": None,
                    "provenance": {"utterance_span": "u"},
                }
            ]
        },
        "steps": [
            {
                "step_id": "s0",
                "op": "op_filter",
                "args": {},
                "inputs": ["src:t1"],
                "output": "filtered",
                "provenance": {"utterance_span": "u"},
            }
        ],
        "edges": [],
    }
    plan.update(extra)
    return plan


def _needs_user_projection(**extra):
    base = {
        "status": "needs_user",
        "fail_locus": "projection",
        "speculation": "s",
        "evidence": "e",
        "what_might_be_wrong": "w",
        "next_step": "n",
        "please_confirm_or_correct": "p",
        "rollback_anchors": ["query_planner_start"],
        "demand_points": ["d"],
        "condition_trees": {},
        "result_projection": {"columns": []},
        "steps": [],
        "edges": [],
        "query_time_basis": "utterance",
    }
    base.update(extra)
    return base


class TestNormalizeProjectionShape(unittest.TestCase):
    def test_empty_projection_stays_empty_no_out_col(self):
        raw = {
            "status": "plan_ready",
            "steps": [{"step_id": "s0", "op": "op_filter", "args": {}, "inputs": []}],
            "result_projection": {"columns": []},
            "condition_trees": {},
            "edges": [],
            "demand_points": [],
        }
        out = _normalize_plan(raw, table_ids=["t1"])
        cols = out["result_projection"]["columns"]
        self.assertEqual(cols, [])
        blob = str(out)
        self.assertNotIn("out_col", blob)

    def test_missing_column_id_stays_missing(self):
        raw = {
            "status": "plan_ready",
            "steps": [{"step_id": "s0", "op": "op_filter", "args": {}, "inputs": []}],
            "result_projection": {
                "columns": [{"display_name": "x", "provenance": {"utterance_span": "u"}}]
            },
            "condition_trees": {},
            "edges": [],
            "demand_points": [],
        }
        out = _normalize_plan(raw, table_ids=["t1"])
        col = out["result_projection"]["columns"][0]
        self.assertFalse(str(col.get("column_id") or "").strip())
        self.assertNotIn("out_col", str(col))

    def test_string_real_name_preserved(self):
        raw = {
            "status": "plan_ready",
            "steps": [{"step_id": "s0", "op": "op_filter", "args": {}, "inputs": []}],
            "result_projection": {"columns": ["col_a"]},
            "condition_trees": {},
            "edges": [],
            "demand_points": [],
        }
        out = _normalize_plan(raw, table_ids=["t1"])
        self.assertEqual(out["result_projection"]["columns"][0]["column_id"], "col_a")

    def test_bare_string_array_lifts_to_columns(self):
        raw = {
            "status": "plan_ready",
            "steps": [{"step_id": "s0", "op": "op_filter", "args": {}, "inputs": []}],
            "result_projection": ["col_a", "col_b"],
            "condition_trees": {},
            "edges": [],
            "demand_points": [],
        }
        out = _normalize_plan(raw, table_ids=["t1"])
        ids = [c["column_id"] for c in out["result_projection"]["columns"]]
        self.assertEqual(ids, ["col_a", "col_b"])
        self.assertNotIn("out_col", str(out["result_projection"]))

    def test_old_fail_bare_array_no_longer_invents_out_col(self):
        """旧 fail 原始形状=裸字符串数组 → 新 normalize 升 {columns} 保留原名，无 out_col*。"""
        import json

        # Shape from fail_20261021T141940.txt (names are AI output under test, not Prompt).
        bare = ["col_alpha", "col_beta", "col_gamma"]
        raw = {
            "status": "plan_ready",
            "result_projection": bare,
            "steps": [
                {
                    "step_id": "filter_secondary_security",
                    "op": "op_filter",
                    "args": {},
                    "inputs": [],
                }
            ],
            "condition_trees": {},
            "edges": [],
            "demand_points": [],
        }
        out = _normalize_plan(raw, table_ids=["event_record"])
        ids = [c["column_id"] for c in out["result_projection"]["columns"]]
        self.assertEqual(ids, bare)
        blob = json.dumps(out["result_projection"], ensure_ascii=False)
        self.assertNotIn("out_col", blob)
        self.assertFalse(any(str(i).startswith("out_col") for i in ids))


class TestProjectionGate(unittest.TestCase):
    def test_reject_empty_and_placeholder(self):
        empty = _plan_ready_proj("col_a")
        empty["result_projection"] = {"columns": []}
        with self.assertRaises(ResultProjectionError):
            check_plan_ready_projection_columns(empty, _tables())

        bad = _plan_ready_proj("out_col")
        with self.assertRaises(ResultProjectionError) as ctx:
            check_plan_ready_projection_columns(bad, _tables())
        self.assertEqual(ctx.exception.fail_locus, "projection")

    def test_accept_table_column(self):
        plan = _plan_ready_proj("col_a")
        check_plan_ready_projection_columns(plan, _tables())

    def test_qp_reject_retry_then_needs_user(self):
        calls = {"n": 0}

        def fake_chat(prompt, api_key, system=None, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                return _plan_ready_proj("out_col")
            return _needs_user_projection()

        with patch(
            "pipeline.block_cross_table.query_engine.qp_ai.chat_json",
            side_effect=fake_chat,
        ):
            out = qp_call_ai(
                utterance="u",
                portraits=[],
                catalog=_catalog(),
                tables_by_id=_tables(),
                api_key="k",
                recalled_memories=[],
                require_full_capability_cover=False,
                apply_column_repair=False,
                max_retries=2,
            )
        self.assertEqual(out["status"], "needs_user")
        self.assertEqual(out["fail_locus"], "projection")
        for k in FIVE_PART_FIELD_KEYS:
            self.assertTrue(str(out["five_part"].get(k) or "").strip())
        self.assertEqual(calls["n"], 2)

    def test_qp_reject_exhausted_raises_projection(self):
        def fake_chat(prompt, api_key, system=None, **_k):
            return _plan_ready_proj("out_col")

        with patch(
            "pipeline.block_cross_table.query_engine.qp_ai.chat_json",
            side_effect=fake_chat,
        ):
            with self.assertRaises(QueryEngineError) as ctx:
                qp_call_ai(
                    utterance="u",
                    portraits=[],
                    catalog=_catalog(),
                    tables_by_id=_tables(),
                    api_key="k",
                    recalled_memories=[],
                    require_full_capability_cover=False,
                    apply_column_repair=False,
                    max_retries=2,
                )
        self.assertIn("failed after retries", str(ctx.exception))
        self.assertEqual(getattr(ctx.exception, "fail_locus", None), "projection")

    def test_qp_accepts_real_column_after_check(self):
        def fake_chat(prompt, api_key, system=None, **_k):
            return _plan_ready_proj("col_a")

        with patch(
            "pipeline.block_cross_table.query_engine.qp_ai.chat_json",
            side_effect=fake_chat,
        ), patch(
            "pipeline.block_cross_table.query_engine.qp_ai.validate_query_plan_ast",
            return_value={"status": "plan_ready"},
        ):
            out = qp_call_ai(
                utterance="u",
                portraits=[],
                catalog=_catalog(),
                tables_by_id=_tables(),
                api_key="k",
                recalled_memories=[],
                require_full_capability_cover=False,
                apply_column_repair=False,
                max_retries=2,
            )
        self.assertEqual(out["status"], "plan_ready")
        self.assertEqual(
            out["plan"]["result_projection"]["columns"][0]["column_id"], "col_a"
        )


class TestQpSystemAndOkTrace(unittest.TestCase):
    def test_qp_system_projection_rules_no_business_cols(self):
        self.assertIn("column_id", QP_SYSTEM)
        self.assertIn("fail_locus=projection", QP_SYSTEM)
        self.assertIn("out_col", QP_SYSTEM)  # abstract ban example only
        self.assertIn("result_col", QP_SYSTEM)
        for w in ("设备子类型", "设备名称", "二次安防", "站控层"):
            self.assertNotIn(w, QP_SYSTEM)

    def test_ok_trace_format_under_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_id = "test_ok_trace_%s" % Path(tmp).name
            path = _write_chat_json_ok_trace(
                run_id=run_id,
                system="sys-test",
                prompt='{"utterance":"hello world query"}',
                parsed={
                    "status": "plan_ready",
                    "result_projection": {"columns": [{"column_id": "col_a"}]},
                    "steps": [],
                },
                raw='{"status":"plan_ready"}',
            )
            self.assertIsNotNone(path)
            p = Path(path)
            self.assertTrue(p.exists())
            self.assertIn(run_id, str(p))
            self.assertTrue(p.name.startswith("ok_"))
            text = p.read_text(encoding="utf-8")
            self.assertLessEqual(len(text.encode("utf-8")), 4096)
            self.assertIn("system_sha256_16=", text)
            self.assertIn("user_prompt_len=", text)
            self.assertIn("utterance_prefix=", text)
            self.assertIn("column_ids=col_a", text)
            self.assertNotIn("sys-test", text)  # no full system body
            try:
                p.unlink()
                p.parent.rmdir()
            except OSError:
                pass

    def test_ok_trace_truncates_at_4kb_no_full_body(self):
        """超大 keys/raw 时文件仍 ≤4KB，且不含 system/prompt 全文。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_id = "test_ok_4k_%s" % Path(tmp).name
            huge_sys = "SYSTEM_FULL_BODY_" + ("X" * 8000)
            huge_prompt = '{"utterance":"' + ("U" * 2000) + '"}'
            huge_keys = {("k_%s" % i): i for i in range(200)}
            huge_keys["status"] = "plan_ready"
            huge_keys["result_projection"] = {
                "columns": [{"column_id": "c_%s" % i} for i in range(80)]
            }
            path = _write_chat_json_ok_trace(
                run_id=run_id,
                system=huge_sys,
                prompt=huge_prompt,
                parsed=huge_keys,
                raw="R" * 20000,
            )
            self.assertIsNotNone(path)
            p = Path(path)
            data = p.read_bytes()
            self.assertLessEqual(len(data), 4096)
            text = data.decode("utf-8", errors="replace")
            self.assertNotIn("SYSTEM_FULL_BODY_", text)
            self.assertNotIn("U" * 100, text)
            self.assertNotIn("R" * 100, text)
            self.assertIn("system_sha256_16=", text)
            self.assertIn("raw_len=20000", text)
            try:
                p.unlink()
                p.parent.rmdir()
            except OSError:
                pass

    def test_fail_trace_still_written_on_parse_error(self):
        """失败路径仍写 fail_*（与 ok_ 并存规则不变）。"""
        from ai import client as ai_client

        run_id = "test_fail_trace_keep"
        with patch.object(
            ai_client,
            "chat_completion",
            side_effect=ValueError("boom_json"),
        ):
            out = chat_json(
                '{"utterance":"u"}',
                "k",
                system="s",
                trace_run_id=run_id,
            )
        self.assertIsNone(out)
        base = (
            Path(__file__).resolve().parents[2]
            / "diagnostics"
            / "trace"
            / "chat_json"
            / run_id
        )
        fails = list(base.glob("fail_*.txt"))
        self.assertTrue(fails, "expected fail_*.txt under run_id")
        for f in fails:
            try:
                f.unlink()
            except OSError:
                pass
        try:
            base.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()