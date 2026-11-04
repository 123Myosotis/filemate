# -*- coding: utf-8 -*-
"""
M5 pipeline unit tests with injectable HTTP transport (no real third-party).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def _fake_embedding(dim: int, seed: float = 0.01) -> list[float]:
    return [float(seed) + (i % 7) * 0.0001 for i in range(int(dim))]


class TestMemoryRagM5Pipeline(unittest.TestCase):
    def setUp(self):
        self.dim = 2048
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.db_path = path
        os.environ["ZHIPU_API_KEY"] = "test-only-not-real"
        os.environ["MEMORY_RAG_EMBEDDING_MODEL_ID"] = "embedding-3"
        os.environ["MEMORY_RAG_EMBEDDING_DIM"] = str(self.dim)
        from pipeline.block_cross_table.memory_rag.bootstrap import open_memory_db
        from pipeline.block_cross_table.memory_rag.backend_switch import set_backend_switch
        from pipeline.block_cross_table.memory_rag.vec_index import rebuild_vec0_to_dim

        self.conn = open_memory_db(path, embedding_dim=1536, enable_vec=True)
        self.rebuild = rebuild_vec0_to_dim(self.conn, new_embedding_dim=self.dim)
        set_backend_switch(
            self.conn,
            backend="external_api_openai_compatible",
            chosen_at=datetime.now(timezone.utc).isoformat(),
            chosen_source_trace_id="m5_unit",
        )

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        for p in (self.db_path, self.db_path + "-wal", self.db_path + "-shm"):
            Path(p).unlink(missing_ok=True)
        os.environ.pop("ZHIPU_API_KEY", None)

    def _transport(self, dim=None, seed=0.01):
        d = int(dim or self.dim)

        def transport(url, headers, body, timeout_s):
            req = json.loads(body.decode("utf-8"))
            self.assertEqual(set(req.keys()), {"model", "input"})
            self.assertIn("Authorization", headers)
            payload = {
                "model": req["model"],
                "object": "list",
                "data": [{"embedding": _fake_embedding(d, seed=seed), "index": 0}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            }
            return 200, json.dumps(payload).encode("utf-8")

        return transport

    def test_rebuild_log_and_write_knn_conflict(self):
        from pipeline.block_cross_table.memory_rag.conflict import (
            detect_multi_binding_conflict,
        )
        from pipeline.block_cross_table.memory_rag.external_settings import (
            load_external_embedding_settings,
        )
        from pipeline.block_cross_table.memory_rag.meta import EmbeddingVectorMeta
        from pipeline.block_cross_table.memory_rag.query_embed import embed_query_text
        from pipeline.block_cross_table.memory_rag.recall import (
            recall_bindings,
            recall_by_knn,
        )
        from pipeline.block_cross_table.memory_rag.write_with_embed import (
            write_binding_with_external_embed,
        )

        self.assertEqual(self.rebuild["old_embedding_dim"], 1536)
        self.assertEqual(self.rebuild["new_embedding_dim"], 2048)
        settings = load_external_embedding_settings()
        payload = {
            "term": "sample_term_alpha",
            "description": "alpha desc",
            "values": [],
            "scope": "unit",
        }
        w1 = write_binding_with_external_embed(
            self.conn,
            utterance_span_fingerprint="fp:sample",
            binding_payload=payload,
            scope={"source": "unit"},
            source_trace_id="u1",
            settings=settings,
            http_transport=self._transport(seed=0.01),
        )
        self.assertEqual(w1["embedding_dim"], 2048)
        self.assertIn("model_id", w1["meta"])

        q_emb, q_meta, _ = embed_query_text(
            self.conn,
            text="sample query close",
            settings=settings,
            http_transport=self._transport(seed=0.011),
        )
        knn = recall_by_knn(
            self.conn,
            query_embedding=q_emb,
            top_k=10,
            query_meta=q_meta,
        )
        self.assertTrue(any(a["binding_id"] == w1["binding_id"] for a in knn["accepted"]))

        w2 = write_binding_with_external_embed(
            self.conn,
            utterance_span_fingerprint="fp:sample",
            binding_payload={
                "term": "sample_term_alpha",
                "description": "wrong mapping",
                "values": [],
                "scope": "conflict",
            },
            scope={"source": "unit_conflict"},
            source_trace_id="u2",
            settings=settings,
            http_transport=self._transport(seed=0.02),
        )
        mech = recall_bindings(
            self.conn,
            top_k=10,
            query_meta=EmbeddingVectorMeta.from_mapping(w2["meta"]),
        )
        conflict = detect_multi_binding_conflict(
            mech["accepted"], source_trace_id="u_conflict"
        )
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict["package"]["conflict_kind"], "multi_binding")
        self.assertTrue(conflict["e_route"])


if __name__ == "__main__":
    unittest.main()
