# -*- coding: utf-8 -*-
"""
M4.4 归档测试（上下文 API 已由 M4.6 删除；保留遗留 OR 构建纯函数）。

页眉：真实第三方 embeddings 未验证；CURRENT 数值槽仍 null。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
WEB_PKG = ROOT / "web" / "memory_correction"


class TestMemoryRagM44(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["MEMORY_CORRECTION_EMBEDDING_DIM"] = "1536"
        os.environ.pop("MEMORY_CORRECTION_DB", None)
        os.environ.pop("MEMORY_CORRECTION_CONTEXT_JSON", None)
        os.environ.pop("MEMORY_CORRECTION_CONTEXT_XLSX", None)

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

    def test_system_data_context_routes_removed(self):
        r = self.client.get("/api/system_data_context", headers=self.hdr)
        self.assertEqual(r.status_code, 404)
        r2 = self.client.post(
            "/api/system_data_context/load_placeholder", headers=self.hdr
        )
        self.assertEqual(r2.status_code, 404)

    def test_legacy_or_builder_still_available(self):
        from web.memory_correction.system_context import build_or_binding_payload

        payload = build_or_binding_payload(
            term="sample_term_alpha",
            columns=["c_flag", "c_attr"],
            values=["yes", "no"],
        )
        self.assertEqual(payload["op"], "OR")
        self.assertEqual(len(payload["predicates"]), 4)

    def test_index_has_no_context_panel(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("系统数据上下文", r.text)
        self.assertNotIn('id="kv-rows"', r.text)


if __name__ == "__main__":
    unittest.main()
