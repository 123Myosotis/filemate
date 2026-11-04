# -*- coding: utf-8 -*-
"""M8: purge local memory + sparse-only hybrid labels. No live third-party."""
from __future__ import annotations

import unittest

from pipeline.block_cross_table.memory_rag.bootstrap import open_memory_db
from pipeline.block_cross_table.memory_rag.constants import (
    SETTING_MAIBOT_DATA_DIR,
    TABLE_FILEMATE_JARGONS,
    TABLE_SETTINGS_KV,
)
from pipeline.block_cross_table.memory_rag.hybrid_recall import hybrid_recall
from pipeline.block_cross_table.memory_rag.jargon_store import upsert_filemate_jargon
from pipeline.block_cross_table.memory_rag.purge_local import purge_filemate_local_memory
from pipeline.block_cross_table.memory_rag.settings_kv import (
    get_setting,
    set_setting,
)
from pipeline.block_cross_table.memory_rag.store import insert_binding_confirmed
from tests.cross_table.memory_rag_test_helpers import slots_for_tests
from tests.cross_table.test_memory_rag_m5_cpu_merge import _settings
from web.memory_correction.service import (
    list_proposals,
    propose_memory_edit,
    purge_local_test_data,
)


class TestMemoryRagM8PurgeAndSparse(unittest.TestCase):
    def setUp(self):
        self.dim = int(slots_for_tests()["embedding_dim"])
        self.conn = open_memory_db(embedding_dim=self.dim, enable_vec=True)

    def tearDown(self):
        self.conn.close()

    def test_purge_clears_proposals_keeps_settings(self):
        set_setting(self.conn, SETTING_MAIBOT_DATA_DIR, "kept-dir-marker")
        propose_memory_edit(
            self.conn,
            utterance_span_fingerprint="fp-purge",
            binding_payload={"term": "short-residue", "description": "test residue"},
        )
        upsert_filemate_jargon(
            self.conn, content="short-residue", meaning="m", count=1
        )
        self.assertGreaterEqual(len(list_proposals(self.conn)), 1)
        out = purge_local_test_data(self.conn)
        self.assertTrue(out["ok"])
        self.assertTrue(out["maibot_untouched"])
        self.assertEqual(out["pending_proposals_remaining"], 0)
        self.assertEqual(len(list_proposals(self.conn)), 0)
        jn = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM {TABLE_FILEMATE_JARGONS}"
            ).fetchone()[0]
        )
        self.assertEqual(jn, 0)
        self.assertEqual(
            get_setting(self.conn, SETTING_MAIBOT_DATA_DIR), "kept-dir-marker"
        )
        sk = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM {TABLE_SETTINGS_KV}"
            ).fetchone()[0]
        )
        self.assertGreaterEqual(sk, 1)

    def test_hybrid_sparse_only_labeled_without_vector(self):
        insert_binding_confirmed(
            self.conn,
            user_confirmed=True,
            utterance_span_fingerprint="fp-s",
            binding_payload={
                "term": "星云网关",
                "description": "d",
                "values": [],
                "scope": "",
            },
            source_trace_id="t-s",
            confirmed_at="2026-10-21T00:00:00+00:00",
            scope={},
        )
        upsert_filemate_jargon(
            self.conn, content="星云网关", meaning="网关组件", count=4
        )
        result = hybrid_recall(
            self.conn,
            query="星云网关",
            query_embedding=None,
            settings=_settings(maibot_data_dir="", embedded_model_path=""),
            auto_local_embed=False,
        )
        self.assertTrue(result["sparse_only"])
        self.assertEqual(result["retrieval_mode"], "sparse_only")
        self.assertFalse(result["vector_branch_used"])
        self.assertIn("无向量", result["note"])
        self.assertGreaterEqual(len(result["accepted"]), 1)

    def test_purge_filemate_api_shape(self):
        out = purge_filemate_local_memory(self.conn)
        self.assertEqual(out["scope"], "filemate_local_only")


if __name__ == "__main__":
    unittest.main()
