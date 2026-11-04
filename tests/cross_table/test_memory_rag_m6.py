# -*- coding: utf-8 -*-
"""
M6: dialogue-review mining + egress audit; directory-scan auto_learning removed.
CURRENT stays null. Mock chat — no live third-party.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from pipeline.block_cross_table.memory_rag.bootstrap import open_memory_db
from pipeline.block_cross_table.memory_rag.constants import (
    DIALOGUE_SESSION_DONE,
    EGRESS_LOCAL_ONLY,
    SOURCE_DIALOGUE_REVIEW,
    TABLE_DIALOGUE_SESSION,
    TABLE_EGRESS_AUDIT,
)
from pipeline.block_cross_table.memory_rag.dialogue_review import (
    append_dialogue_turn,
    assert_dialogue_text_safe,
    end_dialogue_session,
    start_dialogue_session,
)
from pipeline.block_cross_table.memory_rag.egress_audit import (
    assert_external_egress_allowed,
    list_egress_audits,
    record_egress,
    register_audit_sink,
    resolve_egress_policy,
)
from pipeline.block_cross_table.memory_rag.errors import MemoryRagError
from pipeline.block_cross_table.memory_rag.merge_settings import (
    save_merge_settings_patch,
)
from tests.cross_table.memory_rag_test_helpers import slots_for_tests
from tests.cross_table.test_memory_rag_m5_cpu_merge import _settings


class TestMemoryRagM6Dialogue(unittest.TestCase):
    def setUp(self):
        self.dim = int(slots_for_tests()["embedding_dim"])
        self.conn = open_memory_db(embedding_dim=self.dim, enable_vec=True)

    def tearDown(self):
        self.conn.close()

    def test_schema_has_dialogue_and_egress(self):
        tables = {
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn(TABLE_DIALOGUE_SESSION, tables)
        self.assertIn(TABLE_EGRESS_AUDIT, tables)

    def test_refuse_tabular_dialogue(self):
        dump = "\n".join(["a\tb\tc"] * 4)
        with self.assertRaises(MemoryRagError) as cm:
            assert_dialogue_text_safe(dump)
        self.assertEqual(cm.exception.subreason, "tabular_payload_forbidden")

    def test_dialogue_review_to_pending_with_mock(self):
        sess = start_dialogue_session(self.conn, title="t")
        sid = sess["session_id"]
        append_dialogue_turn(
            self.conn, session_id=sid, role="user", content="那个缩写 X 是什么意思"
        )
        append_dialogue_turn(
            self.conn,
            session_id=sid,
            role="assistant",
            content="X 在语境里指代某类凭证编号",
        )

        def fake(prompt: str, **_k):
            if "业务语义分析专家" in prompt or "items" in prompt:
                return {
                    "items": [
                        {
                            "term": "X",
                            "meaning": "某类凭证编号",
                            "evidence": "那个缩写 X",
                        }
                    ]
                }
            # equivalence path if hit
            return {"is_equivalent": False}

        result = end_dialogue_session(
            self.conn,
            session_id=sid,
            settings=_settings(maibot_data_dir=""),
            chat_json_fn=fake,
            async_review=False,
        )
        self.assertEqual(result["status"], DIALOGUE_SESSION_DONE)
        self.assertGreaterEqual(result["extracted_count"], 1)
        self.assertGreaterEqual(result["pending_count"], 1)

        from web.memory_correction.service import list_proposals

        props = list_proposals(self.conn, status="proposed")
        self.assertTrue(any(p.get("source_type") == SOURCE_DIALOGUE_REVIEW for p in props))

    def test_current_still_null(self):
        cols = [
            r[1]
            for r in self.conn.execute(
                "PRAGMA table_info(memory_binding_record)"
            ).fetchall()
        ]
        self.assertNotIn("CURRENT", cols)


class TestMemoryRagM6Egress(unittest.TestCase):
    def setUp(self):
        self.dim = int(slots_for_tests()["embedding_dim"])
        self.conn = open_memory_db(embedding_dim=self.dim, enable_vec=True)
        register_audit_sink(lambda: self.conn)
        self._prev_policy = resolve_egress_policy()

    def tearDown(self):
        register_audit_sink(None)
        save_merge_settings_patch({"egress_policy": "allow_external"})
        self.conn.close()

    def test_local_only_hard_blocks(self):
        save_merge_settings_patch({"egress_policy": EGRESS_LOCAL_ONLY})
        with self.assertRaises(MemoryRagError) as cm:
            assert_external_egress_allowed(channel="chat")
        self.assertEqual(cm.exception.subreason, "egress_local_only")

    def test_record_and_list_egress(self):
        aid = record_egress(
            target_url="https://example.test/v4/chat/completions",
            model_name="mock-model",
            payload_chars=12,
            payload_preview="user: hello Bearer sk-secret-should-redact",
            status="ok",
            channel="chat",
        )
        self.assertIsNotNone(aid)
        rows = list_egress_audits(self.conn, limit=5)
        self.assertGreaterEqual(len(rows), 1)
        self.assertNotIn("sk-secret", rows[0]["payload_preview"])
        self.assertEqual(rows[0]["model_name"], "mock-model")


class TestMemoryRagM6Web(unittest.TestCase):
    def setUp(self):
        self.dim = int(slots_for_tests()["embedding_dim"])
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = Path(self._tmpdir.name) / "m6.db"
        os.environ["MEMORY_CORRECTION_DB"] = str(self.db)
        os.environ["MEMORY_CORRECTION_EMBEDDING_DIM"] = str(self.dim)
        for name in list(sys.modules):
            if name.startswith("web.memory_correction"):
                del sys.modules[name]
        from fastapi.testclient import TestClient
        from web.memory_correction import app as app_mod
        from web.memory_correction.auth import ensure_token

        self.app_mod = app_mod
        self.token = ensure_token(app_mod.ROOT)
        self.headers = {"X-Memory-Token": self.token}
        self._client_cm = TestClient(app_mod.app)
        self.client = self._client_cm.__enter__()

    def tearDown(self):
        try:
            self._client_cm.__exit__(None, None, None)
        except Exception:
            pass
        os.environ.pop("MEMORY_CORRECTION_DB", None)
        self._tmpdir.cleanup()

    def test_no_scan_ui_and_dialogue_apis(self):
        html = self.client.get("/").text
        self.assertNotIn("监视目录", html)
        self.assertNotIn("立即扫描一次", html)
        self.assertNotIn("btn-auto-once", html)
        self.assertIn("对话复盘", html)
        self.assertIn("数据外带审计", html)
        self.assertIn("egress_policy", html)

        r = self.client.post(
            "/api/dialogue/sessions",
            headers=self.headers,
            data={"title": "m6"},
        )
        self.assertEqual(r.status_code, 200)
        sid = r.json()["session_id"]
        t = self.client.post(
            "/api/dialogue/turns",
            headers=self.headers,
            data={"session_id": sid, "role": "user", "content": "追问一句"},
        )
        self.assertEqual(t.status_code, 200)

        dash = self.client.get("/api/dashboard", headers=self.headers)
        self.assertEqual(dash.status_code, 200)
        body = dash.json()
        self.assertIn("egress_audits", body)
        self.assertIn("inbox", body)
        self.assertNotIn("auto_learning_enabled", body)

    def test_settings_egress_policy(self):
        r = self.client.post(
            "/api/settings",
            headers={**self.headers, "Content-Type": "application/json"},
            json={"egress_policy": "local_only", "dialogue_review_enabled": True},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["settings"]["egress_policy"], "local_only")
        # restore
        self.client.post(
            "/api/settings",
            headers={**self.headers, "Content-Type": "application/json"},
            json={"egress_policy": "allow_external"},
        )


if __name__ == "__main__":
    unittest.main()
