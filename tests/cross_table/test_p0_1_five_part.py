# -*- coding: utf-8 -*-
"""P0-1: needs_user five-part gate + ask_history abort/confirm (no business hardcoding)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.block_cross_table.memory_rag.bootstrap import open_memory_db
from pipeline.block_cross_table.query_engine.errors import QueryEngineError
from pipeline.block_cross_table.query_engine.ie_query import (
    FIVE_PART_FIELD_KEYS,
    USER_REPLY_ABORT,
    USER_REPLY_CONFIRM,
    handle_ask_history_user_reply,
    validate_needs_user_plan,
)
from pipeline.block_cross_table.query_engine.qp_ai import (
    QP_SYSTEM,
    _normalize_plan,
    qp_call_ai,
)
from tests.cross_table.memory_rag_test_helpers import slots_for_tests


def _catalog() -> dict:
    return {"operators": {"op_filter": {"args_schema": {}}}}


def _tables() -> dict:
    return {"t1": {"columns": ["c1"], "rows": [{"c1": "v"}]}}


def _ai_needs_user(**extra) -> dict:
    base = {
        "status": "needs_user",
        "fail_locus": "other",
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


def _binding_count(conn) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='bindings'"
    )
    if not cur.fetchone()[0]:
        # alternate table name used by store
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        for cand in ("bindings", "confirmed_binding", "memory_binding"):
            if cand in names:
                return int(conn.execute("SELECT COUNT(*) FROM %s" % cand).fetchone()[0])
        return 0
    return int(conn.execute("SELECT COUNT(*) FROM bindings").fetchone()[0])


class TestP01FivePart(unittest.TestCase):
    def test_normalize_does_not_fill_five_part(self):
        """_normalize_plan: five-part stays empty (no default prose)."""
        raw = {
            "status": "needs_user",
            "fail_locus": "other",
            "steps": [],
            "edges": [],
            "condition_trees": {},
            "result_projection": {"columns": []},
        }
        out = _normalize_plan(raw, table_ids=["t1"])
        for k in FIVE_PART_FIELD_KEYS:
            self.assertNotIn(k, out)
            self.assertFalse(out.get(k))

    def test_empty_five_part_each_field_rejected(self):
        """Each of 5 five-part fields empty alone → reject; no local fill."""
        for field in FIVE_PART_FIELD_KEYS:
            with self.subTest(field=field):
                plan = _ai_needs_user(**{field: ""})
                before = {k: plan.get(k) for k in FIVE_PART_FIELD_KEYS}
                with self.assertRaises(QueryEngineError) as ctx:
                    validate_needs_user_plan(plan)
                self.assertEqual(
                    getattr(ctx.exception, "subreason", None), "five_part_empty"
                )
                # Plan object not locally rewritten with default prose.
                for k in FIVE_PART_FIELD_KEYS:
                    self.assertEqual(plan.get(k), before[k])

    def test_empty_five_part_field_rejected(self):
        """已覆盖：见 test_empty_five_part_each_field_rejected（speculation 空）。"""
        plan = _ai_needs_user(speculation="")
        with self.assertRaises(QueryEngineError) as ctx:
            validate_needs_user_plan(plan)
        self.assertEqual(getattr(ctx.exception, "subreason", None), "five_part_empty")

    def test_illegal_fail_locus_rejected_not_mapped(self):
        """非法 fail_locus 拒收，且不本地改写成枚举。"""
        plan = _ai_needs_user(
            fail_locus="无法确定用户指的是哪个对象，需要进一步澄清"
        )
        with self.assertRaises(QueryEngineError) as ctx:
            validate_needs_user_plan(plan)
        self.assertEqual(getattr(ctx.exception, "subreason", None), "fail_locus_invalid")
        self.assertNotIn(plan.get("fail_locus"), ("other", "term_expand"))

    def test_illegal_fail_locus_retry_still_illegal_fails(self):
        """首次非法→重试；重试仍非法→最终失败（有限次，不本地改写）。"""
        calls = {"n": 0}

        def fake_chat(prompt, api_key, system=None, **_k):
            calls["n"] += 1
            return _ai_needs_user(fail_locus="still_not_enum_%s" % calls["n"])

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
                    max_retries=1,
                )
        self.assertIn("failed after retries", str(ctx.exception))
        self.assertEqual(calls["n"], 2)  # attempt 0 + retry 1

    def test_qp_retries_then_accepts_valid_needs_user(self):
        """合法五段+合法 locus：validate 通过、route_e_entry、审计写入。"""
        calls = {"n": 0}

        def fake_chat(prompt, api_key, system=None, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                return _ai_needs_user(fail_locus="not_an_enum")
            return _ai_needs_user(fail_locus="term_expand")

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
        self.assertEqual(out["fail_locus"], "term_expand")
        self.assertEqual(out["ai_calls"], 2)
        for k in FIVE_PART_FIELD_KEYS:
            self.assertTrue(str(out["five_part"].get(k) or "").strip())
        self.assertEqual(out["e_route"]["e_entry"], "E_entry_ding")
        self.assertEqual((out.get("audit") or {}).get("event"), "qp_needs_user_routed")
        self.assertEqual((out.get("audit") or {}).get("fail_locus"), "term_expand")

    def test_confirm_then_qp_call_ai_only(self):
        """confirm 后仅经 ask_history→qp_call_ai 再计划。"""
        prior = _ai_needs_user()
        cont = handle_ask_history_user_reply(
            user_reply_kind=USER_REPLY_CONFIRM,
            user_reply="yes",
            prior_needs_user=prior,
            ask_history=[],
        )
        self.assertTrue(cont["qp_call_allowed"])
        self.assertTrue(cont["replan"])
        self.assertFalse(cont["persist_binding"])

        def fake_chat(prompt, api_key, system=None, **_k):
            import json

            body = json.loads(prompt)
            self.assertTrue(body.get("ask_history"))
            return _ai_needs_user(fail_locus="term_expand", speculation="after_user")

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
                ask_history=cont["ask_history"],
                recalled_memories=[],
                require_full_capability_cover=False,
                apply_column_repair=False,
            )
        self.assertEqual(out["status"], "needs_user")
        self.assertEqual(out["fail_locus"], "term_expand")
        self.assertEqual(out["five_part"]["speculation"], "after_user")
        self.assertEqual(out["ai_calls"], 1)

    def test_abort_no_second_qp_no_binding(self):
        """abort：binding 表计数不变 + qp 调用不增 + 记审计。"""
        dim = int(slots_for_tests()["embedding_dim"])
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "abort.sqlite"
            conn = open_memory_db(db, embedding_dim=dim, enable_vec=False)
            before = _binding_count(conn)
            qp_calls = {"n": 0}

            prior = _ai_needs_user()
            aborted = handle_ask_history_user_reply(
                user_reply_kind=USER_REPLY_ABORT,
                user_reply="stop",
                prior_needs_user=prior,
                ask_history=[],
            )
            self.assertEqual(aborted["status"], "aborted")
            self.assertFalse(aborted["qp_call_allowed"])
            self.assertFalse(aborted["replan"])
            self.assertFalse(aborted["persist_binding"])
            self.assertEqual(
                aborted["ask_history"][-1]["user_reply_kind"], USER_REPLY_ABORT
            )
            self.assertEqual((aborted.get("audit") or {}).get("event"), "ask_abort")
            self.assertFalse((aborted.get("audit") or {}).get("persist_binding"))
            self.assertIn("e_route", aborted)

            if aborted["qp_call_allowed"]:
                qp_calls["n"] += 1
            self.assertEqual(qp_calls["n"], 0)
            after = _binding_count(conn)
            self.assertEqual(after, before)
            conn.close()

    def test_qp_system_mentions_five_part_and_enum_not_business_cols(self):
        """QP_SYSTEM 含五段/枚举要求，无业务列名硬编码。"""
        self.assertIn("speculation", QP_SYSTEM)
        self.assertIn("please_confirm_or_correct", QP_SYSTEM)
        self.assertIn("term_expand", QP_SYSTEM)
        for w in ("设备子类型", "设备名称", "二次安防"):
            self.assertNotIn(w, QP_SYSTEM)


if __name__ == "__main__":
    unittest.main()
