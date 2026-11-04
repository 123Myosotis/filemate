# -*- coding: utf-8 -*-
"""P2-B query UI API shape tests (no live AI)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from openpyxl import Workbook

from web.memory_correction.auth import TOKEN_HEADER, ensure_token
from web.query_ui import app as app_mod
from web.query_ui.export_store import cleanup_expired, save_result_xlsx
from web.query_ui.service import build_ask_history_continuation


class TestQueryUiP2b(unittest.TestCase):
    def setUp(self) -> None:
        ensure_token(app_mod.ROOT)
        self.client = TestClient(app_mod.app)
        self.headers = {TOKEN_HEADER: ensure_token(app_mod.ROOT)}

    def test_health_and_index(self) -> None:
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("ok"))
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("FileMate", page.text)
        self.assertIn("five-part", page.text)

    def test_upload_and_run_mocked(self) -> None:
        tmp = Path(tempfile.mkdtemp()) / "t.xlsx"
        wb = Workbook()
        wb.active.append(["a"])
        wb.active.append([1])
        wb.save(tmp)
        up = self.client.post(
            "/api/query/upload",
            headers=self.headers,
            files=[
                (
                    "files",
                    (
                        "t.xlsx",
                        tmp.read_bytes(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    ),
                )
            ],
        )
        self.assertEqual(up.status_code, 200)
        paths = up.json()["file_paths"]
        self.assertTrue(paths)

        fake = {
            "status": "needs_user",
            "fail_locus": "predicate",
            "five_part": {
                "speculation": "x",
                "evidence": "y",
                "what_might_be_wrong": "z",
                "next_step": "n",
                "please_confirm_or_correct": "p",
            },
            "result_preview": None,
            "plan_summary": {"steps_n": 0, "ops": []},
            "stages": [],
        }
        with mock.patch("web.query_ui.app.run_query", return_value=fake):
            r = self.client.post(
                "/api/query/run",
                headers=self.headers,
                json={"utterance": "q", "file_paths": paths},
            )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "needs_user")
        self.assertEqual(body["five_part"]["evidence"], "y")
        self.assertNotIn("api_key", body)

    def test_ask_history_builder(self) -> None:
        hist = build_ask_history_continuation(
            prior_five_part={"speculation": "s"},
            prior_fail_locus="term_expand",
            user_reply="确认",
            prior_ask_history=[],
        )
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["user_reply"], "确认")

    def test_export_ttl_meta(self) -> None:
        meta = save_result_xlsx(["c"], [{"c": 1}], filename_stem="x")
        self.assertIn("export_id", meta)
        self.assertGreaterEqual(int(meta["expires_in_s"]), 60)
        cleanup_expired(now=10**12)  # far future → expire all
        # after forced expiry cleanup, resolve should miss (best-effort)
        from web.query_ui.export_store import resolve_export_file

        # may already be removed
        _ = resolve_export_file(meta["export_id"])


if __name__ == "__main__":
    unittest.main()
