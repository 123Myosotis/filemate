# -*- coding: utf-8 -*-
"""
M4.6 极简知识台 + 列表 GET 容错。

页眉：无系统数据上下文；真实第三方 embeddings 未验证；CURRENT 数值槽仍 null。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
WEB_PKG = ROOT / "web" / "memory_correction"


class TestMemoryRagM46(unittest.TestCase):
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

    def test_bindings_empty_list_ok(self):
        r = self.client.get("/api/bindings", headers=self.hdr)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body.get("bindings"), [])

    def test_bindings_exception_returns_empty(self):
        with mock.patch(
            "web.memory_correction.app.list_bindings",
            side_effect=RuntimeError("boom"),
        ):
            r = self.client.get("/api/bindings", headers=self.hdr)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json().get("bindings"), [])

    def test_proposals_and_traces_empty_ok(self):
        pr = self.client.get("/api/proposals?status=proposed", headers=self.hdr)
        tr = self.client.get("/api/traces", headers=self.hdr)
        self.assertEqual(pr.status_code, 200)
        self.assertEqual(tr.status_code, 200)
        self.assertEqual(pr.json().get("proposals"), [])
        self.assertEqual(tr.json().get("traces"), [])

    def test_context_routes_gone(self):
        for path in (
            "/api/system_data_context",
            "/api/system_data_context/load_placeholder",
        ):
            r = self.client.get(path, headers=self.hdr)
            self.assertEqual(r.status_code, 404, msg=path)

    def test_index_minimal_four_blocks(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertNotIn("系统数据上下文", html)
        self.assertNotIn("system_data_context", html)
        for needle in ("新建知识", "待确认提议", "已生效知识", "操作记录"):
            self.assertIn(needle, html)
        js = (WEB_PKG / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("system_data_context", js)
        self.assertIn("加载失败：", js)
        self.assertIn(".catch", js)

    def test_empty_wizard_rejected(self):
        r = self.client.post(
            "/api/propose_wizard",
            data={"term": "", "description": "", "scope_text": ""},
            headers=self.hdr,
        )
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
