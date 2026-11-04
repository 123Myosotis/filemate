# -*- coding: utf-8 -*-
"""P2-A: V18 network reconnect + V20 table-alias prefix strip."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from pipeline.block_cross_table.query_engine.errors import QueryEngineError
from pipeline.block_cross_table.query_engine.qp_ai import (
    QP_SYSTEM,
    _is_transport_wall_failure,
    _network_reconnect_budget,
    _normalize_plan,
    check_plan_ready_projection_columns,
    qp_call_ai,
    strip_table_alias_prefix,
    strip_table_alias_prefixes_in_plan,
)


class TestV20AliasStrip(unittest.TestCase):
    def test_strip_t_a_prefix(self):
        self.assertEqual(
            strip_table_alias_prefix("t_a.资产名称", table_ids=["t_a", "t_b"]),
            "资产名称",
        )

    def test_strip_when_right_in_known_columns(self):
        self.assertEqual(
            strip_table_alias_prefix(
                "weird.col_x",
                table_ids=["t1"],
                known_columns={"col_x"},
            ),
            "col_x",
        )

    def test_keep_dotted_when_unknown(self):
        self.assertEqual(
            strip_table_alias_prefix("foo.bar", table_ids=["t1"], known_columns=set()),
            "foo.bar",
        )

    def test_normalize_strips_projection_and_args(self):
        raw = {
            "status": "plan_ready",
            "steps": [
                {
                    "step_id": "s0",
                    "op": "op_join",
                    "args": {"left_key": "t_a.id", "right_key": "t_b.id"},
                    "inputs": ["src:t_a", "src:t_b"],
                }
            ],
            "result_projection": {
                "columns": [{"column_id": "t_a.资产名称", "display_name": "x"}]
            },
            "condition_trees": {
                "ct1": {
                    "op": "AND",
                    "children": [{"atom": {"column_id": "t_a.资产名称", "cmp": "eq"}}],
                }
            },
            "edges": [],
            "demand_points": [],
        }
        out = _normalize_plan(raw, table_ids=["t_a", "t_b"])
        self.assertEqual(
            out["result_projection"]["columns"][0]["column_id"], "资产名称"
        )
        out2 = strip_table_alias_prefixes_in_plan(
            out,
            table_ids=["t_a", "t_b"],
            tables_by_id={
                "t_a": {"columns": ["资产名称", "id"]},
                "t_b": {"columns": ["id"]},
            },
        )
        self.assertEqual(out2["steps"][0]["args"]["left_key"], "id")
        self.assertEqual(out2["steps"][0]["args"]["right_key"], "id")
        self.assertEqual(
            out2["condition_trees"]["ct1"]["children"][0]["atom"]["column_id"],
            "资产名称",
        )
        check_plan_ready_projection_columns(
            out2,
            {
                "t_a": {"columns": ["资产名称", "id"]},
                "t_b": {"columns": ["id"]},
            },
        )

    def test_qp_system_bans_alias_prefix(self):
        self.assertIn("bare column names", QP_SYSTEM)
        self.assertIn("t_a.col", QP_SYSTEM)


class TestV18NetworkReconnect(unittest.TestCase):
    def test_budget_from_env(self):
        old = os.environ.get("QP_NETWORK_RECONNECT")
        try:
            os.environ["QP_NETWORK_RECONNECT"] = "2"
            self.assertEqual(_network_reconnect_budget(), 2)
            os.environ["QP_NETWORK_RECONNECT"] = "0"
            self.assertEqual(_network_reconnect_budget(), 0)
        finally:
            if old is None:
                os.environ.pop("QP_NETWORK_RECONNECT", None)
            else:
                os.environ["QP_NETWORK_RECONNECT"] = old

    def test_classify_timeout_not_ast(self):
        self.assertTrue(
            _is_transport_wall_failure(TimeoutError("timed out"))
        )
        self.assertFalse(
            _is_transport_wall_failure(
                QueryEngineError("column missing", fail_locus="projection")
            )
        )

    def test_reconnect_once_then_success(self):
        os.environ["QP_NETWORK_RECONNECT"] = "1"
        good_plan = {
            "status": "plan_ready",
            "query_time_basis": "data_max_time",
            "rollback_anchors": ["query_planner_start"],
            "demand_points": [
                {
                    "demand_id": "d0",
                    "kind": "filter",
                    "utterance_span": "u",
                    "bound_step_id": "s0",
                    "bound_column_or_filter": "c",
                }
            ],
            "condition_trees": {},
            "result_projection": {
                "columns": [
                    {
                        "column_id": "col_a",
                        "display_name": "col_a",
                        "column_name_source": "from_ai_runtime",
                        "source_step_id": "s0",
                        "derive_rule_ref": None,
                        "provenance": {
                            "utterance_span": "hello",
                            "portrait_path": "tables/t1/columns/col_a",
                        },
                    }
                ]
            },
            "steps": [
                {
                    "step_id": "s0",
                    "op": "op_project",
                    "args": {},
                    "inputs": ["src:t1"],
                    "output": "s0",
                    "provenance": {"utterance_span": "hello"},
                }
            ],
            "edges": [],
        }
        calls = {"n": 0}

        def fake_chat_json(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("wall clock disconnect")
            return good_plan

        catalog = {
            "operators": {
                "op_project": {"args_schema": {}},
            }
        }
        tables = {"t1": {"columns": ["col_a"], "rows": [{"col_a": 1}]}}
        with mock.patch(
            "pipeline.block_cross_table.query_engine.qp_ai.chat_json",
            side_effect=fake_chat_json,
        ):
            with mock.patch(
                "pipeline.block_cross_table.query_engine.qp_ai.validate_query_plan_ast",
                return_value={"status": "plan_ready", "ok": True},
            ):
                with mock.patch(
                    "pipeline.block_cross_table.query_engine.qp_ai.check_plan_ready_condition_trees",
                    return_value=None,
                ):
                    out = qp_call_ai(
                        utterance="hello",
                        portraits=[{"table_id": "t1"}],
                        catalog=catalog,
                        tables_by_id=tables,
                        api_key="k",
                        apply_column_repair=False,
                        max_retries=0,
                    )
        self.assertEqual(out.get("status"), "plan_ready")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertGreaterEqual(int(out.get("ai_calls") or 0), 2)

    def test_business_error_not_swallowed_by_network_path(self):
        """AST / projection errors must still surface after retries — not network path."""
        bad = {
            "status": "plan_ready",
            "rollback_anchors": ["query_planner_start"],
            "demand_points": [{"demand_id": "d0"}],
            "condition_trees": {},
            "result_projection": {"columns": [{"column_id": "no_such"}]},
            "steps": [
                {
                    "step_id": "s0",
                    "op": "op_project",
                    "args": {},
                    "inputs": ["src:t1"],
                    "output": "s0",
                    "provenance": {"utterance_span": "x"},
                }
            ],
            "edges": [],
        }
        catalog = {"operators": {"op_project": {"args_schema": {}}}}
        tables = {"t1": {"columns": ["col_a"], "rows": []}}
        with mock.patch(
            "pipeline.block_cross_table.query_engine.qp_ai.chat_json",
            return_value=bad,
        ):
            with self.assertRaises(QueryEngineError) as ctx:
                qp_call_ai(
                    utterance="x",
                    portraits=[{"table_id": "t1"}],
                    catalog=catalog,
                    tables_by_id=tables,
                    api_key="k",
                    apply_column_repair=False,
                    max_retries=0,
                )
        self.assertIn("projection", str(getattr(ctx.exception, "fail_locus", "")) + str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
