# -*- coding: utf-8 -*-
"""
M4.5 泛化知识录入（废除强制列绑定）。

页眉：本地只存结构化文本；不猜物理列；真实第三方 embeddings 未验证；
CURRENT 数值槽仍 null。用例业务原话仅作运行时输入，不写入断言常量依赖。
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
WEB_PKG = ROOT / "web" / "memory_correction"


class TestMemoryRagM45(unittest.TestCase):
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
        from web.memory_correction import system_context as sc

        sc._SESSION = None
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

    def test_wizard_has_no_col_binding(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertNotIn('id="wizard-col-select"', html)
        self.assertNotIn("wizard-col-add", html)
        self.assertNotIn("添加列", html)
        self.assertNotIn("目标列", html)
        self.assertNotIn('data-add-col=', html)
        self.assertIn("业务术语", html)
        self.assertIn("术语描述", html)
        self.assertIn("适用范围", html)
        self.assertIn("由 AI 自动判断如何应用", html)

    def test_propose_generates_general_payload(self):
        # Generic runtime inputs only — no product logic depending on domain words.
        r = self.client.post(
            "/api/propose_wizard",
            data={
                "term": "sample_term_gamma",
                "description": "alpha, beta, gamma",
                "scope_text": "sample_scene",
            },
            headers=self.hdr,
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body.get("binding_count_unchanged"))
        props = self.client.get(
            "/api/proposals?status=proposed", headers=self.hdr
        ).json()["proposals"]
        match = [p for p in props if p["proposal_id"] == body["proposal_id"]][0]
        payload = match["payload"]["binding_payload"]
        self.assertEqual(payload.get("term"), "sample_term_gamma")
        self.assertEqual(payload.get("description"), "alpha, beta, gamma")
        self.assertEqual(payload.get("scope"), "sample_scene")
        self.assertIn("values", payload)
        self.assertIsInstance(payload["values"], list)
        # Must not force physical column binding fields
        self.assertNotIn("predicates", payload)
        self.assertNotIn("op", payload)
        self.assertFalse(any("col" in (p if isinstance(p, dict) else {}) for p in payload.get("values", []) if isinstance(p, dict)))
        blob = str(payload)
        for key in ("predicates", '"col"', "'col'"):
            self.assertNotIn(key if key != '"col"' else "predicates", blob if key == "predicates" else "")
        self.assertNotIn('"col"', blob)
        self.assertNotIn("'col'", blob)

    def test_general_propose_no_write_until_confirm(self):
        from web.memory_correction.service import binding_count

        before = binding_count(self.app_mod._CONN)
        self.client.post(
            "/api/propose_wizard",
            data={
                "term": "sample_term_delta",
                "description": "desc only",
                "scope_text": "",
            },
            headers=self.hdr,
        )
        self.assertEqual(binding_count(self.app_mod._CONN), before)


if __name__ == "__main__":
    unittest.main()
