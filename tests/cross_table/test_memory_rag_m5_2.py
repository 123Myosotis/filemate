# -*- coding: utf-8 -*-
"""
M5.2: dual self-learning engine (vector/chat), unified settings.
(Directory-scan auto-learn removed in M6.)
CURRENT stays null. Mock chat injection — no live third-party.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pipeline.block_cross_table.memory_rag.bootstrap import open_memory_db
from pipeline.block_cross_table.memory_rag.constants import (
    DEDUP_CONFLICT,
    DEDUP_SILENT_SKIP,
    ENGINE_CHAT,
    ENGINE_VECTOR,
    MEANING_EQ_AI,
    MEANING_EQ_LOCAL,
)
from pipeline.block_cross_table.memory_rag.jargon_dedup import (
    apply_dedup_after_inference,
    classify_against_maibot,
)
from pipeline.block_cross_table.memory_rag.jargon_store import upsert_filemate_jargon
from pipeline.block_cross_table.memory_rag.meaning_equivalence import (
    METHOD_AI,
    METHOD_FALLBACK,
    METHOD_VECTOR_SCREEN,
    meanings_equivalent_for_engine,
)
from pipeline.block_cross_table.memory_rag.merge_settings import (
    load_merge_runtime_settings,
    save_merge_settings_patch,
)
from tests.cross_table.memory_rag_test_helpers import slots_for_tests
from tests.cross_table.test_memory_rag_m5_cpu_merge import _make_maibot_db, _settings


class TestMemoryRagM52(unittest.TestCase):
    def setUp(self):
        self.dim = int(slots_for_tests()["embedding_dim"])
        self.conn = open_memory_db(embedding_dim=self.dim, enable_vec=True)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self.conn.close()
        self._tmpdir.cleanup()

    def test_chat_engine_uses_ai_mock(self):
        calls = {"n": 0}

        def fake(prompt: str, **_k):
            calls["n"] += 1
            self.assertIn("句子 A:", prompt)
            return {"is_equivalent": True}

        v = meanings_equivalent_for_engine(
            "甲含义",
            "乙同义不同措辞",
            engine=ENGINE_CHAT,
            meaning_equivalence_mode=MEANING_EQ_AI,
            fallback_threshold=0.6,
            chat_json_fn=fake,
        )
        self.assertTrue(v.equivalent)
        self.assertEqual(v.method, METHOD_AI)
        self.assertEqual(calls["n"], 1)

    def test_vector_local_uses_sequence_matcher(self):
        calls = {"n": 0}

        def fake(_prompt: str, **_k):
            calls["n"] += 1
            return {"is_equivalent": False}

        v = meanings_equivalent_for_engine(
            "苹果手机",
            "苹果 手机",
            engine=ENGINE_VECTOR,
            meaning_equivalence_mode=MEANING_EQ_LOCAL,
            fallback_threshold=0.6,
            chat_json_fn=fake,
        )
        self.assertTrue(v.equivalent)
        self.assertEqual(v.method, METHOD_FALLBACK)
        self.assertEqual(calls["n"], 0)

    def test_vector_ai_screen_rejects_low_sim(self):
        calls = {"n": 0}

        def fake(_prompt: str, **_k):
            calls["n"] += 1
            return {"is_equivalent": True}

        v = meanings_equivalent_for_engine(
            "完全不同的含义甲",
            "XYZ unrelated meaning",
            engine=ENGINE_VECTOR,
            meaning_equivalence_mode=MEANING_EQ_AI,
            fallback_threshold=0.6,
            chat_json_fn=fake,
        )
        self.assertFalse(v.equivalent)
        self.assertEqual(v.method, METHOD_VECTOR_SCREEN)
        self.assertEqual(calls["n"], 0)

    def test_vector_ai_screen_then_ai_confirm(self):
        calls = {"n": 0}

        def fake(_prompt: str, **_k):
            calls["n"] += 1
            return {"is_equivalent": False}

        v = meanings_equivalent_for_engine(
            "苹果手机",
            "苹果 手机",
            engine=ENGINE_VECTOR,
            meaning_equivalence_mode=MEANING_EQ_AI,
            fallback_threshold=0.6,
            chat_json_fn=fake,
        )
        self.assertFalse(v.equivalent)
        self.assertEqual(v.method, METHOD_AI)
        self.assertEqual(calls["n"], 1)

    def test_dedup_chat_engine_silent_via_ai(self):
        _make_maibot_db(self.tmp / "MaiBot.db", [("术语甲", "旧含义")])

        def fake(_prompt: str, **_k):
            return {"is_equivalent": True}

        d = apply_dedup_after_inference(
            self.conn,
            content="术语甲",
            meaning="新措辞同义",
            settings=_settings(
                maibot_data_dir=str(self.tmp),
                self_learning_engine=ENGINE_CHAT,
            ),
            chat_json_fn=fake,
        )
        self.assertEqual(d.disposition, DEDUP_SILENT_SKIP)

    def test_dedup_also_checks_filemate_jargons(self):
        upsert_filemate_jargon(
            self.conn, content="本地词", meaning="本地含义", count=1
        )

        def fake(_prompt: str, **_k):
            return {"is_equivalent": True}

        d = apply_dedup_after_inference(
            self.conn,
            content="本地词",
            meaning="同义改写",
            settings=_settings(
                maibot_data_dir="",
                self_learning_engine=ENGINE_CHAT,
            ),
            chat_json_fn=fake,
        )
        self.assertEqual(d.disposition, DEDUP_SILENT_SKIP)

    def test_dedup_vector_local_conflict(self):
        _make_maibot_db(self.tmp / "MaiBot.db", [("术语乙", "含义完全不同AAA")])

        d = apply_dedup_after_inference(
            self.conn,
            content="术语乙",
            meaning="另一个无关含义BBB",
            settings=_settings(
                maibot_data_dir=str(self.tmp),
                self_learning_engine=ENGINE_VECTOR,
                meaning_equivalence_mode=MEANING_EQ_LOCAL,
                meaning_fallback_threshold=0.6,
            ),
        )
        self.assertEqual(d.disposition, DEDUP_CONFLICT)

    def test_settings_engine_persist(self):
        save_merge_settings_patch(
            {
                "self_learning_engine": "vector",
                "meaning_equivalence_mode": "local",
            }
        )
        s = load_merge_runtime_settings()
        self.assertEqual(s.self_learning_engine, ENGINE_VECTOR)
        self.assertEqual(s.meaning_equivalence_mode, MEANING_EQ_LOCAL)
        save_merge_settings_patch(
            {
                "self_learning_engine": "chat",
                "meaning_equivalence_mode": "ai",
            }
        )

    def test_current_still_null(self):
        cols = [
            r[1]
            for r in self.conn.execute(
                "PRAGMA table_info(memory_binding_record)"
            ).fetchall()
        ]
        self.assertNotIn("CURRENT", cols)

    def test_classify_engine_param_chat(self):
        def fake(_p, **_k):
            return {"is_equivalent": False}

        disp, _, _, meta = classify_against_maibot(
            content="c",
            meaning="m1",
            maibot_rows=[{"meaning": "m2 totally different xyz"}],
            fallback_threshold=0.6,
            chat_json_fn=fake,
            engine=ENGINE_CHAT,
            meaning_equivalence_mode=MEANING_EQ_AI,
        )
        self.assertEqual(disp, DEDUP_CONFLICT)
        self.assertIn("equivalence_methods", meta)


class TestMemoryRagM52Web(unittest.TestCase):
    def setUp(self):
        import os
        import sys

        self.dim = int(slots_for_tests()["embedding_dim"])
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = Path(self._tmpdir.name) / "m52.db"
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
        self.header = {"X-Memory-Token": self.token}
        self._client_cm = TestClient(app_mod.app)
        self.client = self._client_cm.__enter__()

    def tearDown(self):
        import os

        try:
            self._client_cm.__exit__(None, None, None)
        except Exception:
            pass
        os.environ.pop("MEMORY_CORRECTION_DB", None)
        self._tmpdir.cleanup()

    def test_settings_and_dashboard_endpoints(self):
        r = self.client.get("/api/settings", headers=self.header)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("self_learning_engine", body)
        self.assertIn("model", body)
        self.assertIn("api_base_url", body["model"])

        r2 = self.client.post(
            "/api/settings",
            headers={**self.header, "Content-Type": "application/json"},
            json={
                "self_learning_engine": "vector",
                "meaning_equivalence_mode": "local",
                "fusion": {"vector_weight": 0.6, "bm25_weight": 0.4},
            },
        )
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(
            r2.json()["settings"]["self_learning_engine"], "vector"
        )

        r3 = self.client.get("/api/dashboard", headers=self.header)
        self.assertEqual(r3.status_code, 200)
        dash = r3.json()
        self.assertIn("self_learning_engine", dash)
        self.assertIn("pending_proposals", dash)
        self.assertIn("conflicts", dash)
        self.assertIn("job_stats", dash)

        html = self.client.get("/")
        self.assertEqual(html.status_code, 200)
        self.assertIn("记忆看板", html.text)
        self.assertIn("自学习引擎", html.text)

        self.client.post(
            "/api/settings",
            headers={**self.header, "Content-Type": "application/json"},
            json={"self_learning_engine": "chat", "meaning_equivalence_mode": "ai"},
        )


if __name__ == "__main__":
    unittest.main()
