# -*- coding: utf-8 -*-
"""M8.1: shared-conn thread safety + purge then propose/discard. No live API."""
from __future__ import annotations

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from fastapi.testclient import TestClient

from pipeline.block_cross_table.memory_rag.bootstrap import open_memory_db
from pipeline.block_cross_table.memory_rag.purge_local import purge_filemate_local_memory
from tests.cross_table.memory_rag_test_helpers import slots_for_tests
from web.memory_correction.service import (
    discard_proposal,
    propose_memory_edit,
    purge_local_test_data,
)


class TestMemoryRagM81ThreadSafety(unittest.TestCase):
    def setUp(self):
        self.dim = int(slots_for_tests()["embedding_dim"])
        self._td = tempfile.TemporaryDirectory(prefix="m81_")
        self.db = Path(self._td.name) / "mem.sqlite"
        os.environ["MEMORY_CORRECTION_DB"] = str(self.db)
        os.environ["MEMORY_CORRECTION_EMBEDDING_DIM"] = str(self.dim)
        import importlib
        import web.memory_correction.app as app_mod

        importlib.reload(app_mod)
        self.app_mod = app_mod
        self.client = TestClient(app_mod.app)
        self.client.__enter__()
        self.hdr = {"X-Memory-Token": app_mod.ensure_token(app_mod.ROOT)}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self._td.cleanup()

    def test_concurrent_propose_and_dashboard(self):
        errors: list[str] = []

        def one(i: int) -> str:
            try:
                if i % 2 == 0:
                    r = self.client.post(
                        "/api/propose_wizard",
                        headers=self.hdr,
                        data={
                            "term": f"term{i}",
                            "description": f"desc{i}",
                            "scope_text": "",
                        },
                    )
                else:
                    r = self.client.get("/api/dashboard", headers=self.hdr)
                if r.status_code >= 500:
                    return f"HTTP{r.status_code}"
                return "ok"
            except Exception as exc:  # noqa: BLE001
                return f"EXC:{type(exc).__name__}"

        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = [pool.submit(one, i) for i in range(40)]
            for f in as_completed(futs):
                res = f.result()
                if res != "ok":
                    errors.append(res)
        self.assertEqual(errors, [], msg=str(errors[:10]))

    def test_purge_then_propose_discard(self):
        r = self.client.post(
            "/api/propose_wizard",
            headers=self.hdr,
            data={"term": "before", "description": "d", "scope_text": ""},
        )
        self.assertEqual(r.status_code, 200)
        p = self.client.post(
            "/api/admin/purge_local_memory",
            headers=self.hdr,
            data={"confirm": "PURGE"},
        )
        self.assertEqual(p.status_code, 200)
        r2 = self.client.post(
            "/api/propose_wizard",
            headers=self.hdr,
            data={"term": "after", "description": "d2", "scope_text": ""},
        )
        self.assertEqual(r2.status_code, 200)
        pid = r2.json()["proposal_id"]
        d = self.client.post(
            "/api/discard_proposal",
            headers=self.hdr,
            data={"proposal_id": pid},
        )
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.json()["status"], "discarded")


class TestMemoryRagM81PurgeService(unittest.TestCase):
    def setUp(self):
        self.dim = int(slots_for_tests()["embedding_dim"])
        self.conn = open_memory_db(embedding_dim=self.dim, enable_vec=True)

    def tearDown(self):
        self.conn.close()

    def test_purge_keeps_writable(self):
        propose_memory_edit(
            self.conn,
            utterance_span_fingerprint="x",
            binding_payload={"term": "a", "description": "b"},
        )
        out = purge_local_test_data(self.conn)
        self.assertTrue(out["ok"])
        r = propose_memory_edit(
            self.conn,
            utterance_span_fingerprint="y",
            binding_payload={"term": "c", "description": "d"},
        )
        discard_proposal(self.conn, proposal_id=r["proposal_id"])
        purge_filemate_local_memory(self.conn)


if __name__ == "__main__":
    unittest.main()
