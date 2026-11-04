# -*- coding: utf-8 -*-
"""Intent router: tool must come from LLM; local fallback is structure-only."""
from __future__ import annotations

import inspect
import unittest
from unittest import mock

from agent_core import intent_router
from agent_core.intent_router import (
    VALID_TOOLS,
    _infer_tool_from_actions,
    _normalize,
)


class TestInferToolStructuralOnly(unittest.TestCase):
    def test_no_business_regex_in_infer_source(self):
        src = inspect.getsource(_infer_tool_from_actions)
        for w in ("设备", "数据", "漏扫", "值班", "服务器", "安防", "re.search"):
            self.assertNotIn(w, src)

    def test_file_only(self):
        self.assertEqual(
            _infer_tool_from_actions(
                "execute",
                [{"domain": "file", "op": "move", "params": {}}],
            ),
            "file_organize",
        )

    def test_content_with_targets(self):
        self.assertEqual(
            _infer_tool_from_actions(
                "execute",
                [{"domain": "content", "op": "report", "all": True, "target_files": []}],
            ),
            "table_synthesize",
        )

    def test_empty_actions_cannot_guess_table_query(self):
        self.assertIsNone(_infer_tool_from_actions("execute", []))

    def test_normalize_missing_tool_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _normalize({"mode": "execute", "actions": [], "subdir": "", "plan": ""})
        self.assertIn("tool", str(ctx.exception).lower())


class TestRouteServerUtterance(unittest.TestCase):
    def test_route_find_server_vuln_returns_table_query(self):
        """
        LLM path: utterance about finding servers/vulns in Excel → tool=table_query.
        Local must not regex-guess; we stub the model JSON.
        """
        utterance = "帮我找出这批 Excel 里所有【服务器】的漏洞"
        fake = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"tool":"table_query","mode":"execute","subdir":"",'
                            '"actions":[],"plan":"跨表查询服务器漏洞"}'
                        )
                    }
                }
            ]
        }
        with mock.patch("config.get_api_key", return_value="test-key"):
            with mock.patch("ai.client.chat_completion", return_value=fake):
                with mock.patch.object(
                    intent_router,
                    "_scan",
                    return_value=(
                        __import__("pathlib").Path("."),
                        [],
                        ["a.xlsx"],
                        {},
                    ),
                ):
                    out = intent_router.route(utterance, ".")
        self.assertIsNotNone(out)
        self.assertEqual(out["tool"], "table_query")
        self.assertEqual(out["mode"], "execute")
        self.assertIn(out["tool"], VALID_TOOLS)

    def test_route_retries_when_tool_missing(self):
        bad = {
            "choices": [
                {
                    "message": {
                        "content": '{"mode":"execute","subdir":"","actions":[],"plan":""}'
                    }
                }
            ]
        }
        good = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"tool":"table_query","mode":"execute","subdir":"",'
                            '"actions":[],"plan":"ok"}'
                        )
                    }
                }
            ]
        }
        calls = {"n": 0}

        def fake_chat(*_a, **_k):
            calls["n"] += 1
            return bad if calls["n"] == 1 else good

        with mock.patch("config.get_api_key", return_value="test-key"):
            with mock.patch("config.get_param", side_effect=lambda k: {
                "router.max_retries": 3,
                "router.retry_waits": [0],
                "router.max_tokens": 512,
                "router.temperature": 0,
                "router.timeout": 30,
                "router.max_list_files": 50,
                "router.max_list_subdirs": 20,
                "router.skip_dirs": [],
            }.get(k, 0)):
                with mock.patch("ai.client.chat_completion", side_effect=fake_chat):
                    with mock.patch.object(
                        intent_router,
                        "_scan",
                        return_value=(
                            __import__("pathlib").Path("."),
                            [],
                            ["a.xlsx"],
                            {},
                        ),
                    ):
                        out = intent_router.route("任意查表原话", ".")
        self.assertIsNotNone(out)
        self.assertEqual(out["tool"], "table_query")
        self.assertGreaterEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
