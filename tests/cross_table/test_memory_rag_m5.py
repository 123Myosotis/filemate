# -*- coding: utf-8 -*-
"""
M5 unit tests (no real third-party required).

页眉：CURRENT 数值槽仍 null；真实第三方验证见 acceptance 冒烟。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


class TestMemoryRagM5(unittest.TestCase):
    def test_settings_share_chat_stack_and_dim_map(self):
        from pipeline.block_cross_table.memory_rag import external_settings as es

        with mock.patch.dict(os.environ, {}, clear=False):
            for k in (
                es.ENV_MODEL_ID,
                es.ENV_EMBEDDING_DIM,
                es.ENV_BASE_URL,
                es.ENV_API_KEY,
            ):
                os.environ.pop(k, None)
            # Shared default is chat model (not a separate embedding-* default).
            mid = es.resolve_model_id()
            self.assertTrue(mid)
            self.assertFalse(
                mid.startswith("embedding-"),
                msg="must not invent separate embedding model by default",
            )
            # Explicit embedding models still map dims (for later split).
            os.environ[es.ENV_MODEL_ID] = "embedding-3"
            self.assertEqual(es.resolve_embedding_dim(), 2048)
            os.environ[es.ENV_MODEL_ID] = "embedding-2"
            self.assertEqual(es.resolve_embedding_dim(), 1024)
            os.environ[es.ENV_EMBEDDING_DIM] = "512"
            self.assertEqual(es.resolve_embedding_dim(), 512)

    def test_chat_model_refuses_embeddings_path(self):
        from pipeline.block_cross_table.memory_rag import external_settings as es
        from pipeline.block_cross_table.memory_rag.errors import MemoryRagError

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(es.ENV_MODEL_ID, None)
            os.environ.pop(es.ENV_EMBEDDING_DIM, None)
            os.environ[es.ENV_API_KEY] = "x"
            # Force shared chat model id.
            os.environ[es.ENV_MODEL_ID] = "glm-4.5-flash"
            with self.assertRaises(MemoryRagError) as ctx:
                es.require_embedding_capable_model()
            self.assertEqual(
                ctx.exception.subreason, "model_not_embedding_capable"
            )

    def test_api_key_only_from_env(self):
        from pipeline.block_cross_table.memory_rag import external_settings as es
        from pipeline.block_cross_table.memory_rag.errors import MemoryRagError

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(es.ENV_API_KEY, None)
            with self.assertRaises(MemoryRagError) as ctx:
                es.require_api_key()
            self.assertEqual(ctx.exception.fail_locus, "memory_external_call_failed")

    def test_rebuild_1536_to_2048(self):
        from pipeline.block_cross_table.memory_rag.bootstrap import open_memory_db
        from pipeline.block_cross_table.memory_rag.vec_index import (
            rebuild_vec0_to_dim,
            stored_embedding_dim,
        )

        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            conn = open_memory_db(path, embedding_dim=1536, enable_vec=True)
            self.assertEqual(stored_embedding_dim(conn), 1536)
            log = rebuild_vec0_to_dim(conn, new_embedding_dim=2048)
            self.assertEqual(log["old_embedding_dim"], 1536)
            self.assertEqual(log["new_embedding_dim"], 2048)
            self.assertEqual(stored_embedding_dim(conn), 2048)
            conn.close()
        finally:
            Path(path).unlink(missing_ok=True)
            Path(path + "-wal").unlink(missing_ok=True)
            Path(path + "-shm").unlink(missing_ok=True)

    def test_runtime_cases_file_exists(self):
        p = ROOT / "acceptance" / "cross_table" / "m5_runtime_cases.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertIn("case1_binding", data)
        self.assertIn("case2_synonym_query", data)
        for rel in data["tables"]:
            self.assertTrue((ROOT / rel).is_file(), msg=rel)


if __name__ == "__main__":
    unittest.main()
