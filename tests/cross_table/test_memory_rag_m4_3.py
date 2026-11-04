# -*- coding: utf-8 -*-
"""
M4.3 Web 交互层重构。

页眉：消灭手填 ID；列表+按钮；横向分栏；真实第三方 embeddings 未验证；
CURRENT 数值槽仍 null。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
WEB_PKG = ROOT / "web" / "memory_correction"


class TestMemoryRagM43(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["MEMORY_CORRECTION_EMBEDDING_DIM"] = "1536"
        os.environ.pop("MEMORY_CORRECTION_DB", None)

    def setUp(self):
        token_path = WEB_PKG / ".token"
        if token_path.exists():
            token_path.unlink()
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self._db_path = Path(db_path)
        os.environ["MEMORY_CORRECTION_DB"] = str(self._db_path)
        import sys

        for name in list(sys.modules):
            if name.startswith("web.memory_correction"):
                del sys.modules[name]
        from web.memory_correction import app as app_mod
        from web.memory_correction.auth import ensure_token

        self.token = ensure_token(WEB_PKG)
        self.app_mod = app_mod
        self._client_cm = TestClient(app_mod.app)
        self.client = self._client_cm.__enter__()
        self.hdr = {"X-Memory-Token": self.token}

    def tearDown(self):
        try:
            self._client_cm.__exit__(None, None, None)
        except Exception:
            pass
        try:
            self._db_path.unlink(missing_ok=True)
            Path(str(self._db_path) + "-wal").unlink(missing_ok=True)
            Path(str(self._db_path) + "-shm").unlink(missing_ok=True)
        except Exception:
            pass
        os.environ.pop("MEMORY_CORRECTION_DB", None)

    def test_api_bindings_returns_list(self):
        pr = self.client.post(
            "/api/propose_memory_edit",
            data={
                "utterance_span_fingerprint": "fp_list",
                "binding_payload_text": json.dumps({"a": 1}),
            },
            headers=self.hdr,
        ).json()
        self.client.post(
            "/api/confirm_memory_edit",
            data={"proposal_id": pr["proposal_id"]},
            headers=self.hdr,
        )
        r = self.client.get("/api/bindings", headers=self.hdr)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("bindings", body)
        self.assertIsInstance(body["bindings"], list)
        self.assertGreaterEqual(len(body["bindings"]), 1)
        r2 = self.client.get("/api/bindings?status=active", headers=self.hdr)
        self.assertEqual(r2.status_code, 200)
        for b in r2.json()["bindings"]:
            self.assertEqual(b["lifecycle_status"], "active")

    def test_api_traces_returns_list(self):
        pr = self.client.post(
            "/api/propose_memory_edit",
            data={
                "utterance_span_fingerprint": "fp_tr",
                "binding_payload_text": json.dumps({"a": 2}),
            },
            headers=self.hdr,
        ).json()
        self.client.post(
            "/api/confirm_memory_edit",
            data={"proposal_id": pr["proposal_id"]},
            headers=self.hdr,
        )
        r = self.client.get("/api/traces", headers=self.hdr)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("traces", body)
        self.assertIsInstance(body["traces"], list)
        self.assertGreaterEqual(len(body["traces"]), 1)
        self.assertTrue(body["traces"][0].get("trace_id"))
        self.assertTrue(body["traces"][0].get("kind"))

    def test_api_proposals_and_discard(self):
        pr = self.client.post(
            "/api/propose_memory_edit",
            data={
                "utterance_span_fingerprint": "fp_disc",
                "binding_payload_text": json.dumps({"a": 3}),
            },
            headers=self.hdr,
        ).json()
        r = self.client.get("/api/proposals?status=proposed", headers=self.hdr)
        self.assertEqual(r.status_code, 200)
        ids = [p["proposal_id"] for p in r.json()["proposals"]]
        self.assertIn(pr["proposal_id"], ids)
        d = self.client.post(
            "/api/discard_proposal",
            data={"proposal_id": pr["proposal_id"]},
            headers=self.hdr,
        )
        self.assertEqual(d.status_code, 200)
        self.assertTrue(d.json()["binding_count_unchanged"])
        r2 = self.client.get("/api/proposals?status=proposed", headers=self.hdr)
        ids2 = [p["proposal_id"] for p in r2.json()["proposals"]]
        self.assertNotIn(pr["proposal_id"], ids2)

    def test_index_has_no_id_inputs(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertIsNone(
            re.search(r'<input[^>]*name=["\']proposal_id["\']', html, re.I)
        )
        self.assertIsNone(
            re.search(r'<input[^>]*name=["\']trace_id["\']', html, re.I)
        )
        # Still must not require hand-typed binding_id input for revoke
        self.assertIsNone(
            re.search(
                r'<input(?![^>]*type=["\']hidden["\'])[^>]*name=["\']binding_id["\']',
                html,
                re.I,
            )
        )

    def test_index_has_grid_layout(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertIn('id="main-grid"', html)
        self.assertIn("layout-grid", html)
        css = (WEB_PKG / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns", css)
        self.assertIn(".layout-grid", css)
        self.assertIn("col-propose", html)
        self.assertIn("col-proposals", html)
        self.assertIn("col-bindings", html)
        self.assertIn("col-traces", html)


if __name__ == "__main__":
    unittest.main()
