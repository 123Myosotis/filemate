# -*- coding: utf-8 -*-
"""
M5-CPU-Merge: MaiBot RO, dual-db dedup, FTS+RRF hybrid, TXT mining.
CURRENT stays null. No live third-party required (inject chat_json_fn).
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from pipeline.block_cross_table.memory_rag.bootstrap import open_memory_db
from pipeline.block_cross_table.memory_rag.constants import (
    CONFLICT_STATUS_OPEN,
    DEDUP_CONFLICT,
    DEDUP_PENDING_CONFIRM,
    DEDUP_SILENT_SKIP,
    TABLE_BINDINGS_FTS,
    TABLE_FILEMATE_JARGONS,
    TABLE_JARGON_CONFLICTS,
)
from pipeline.block_cross_table.memory_rag.fts_index import sparse_search_bindings
from pipeline.block_cross_table.memory_rag.hybrid_recall import hybrid_recall, merge_jargon_corpora
from pipeline.block_cross_table.memory_rag.jargon_dedup import (
    apply_dedup_after_inference,
    classify_against_maibot,
    list_open_conflicts,
    resolve_conflict,
)
from pipeline.block_cross_table.memory_rag.jargon_miner import (
    import_txt_and_mine,
    should_infer_meaning,
)
from pipeline.block_cross_table.memory_rag.jargon_store import (
    confirm_jargon_with_binding,
    upsert_filemate_jargon,
)
from pipeline.block_cross_table.memory_rag.maibot_ro import (
    discover_maibot_db_path,
    open_maibot_ro,
)
from pipeline.block_cross_table.memory_rag.meaning_similarity import meanings_equivalent
from pipeline.block_cross_table.memory_rag.merge_settings import (
    MergeRuntimeSettings,
    load_merge_runtime_settings,
)
from pipeline.block_cross_table.memory_rag.store import insert_binding_confirmed
from pipeline.block_cross_table.memory_rag.weighted_rrf import weighted_rrf
from tests.cross_table.memory_rag_test_helpers import slots_for_tests


def _settings(**kwargs) -> MergeRuntimeSettings:
    base = load_merge_runtime_settings()
    data = {
        "maibot_data_dir": kwargs.get("maibot_data_dir", ""),
        "meaning_similarity_threshold": kwargs.get(
            "meaning_similarity_threshold", base.meaning_similarity_threshold
        ),
        "meaning_fallback_threshold": kwargs.get(
            "meaning_fallback_threshold", base.meaning_fallback_threshold
        ),
        "jargon_inference_thresholds": kwargs.get(
            "jargon_inference_thresholds", base.jargon_inference_thresholds
        ),
        "jargon_stopwords": kwargs.get(
            "jargon_stopwords", base.jargon_stopwords
        ),
        "rrf_k": kwargs.get("rrf_k", base.rrf_k),
        "vector_weight": kwargs.get("vector_weight", base.vector_weight),
        "bm25_weight": kwargs.get("bm25_weight", base.bm25_weight),
        "sparse_top_k": kwargs.get("sparse_top_k", base.sparse_top_k),
        "dense_top_k": kwargs.get("dense_top_k", base.dense_top_k),
        "hybrid_final_top_k": kwargs.get("hybrid_final_top_k", base.hybrid_final_top_k),
        "self_learning_engine": kwargs.get(
            "self_learning_engine", base.self_learning_engine
        ),
        "meaning_equivalence_mode": kwargs.get(
            "meaning_equivalence_mode", base.meaning_equivalence_mode
        ),
        "egress_policy": kwargs.get("egress_policy", base.egress_policy),
        "dialogue_review_enabled": kwargs.get(
            "dialogue_review_enabled", base.dialogue_review_enabled
        ),
        "embedded_model_path": kwargs.get(
            "embedded_model_path", base.embedded_model_path
        ),
    }
    return MergeRuntimeSettings(**data)


def _make_maibot_db(path: Path, rows: list[tuple[str, str]]) -> Path:
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE jargons (
            id INTEGER PRIMARY KEY,
            content TEXT NOT NULL,
            meaning TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            is_jargon INTEGER,
            is_complete INTEGER NOT NULL DEFAULT 0,
            is_global INTEGER NOT NULL DEFAULT 0,
            session_id_dict TEXT NOT NULL DEFAULT '{}',
            last_inference_count INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL DEFAULT 'AI'
        )
        """
    )
    for i, (content, meaning) in enumerate(rows, start=1):
        conn.execute(
            """
            INSERT INTO jargons(id, content, meaning, count, is_jargon, is_global, session_id_dict)
            VALUES (?, ?, ?, ?, 1, 0, '{}')
            """,
            (i, content, meaning, 10),
        )
    conn.commit()
    conn.close()
    return path


class TestMemoryRagM5CpuMerge(unittest.TestCase):
    def setUp(self):
        self.dim = int(slots_for_tests()["embedding_dim"])
        self.conn = open_memory_db(embedding_dim=self.dim, enable_vec=True)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self.conn.close()
        self._tmpdir.cleanup()

    def test_schema_has_merge_tables_and_fts(self):
        names = {
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
        }
        self.assertIn(TABLE_FILEMATE_JARGONS, names)
        self.assertIn(TABLE_JARGON_CONFLICTS, names)
        self.assertIn(TABLE_BINDINGS_FTS, names)

    def test_maibot_ro_rejects_write(self):
        db = _make_maibot_db(self.tmp / "MaiBot.db", [("alpha", "meaning-a")])
        with open_maibot_ro(self.tmp) as mconn:
            self.assertIsNotNone(mconn)
            with self.assertRaises(sqlite3.Error):
                mconn.execute(
                    "INSERT INTO jargons(content, meaning, count, is_global, session_id_dict) "
                    "VALUES ('x','y',1,0,'{}')"
                )
                mconn.commit()

    def test_discover_maibot_db_path(self):
        db = _make_maibot_db(self.tmp / "MaiBot.db", [])
        self.assertEqual(discover_maibot_db_path(self.tmp), db.resolve())

    def test_meaning_equivalent_and_classify(self):
        self.assertTrue(
            meanings_equivalent("苹果手机", "苹果 手机", threshold=0.9)
        )
        rows = [{"meaning": "完全不同的解释XYZ"}]

        def ai_neq(_prompt, **_k):
            return {"is_equivalent": False}

        def ai_eq(_prompt, **_k):
            return {"is_equivalent": True}

        disp, _, _, _ = classify_against_maibot(
            content="词",
            meaning="另一套含义ABC",
            maibot_rows=rows,
            fallback_threshold=0.6,
            chat_json_fn=ai_neq,
        )
        self.assertEqual(disp, DEDUP_CONFLICT)
        disp2, _, _, _ = classify_against_maibot(
            content="词",
            meaning="另一套含义ABC",
            maibot_rows=rows,
            fallback_threshold=0.6,
            chat_json_fn=ai_eq,
        )
        self.assertEqual(disp2, DEDUP_SILENT_SKIP)
        disp3, _, _, _ = classify_against_maibot(
            content="新词",
            meaning="含义",
            maibot_rows=[],
            fallback_threshold=0.6,
        )
        self.assertEqual(disp3, DEDUP_PENDING_CONFIRM)

    def test_dedup_silent_skip_no_proposal(self):
        _make_maibot_db(self.tmp / "MaiBot.db", [("术语甲", "统一含义")])
        before = self.conn.execute(
            "SELECT COUNT(*) FROM memory_edit_proposal"
        ).fetchone()[0]
        decision = apply_dedup_after_inference(
            self.conn,
            content="术语甲",
            meaning="统一含义",
            settings=_settings(maibot_data_dir=str(self.tmp)),
            chat_json_fn=lambda *_a, **_k: {"is_equivalent": True},
        )
        self.assertEqual(decision.disposition, DEDUP_SILENT_SKIP)
        after = self.conn.execute(
            "SELECT COUNT(*) FROM memory_edit_proposal"
        ).fetchone()[0]
        self.assertEqual(before, after)
        conflicts = list_open_conflicts(self.conn)
        self.assertEqual(conflicts, [])

    def test_dedup_conflict_opens_row(self):
        _make_maibot_db(self.tmp / "MaiBot.db", [("术语乙", "MaiBot侧含义")])
        decision = apply_dedup_after_inference(
            self.conn,
            content="术语乙",
            meaning="FileMate侧完全不同含义",
            settings=_settings(maibot_data_dir=str(self.tmp)),
            chat_json_fn=lambda *_a, **_k: {"is_equivalent": False},
        )
        self.assertEqual(decision.disposition, DEDUP_CONFLICT)
        self.assertIsNotNone(decision.conflict_id)
        open_rows = list_open_conflicts(self.conn)
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0]["status"], CONFLICT_STATUS_OPEN)

    def test_dedup_pending_creates_proposal(self):
        decision = apply_dedup_after_inference(
            self.conn,
            content="全新词",
            meaning="本地新含义",
            settings=_settings(maibot_data_dir=""),
        )
        self.assertEqual(decision.disposition, DEDUP_PENDING_CONFIRM)
        self.assertTrue(decision.proposal_id)
        row = self.conn.execute(
            "SELECT status FROM memory_edit_proposal WHERE proposal_id=?",
            (decision.proposal_id,),
        ).fetchone()
        self.assertEqual(row[0], "proposed")

    def test_resolve_conflict_keep_filemate(self):
        _make_maibot_db(self.tmp / "MaiBot.db", [("冲突词", "旧义")])
        decision = apply_dedup_after_inference(
            self.conn,
            content="冲突词",
            meaning="新义完全不同",
            settings=_settings(maibot_data_dir=str(self.tmp)),
            chat_json_fn=lambda *_a, **_k: {"is_equivalent": False},
        )
        out = resolve_conflict(
            self.conn,
            conflict_id=decision.conflict_id,
            resolution="keep_filemate",
        )
        self.assertEqual(out["status"], "keep_filemate")
        self.assertTrue(out.get("proposal_id"))

    def test_weighted_rrf_order(self):
        a = [{"id": "x", "score": 1}, {"id": "y", "score": 0.5}]
        b = [{"id": "y", "score": 1}, {"id": "z", "score": 0.5}]
        fused = weighted_rrf([a, b], weights=[0.7, 0.3], rrf_k=60)
        ids = [item["id"] for item in fused]
        self.assertIn("x", ids)
        self.assertIn("y", ids)
        self.assertEqual(len(ids), 3)

    def test_fts_and_hybrid_recall(self):
        insert_binding_confirmed(
            self.conn,
            user_confirmed=True,
            utterance_span_fingerprint="fp1",
            binding_payload={
                "term": "跨表键",
                "description": "用于连接两张表的键",
                "values": [],
                "scope": "",
            },
            source_trace_id="t1",
            confirmed_at="2026-10-21T00:00:00+00:00",
            scope={},
        )
        upsert_filemate_jargon(
            self.conn,
            content="跨表键",
            meaning="用于连接两张表的键",
            count=5,
        )
        hits = sparse_search_bindings(self.conn, query="跨表键", top_k=5)
        self.assertTrue(len(hits) >= 1)
        result = hybrid_recall(
            self.conn,
            query="跨表键",
            query_embedding=None,
            settings=_settings(maibot_data_dir=""),
        )
        self.assertGreaterEqual(len(result["accepted"]), 1)

    def test_should_infer_thresholds(self):
        th = (4, 8, 25, 100)
        self.assertFalse(
            should_infer_meaning(
                count=3, last_inference_count=0, is_complete=False, thresholds=th
            )
        )
        self.assertTrue(
            should_infer_meaning(
                count=4, last_inference_count=0, is_complete=False, thresholds=th
            )
        )
        self.assertFalse(
            should_infer_meaning(
                count=7, last_inference_count=4, is_complete=False, thresholds=th
            )
        )
        self.assertTrue(
            should_infer_meaning(
                count=8, last_inference_count=4, is_complete=False, thresholds=th
            )
        )
        self.assertFalse(
            should_infer_meaning(
                count=100, last_inference_count=25, is_complete=True, thresholds=th
            )
        )

    def test_txt_mine_with_injected_chat(self):
        # Force threshold=1 via settings so a repeated term triggers.
        settings = _settings(
            maibot_data_dir="",
            jargon_inference_thresholds=(1, 2, 3, 4),
        )
        calls = {"n": 0}

        def fake_chat(prompt: str, **_kwargs):
            calls["n"] += 1
            if "仅根据词条本身" in prompt or "仅基于词条" in prompt:
                return {"meaning": "字面义"}
            if "比较" in prompt or "is_similar" in prompt or "推断结果1" in prompt:
                return {"is_similar": False, "reason": "不同"}
            return {"meaning": "上下文义-特有说法", "no_info": False}

        text = "甲词 甲词 甲词 甲词 其他内容"
        out = import_txt_and_mine(
            self.conn,
            text=text,
            source_name="sample.txt",
            settings=settings,
            chat_json_fn=fake_chat,
            max_infer_terms=5,
            cjk_n_min=2,
            cjk_n_max=2,
        )
        self.assertEqual(out["status"], "done")
        self.assertGreaterEqual(calls["n"], 1)
        self.assertGreaterEqual(
            out["pending_count"] + out["silent_skip_count"] + out["conflict_count"],
            0,
        )

    def test_confirm_jargon_writes_binding_and_local_table(self):
        result = confirm_jargon_with_binding(
            self.conn,
            content="确认词",
            meaning="确认含义",
            count=4,
        )
        self.assertTrue(result["binding_id"])
        row = self.conn.execute(
            f"SELECT content FROM {TABLE_FILEMATE_JARGONS} WHERE jargon_id=?",
            (result["jargon_id"],),
        ).fetchone()
        self.assertEqual(row[0], "确认词")

    def test_merge_corpora_dedupes_same_meaning(self):
        _make_maibot_db(self.tmp / "MaiBot.db", [("同义词", "同一含义")])
        upsert_filemate_jargon(
            self.conn, content="同义词", meaning="同一含义", count=3
        )
        merged = merge_jargon_corpora(
            self.conn,
            settings=_settings(maibot_data_dir=str(self.tmp)),
        )
        same = [m for m in merged if m.get("content") == "同义词"]
        self.assertEqual(len(same), 1)

    def test_current_still_null(self):
        from pipeline.block_cross_table.memory_rag.production_slots import (
            load_memory_numeric_slots_from_current,
        )

        slots = load_memory_numeric_slots_from_current()
        self.assertIsNone(slots.get("embedding_dim"))
        self.assertIsNone(slots.get("memory_recall_top_k"))


class TestMemoryRagM5CpuMergeWeb(unittest.TestCase):
    def setUp(self):
        self.dim = int(slots_for_tests()["embedding_dim"])
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "mem.db"
        os.environ["MEMORY_CORRECTION_DB"] = str(self.db_path)
        os.environ["MEMORY_CORRECTION_EMBEDDING_DIM"] = str(self.dim)
        import sys

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

    def test_settings_and_conflicts_endpoints(self):
        r = self.client.get("/api/settings", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertIn("maibot_data_dir", r.json())
        r2 = self.client.post(
            "/api/settings/maibot_data_dir",
            headers=self.headers,
            data={"maibot_data_dir": self._tmpdir.name},
        )
        self.assertEqual(r2.status_code, 200)
        r3 = self.client.get("/api/conflicts", headers=self.headers)
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(r3.json().get("conflicts"), [])
        r4 = self.client.get("/api/mining/status", headers=self.headers)
        self.assertEqual(r4.status_code, 200)
        html = self.client.get("/")
        self.assertEqual(html.status_code, 200)
        body = html.text
        self.assertIn("MaiBot 数据目录", body)
        self.assertIn("TXT 学习", body)
        self.assertIn("双库冲突", body)
        self.assertIn("记忆看板", body)
        self.assertIn("自学习引擎", body)


if __name__ == "__main__":
    unittest.main()
