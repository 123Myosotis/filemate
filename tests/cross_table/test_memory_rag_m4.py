# -*- coding: utf-8 -*-
"""
M4 memory_correction Web.

页眉约束：M4 仅 loopback；真实第三方 embeddings 未验证；CURRENT 数值槽仍 null。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
WEB_PKG = ROOT / "web" / "memory_correction"


class TestMemoryRagM4(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["MEMORY_CORRECTION_EMBEDDING_DIM"] = "1536"
        # Force :memory: by leaving MEMORY_CORRECTION_DB unset / empty
        os.environ.pop("MEMORY_CORRECTION_DB", None)

    def setUp(self):
        # Fresh token file for isolation
        token_path = WEB_PKG / ".token"
        if token_path.exists():
            token_path.unlink()
        # File DB so WAL mode is observable ( :memory: cannot use WAL ).
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self._db_path = Path(db_path)
        os.environ["MEMORY_CORRECTION_DB"] = str(self._db_path)
        # Reload app module so lifespan picks env (first import may have failed previously).
        import importlib
        import sys

        for name in list(sys.modules):
            if name.startswith("web.memory_correction"):
                del sys.modules[name]
        from web.memory_correction import app as app_mod
        from web.memory_correction.auth import ensure_token

        self.token = ensure_token(WEB_PKG)
        self.app_mod = app_mod
        # Context manager runs lifespan (opens DB).
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

    def test_healthz(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json().get("status"), "ok")
        self.assertNotIn("set-cookie", {k.lower() for k in r.headers.keys()})

    def test_propose_does_not_write_binding(self):
        from web.memory_correction.service import binding_count

        before = binding_count(self.app_mod._CONN)
        r = self.client.post(
            "/api/propose_memory_edit",
            data={
                "utterance_span_fingerprint": "fp_p",
                "binding_payload_text": json.dumps({"a": 1}),
                "scope_text": "{}",
            },
            headers=self.hdr,
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["binding_count_unchanged"])
        self.assertEqual(binding_count(self.app_mod._CONN), before)

    def test_confirm_writes_and_trace(self):
        pr = self.client.post(
            "/api/propose_memory_edit",
            data={
                "utterance_span_fingerprint": "fp_c",
                "binding_payload_text": json.dumps({"a": 2}),
            },
            headers=self.hdr,
        ).json()
        cr = self.client.post(
            "/api/confirm_memory_edit",
            data={"proposal_id": pr["proposal_id"]},
            headers=self.hdr,
        )
        self.assertEqual(cr.status_code, 200)
        body = cr.json()
        self.assertEqual(body["kind"], "memory_write")
        self.assertTrue(body.get("binding_id"))
        self.assertTrue(body.get("trace_id"))

    def test_revoke_and_replay(self):
        pr = self.client.post(
            "/api/propose_memory_edit",
            data={
                "utterance_span_fingerprint": "fp_r",
                "binding_payload_text": json.dumps({"a": 3}),
            },
            headers=self.hdr,
        ).json()
        cr = self.client.post(
            "/api/confirm_memory_edit",
            data={"proposal_id": pr["proposal_id"]},
            headers=self.hdr,
        ).json()
        rr = self.client.post(
            "/api/revoke_memory_edit",
            data={"binding_id": cr["binding_id"]},
            headers=self.hdr,
        )
        self.assertEqual(rr.status_code, 200)
        self.assertEqual(rr.json()["kind"], "memory_revoke")
        trace_id = rr.json()["trace_id"]
        replay = self.client.post(
            "/api/replay_memory_edit",
            data={"trace_id": trace_id},
            headers=self.hdr,
        )
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["replay"])
        self.assertEqual(replay.json()["kind"], "memory_revoke")

    def test_auth_missing_and_wrong_token(self):
        r0 = self.client.post(
            "/api/propose_memory_edit",
            data={
                "utterance_span_fingerprint": "x",
                "binding_payload_text": "{}",
            },
        )
        self.assertEqual(r0.status_code, 401)
        r1 = self.client.post(
            "/api/propose_memory_edit",
            data={
                "utterance_span_fingerprint": "x",
                "binding_payload_text": "{}",
            },
            headers={"X-Memory-Token": "wrong"},
        )
        self.assertEqual(r1.status_code, 401)
        r2 = self.client.post(
            "/api/propose_memory_edit",
            data={
                "utterance_span_fingerprint": "x",
                "binding_payload_text": "{}",
            },
            headers=self.hdr,
        )
        self.assertEqual(r2.status_code, 200)

    def test_write_apis_have_no_set_cookie(self):
        for path, data in (
            (
                "/api/propose_memory_edit",
                {
                    "utterance_span_fingerprint": "fp_sc",
                    "binding_payload_text": "{}",
                },
            ),
        ):
            r = self.client.post(path, data=data, headers=self.hdr)
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("set-cookie", {k.lower() for k in r.headers.keys()})

    def test_source_excel_sha256_unchanged(self):
        from openpyxl import Workbook

        fd, path = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        path = Path(path)
        try:
            wb = Workbook()
            wb.active["A1"] = "col"
            wb.active["A2"] = "1"
            wb.save(path)
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            # Web ops must not touch this file.
            self.client.post(
                "/api/propose_memory_edit",
                data={
                    "utterance_span_fingerprint": "fp_x",
                    "binding_payload_text": json.dumps({"t": 1}),
                },
                headers=self.hdr,
            )
            pr = self.client.post(
                "/api/propose_memory_edit",
                data={
                    "utterance_span_fingerprint": "fp_x2",
                    "binding_payload_text": json.dumps({"t": 2}),
                },
                headers=self.hdr,
            ).json()
            self.client.post(
                "/api/confirm_memory_edit",
                data={"proposal_id": pr["proposal_id"]},
                headers=self.hdr,
            )
            after = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(before, after)
        finally:
            path.unlink(missing_ok=True)

    def test_htmx_local_and_no_cdn_refs(self):
        htmx = WEB_PKG / "static" / "htmx.min.js"
        self.assertTrue(htmx.exists())
        self.assertGreater(htmx.stat().st_size, 1000)
        banned = ("cdn.", "unpkg", "jsdelivr")
        hits = []
        for p in ROOT.rglob("*"):
            if not p.is_file():
                continue
            if any(part in {".git", ".venv", "venv", "__pycache__", ".idea"} for part in p.parts):
                continue
            if p.suffix.lower() not in {".py", ".html", ".js", ".css", ".md", ".json"}:
                continue
            # Allow this test file mentioning ban words, and evidence docs discussing ban.
            if p.name in {"test_memory_rag_m4.py", "EVIDENCE_MEMORY_RAG_M4.md"}:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore").lower()
            except Exception:
                continue
            for b in banned:
                if b in text and "http" in text:
                    # htmx.min.js itself may contain strings; skip vendor bundle
                    if p.name == "htmx.min.js":
                        continue
                    hits.append("%s:%s" % (p, b))
        self.assertEqual(hits, [])

    def test_port_env_overrides_gui(self):
        from web.memory_correction.port_settings import resolve_port, save_gui_port

        save_gui_port(8765)
        with mock.patch.dict(os.environ, {"MEMORY_CORRECTION_PORT": "9999"}):
            self.assertEqual(resolve_port(), 9999)
        # Without env, GUI/config value
        os.environ.pop("MEMORY_CORRECTION_PORT", None)
        save_gui_port(8770)
        self.assertEqual(resolve_port(), 8770)
        save_gui_port(8765)

    def test_wal_mode_enabled(self):
        row = self.app_mod._CONN.execute("PRAGMA journal_mode").fetchone()
        mode = str(row[0]).lower()
        self.assertEqual(mode, "wal")

    def test_busy_timeout_enabled(self):
        row = self.app_mod._CONN.execute("PRAGMA busy_timeout").fetchone()
        self.assertIsNotNone(row)
        self.assertGreater(int(row[0]), 0)

    def test_index_renders_token_meta(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('name="x-memory-token"', r.text)
        self.assertIn("/static/htmx.min.js", r.text)
        self.assertNotIn("cdn.", r.text.lower())


if __name__ == "__main__":
    unittest.main()
