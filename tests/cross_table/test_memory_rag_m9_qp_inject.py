# -*- coding: utf-8 -*-
"""
M9: hybrid_recall → QP recalled_memories inject.
No live third-party; chat_json mocked. No local synonym expand.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from pipeline.block_cross_table.memory_rag.bootstrap import open_memory_db
from pipeline.block_cross_table.memory_rag.jargon_store import upsert_filemate_jargon
from pipeline.block_cross_table.memory_rag.qp_memory_inject import (
    normalize_recalled_memories,
    recall_memories_for_qp,
)
from pipeline.block_cross_table.memory_rag.qp_slot import (
    assemble_qp_input_with_memory_candidates,
)
from pipeline.block_cross_table.memory_rag.store import insert_binding_confirmed
from pipeline.block_cross_table.query_engine.qp_ai import QP_SYSTEM, qp_call_ai
from tests.cross_table.memory_rag_test_helpers import slots_for_tests
from tests.cross_table.qe_test_helpers import catalog_from_current


# Acceptance-case vocabulary lives only in tests (not production hardcode).
_TERM_SEC = "二次安防"
_DESC_SEC = "A平面交换机、B平面交换机、态势感知"
_VALS = ("A平面交换机", "B平面交换机", "态势感知")
_COL_DEVICE = "设备子类型"
_TABLE_ID = "t_task"


def _tables():
    return {
        _TABLE_ID: {
            "columns": ["站点", "设备名称", _COL_DEVICE, "任务时间"],
            "rows": [
                {
                    "站点": "s1",
                    "设备名称": "d1",
                    _COL_DEVICE: _VALS[0],
                    "任务时间": "2026-01-01",
                }
            ],
        }
    }


def _memory_item():
    return {
        "term": _TERM_SEC,
        "description": _DESC_SEC,
        "values": list(_VALS),
        "binding_id": "bind-sec",
        "source": "test",
        "memory_ref": "bind-sec",
    }


def _or_plan(*, memory_ref: str = "bind-sec") -> dict:
    """Plan shape asserting OR expansion mapped onto real column."""
    children = [
        {
            "node_kind": "ATOM",
            "atom": {"column_id": _COL_DEVICE, "cmp": "eq", "value": v},
        }
        for v in _VALS
    ]
    return {
        "status": "plan_ready",
        "query_time_basis": "data_max_time",
        "rollback_anchors": ["query_planner_start"],
        "demand_points": [
            {
                "demand_id": "d_or",
                "kind": "filter",
                "utterance_span": "span_sec",
                "bound_step_id": "s_filter",
                "bound_column_or_filter": _COL_DEVICE,
            }
        ],
        "condition_trees": {
            "ct_sec": {
                "node_kind": "OR",
                "children": children,
                "provenance": {"memory_ref": memory_ref, "utterance_span": "span_sec"},
            }
        },
        "result_projection": {
            "columns": [
                {
                    "column_id": _COL_DEVICE,
                    "display_name": _COL_DEVICE,
                    "column_name_source": "from_ai_runtime",
                    "source_step_id": "s_filter",
                    "derive_rule_ref": None,
                    "provenance": {
                        "utterance_span": "span_sec",
                        "memory_ref": memory_ref,
                    },
                }
            ]
        },
        "steps": [
            {
                "step_id": "s_filter",
                "op": "op_filter",
                "args": {
                    "column_id": _COL_DEVICE,
                    "cmp": "in",
                    "value": list(_VALS),
                },
                "inputs": ["src:%s" % _TABLE_ID],
                "output": "h_filt",
                "provenance": {
                    "utterance_span": "span_sec",
                    "memory_ref": memory_ref,
                },
            }
        ],
        "edges": [],
    }


def _unrelated_plan() -> dict:
    return {
        "status": "plan_ready",
        "query_time_basis": "data_max_time",
        "rollback_anchors": ["query_planner_start"],
        "demand_points": [
            {
                "demand_id": "d_sw",
                "kind": "filter",
                "utterance_span": "span_sw",
                "bound_step_id": "s_filter",
                "bound_column_or_filter": _COL_DEVICE,
            }
        ],
        "condition_trees": {},
        "result_projection": {
            "columns": [
                {
                    "column_id": _COL_DEVICE,
                    "display_name": _COL_DEVICE,
                    "column_name_source": "from_ai_runtime",
                    "source_step_id": "s_filter",
                    "derive_rule_ref": None,
                    "provenance": {"utterance_span": "span_sw"},
                }
            ]
        },
        "steps": [
            {
                "step_id": "s_filter",
                "op": "op_filter",
                "args": {
                    "column_id": _COL_DEVICE,
                    "cmp": "eq",
                    "value": "站控层交换机",
                },
                "inputs": ["src:%s" % _TABLE_ID],
                "output": "h_filt",
                "provenance": {"utterance_span": "span_sw"},
            }
        ],
        "edges": [],
    }


class TestMemoryRagM9QpInject(unittest.TestCase):
    def setUp(self):
        self.dim = int(slots_for_tests()["embedding_dim"])
        self.conn = open_memory_db(embedding_dim=self.dim, enable_vec=True)
        self.catalog = catalog_from_current()
        insert_binding_confirmed(
            self.conn,
            user_confirmed=True,
            utterance_span_fingerprint="fp-sec",
            binding_payload={
                "term": _TERM_SEC,
                "description": _DESC_SEC,
                "values": list(_VALS),
                "scope": "",
            },
            source_trace_id="t-sec",
            confirmed_at="2026-10-21T00:00:00+00:00",
            scope={},
        )
        upsert_filemate_jargon(
            self.conn,
            content=_TERM_SEC,
            meaning=_DESC_SEC,
            count=5,
        )

    def tearDown(self):
        self.conn.close()

    def test_prompt_rules_mention_recalled_memories(self):
        self.assertIn("recalled_memories", QP_SYSTEM)
        self.assertIn("OR", QP_SYSTEM)
        self.assertIn("needs_user", QP_SYSTEM)

    def test_hybrid_recall_packages_recalled_memories(self):
        pack = recall_memories_for_qp(self.conn, utterance=_TERM_SEC)
        mems = pack["recalled_memories"]
        self.assertTrue(mems)
        hit = next((m for m in mems if m.get("term") == _TERM_SEC), None)
        self.assertIsNotNone(hit)
        self.assertIn("A平面交换机", hit["description"])

    def test_assemble_envelope_has_recalled_memories(self):
        env = assemble_qp_input_with_memory_candidates(
            utterance=_TERM_SEC,
            portraits=[],
            catalog=self.catalog,
            recalled_memories=[_memory_item()],
        )
        self.assertEqual(env["recalled_memories"][0]["term"], _TERM_SEC)

    def test_case_a_include_split_or_mapped_to_column(self):
        """Case A: query term → OR over description parts on real column + memory_ref."""
        captured = {}

        def fake_chat(prompt, api_key, system=None, **_k):
            captured["prompt"] = prompt
            captured["system"] = system
            body = json.loads(prompt)
            self.assertIn("recalled_memories", body)
            terms = [m.get("term") for m in body["recalled_memories"]]
            self.assertIn(_TERM_SEC, terms)
            # Model (mock) expands from description — not local code.
            return _or_plan(memory_ref="bind-sec")

        with patch(
            "pipeline.block_cross_table.query_engine.qp_ai.chat_json",
            side_effect=fake_chat,
        ):
            out = qp_call_ai(
                utterance=_TERM_SEC,
                portraits=[],
                catalog=self.catalog,
                tables_by_id=_tables(),
                api_key="test-key",
                recalled_memories=[_memory_item()],
                require_full_capability_cover=False,
            )
        self.assertEqual(out["status"], "plan_ready")
        tree = out["plan"]["condition_trees"]["ct_sec"]
        self.assertEqual(tree["node_kind"], "OR")
        vals = [c["atom"]["value"] for c in tree["children"]]
        for v in _VALS:
            self.assertIn(v, vals)
        self.assertEqual(tree["children"][0]["atom"]["column_id"], _COL_DEVICE)
        self.assertEqual(tree["provenance"]["memory_ref"], "bind-sec")
        step_prov = out["plan"]["steps"][0]["provenance"]
        self.assertEqual(step_prov.get("memory_ref"), "bind-sec")
        self.assertIn("recalled_memories", captured["system"])

    def test_case_b_unrelated_query_not_forced_as_sec(self):
        """Case B: unrelated utterance must not be locally rewritten as 二次安防."""
        captured = {}

        def fake_chat(prompt, api_key, system=None, **_k):
            body = json.loads(prompt)
            captured["body"] = body
            # Memories may be present from FTS noise; model must not treat as match.
            return _unrelated_plan()

        with patch(
            "pipeline.block_cross_table.query_engine.qp_ai.chat_json",
            side_effect=fake_chat,
        ):
            out = qp_call_ai(
                utterance="站控层交换机",
                portraits=[],
                catalog=self.catalog,
                tables_by_id=_tables(),
                api_key="test-key",
                recalled_memories=[_memory_item()],
                memory_conn=None,
                require_full_capability_cover=False,
            )
        plan_s = json.dumps(out["plan"], ensure_ascii=False)
        for v in _VALS:
            self.assertNotIn(v, plan_s)
        self.assertEqual(
            out["plan"]["steps"][0]["args"]["value"], "站控层交换机"
        )
        # Local path did not strip/replace utterance.
        self.assertEqual(captured["body"]["utterance"], "站控层交换机")

    def test_case_c_needs_user_when_uncertain(self):
        """Case C: model uncertainty → needs_user, no local special-case expand."""

        def fake_chat(prompt, api_key, system=None, **_k):
            return {
                "status": "needs_user",
                "fail_locus": "other",
                "speculation": "术语边界不明",
                "evidence": "recalled_memories 为空或未命中",
                "what_might_be_wrong": "可能指代多个实体",
                "next_step": "请用户澄清术语所指",
                "please_confirm_or_correct": "请确认该术语含义或给出纠正",
                "rollback_anchors": ["query_planner_start"],
                "demand_points": ["clarify"],
                "condition_trees": {},
                "result_projection": {"columns": []},
                "steps": [],
                "edges": [],
            }

        with patch(
            "pipeline.block_cross_table.query_engine.qp_ai.chat_json",
            side_effect=fake_chat,
        ):
            out = qp_call_ai(
                utterance="模糊术语X",
                portraits=[],
                catalog=self.catalog,
                tables_by_id=_tables(),
                api_key="test-key",
                recalled_memories=[],
                require_full_capability_cover=False,
            )
        self.assertEqual(out["status"], "needs_user")
        self.assertEqual(out.get("fail_locus"), "other")
        self.assertTrue((out.get("five_part") or {}).get("please_confirm_or_correct"))

    def test_memory_conn_path_injects_from_hybrid(self):
        captured = {}

        def fake_chat(prompt, api_key, system=None, **_k):
            captured["body"] = json.loads(prompt)
            return _or_plan(
                memory_ref=captured["body"]["recalled_memories"][0]["memory_ref"]
            )

        with patch(
            "pipeline.block_cross_table.query_engine.qp_ai.chat_json",
            side_effect=fake_chat,
        ):
            out = qp_call_ai(
                utterance=_TERM_SEC,
                portraits=[],
                catalog=self.catalog,
                tables_by_id=_tables(),
                api_key="test-key",
                memory_conn=self.conn,
                require_full_capability_cover=False,
            )
        self.assertTrue(captured["body"]["recalled_memories"])
        self.assertEqual(out["status"], "plan_ready")
        self.assertTrue(out["recalled_memories"])

    def test_normalize_skips_empty(self):
        self.assertEqual(normalize_recalled_memories([{"source": "x"}]), [])


if __name__ == "__main__":
    unittest.main()
