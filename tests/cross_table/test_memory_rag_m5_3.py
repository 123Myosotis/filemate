# -*- coding: utf-8 -*-
"""
M5.3: MaiBot directory browse UI + pick_directory API (mock dialog).
Does not exercise dual-engine / mining logic.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from tests.cross_table.memory_rag_test_helpers import slots_for_tests
from web.memory_correction.directory_picker import (
    pick_directory,
    validate_maibot_data_dir,
)


class TestDirectoryPickerUnit(unittest.TestCase):
    def test_pick_directory_mock_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp).resolve())

            def fake():
                return target

            result = pick_directory(ask_directory_fn=fake)
            self.assertEqual(result["status"], "ok")
            self.assertFalse(result["cancelled"])
            self.assertEqual(Path(result["path"]), Path(target))

    def test_pick_directory_cancelled(self):
        result = pick_directory(ask_directory_fn=lambda: None)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["cancelled"])
        self.assertIsNone(result["path"])

    def test_validate_missing_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = validate_maibot_data_dir(tmp)
            self.assertFalse(r["valid"])
            self.assertEqual(r["message"], "未找到数据库文件")

    def test_validate_with_db_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "MaiBot.db"
            db.write_bytes(b"")
            r = validate_maibot_data_dir(str(root))
            self.assertTrue(r["valid"])
            self.assertTrue(r["db_found"])
            self.assertEqual(Path(r["db_path"]), db.resolve())


class TestPickDirectoryApi(unittest.TestCase):
    def setUp(self):
        self.dim = int(slots_for_tests()["embedding_dim"])
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = Path(self._tmpdir.name) / "m53.db"
        self.pick_target = Path(self._tmpdir.name) / "maibot_data"
        self.pick_target.mkdir()
        (self.pick_target / "MaiBot.db").write_bytes(b"")
        os.environ["MEMORY_CORRECTION_DB"] = str(self.db)
        os.environ["MEMORY_CORRECTION_EMBEDDING_DIM"] = str(self.dim)
        for name in list(sys.modules):
            if name.startswith("web.memory_correction"):
                del sys.modules[name]
        from fastapi.testclient import TestClient
        from web.memory_correction import app as app_mod
        from web.memory_correction.auth import ensure_token

        self.app_mod = app_mod
        app_mod.set_pick_directory_fn_for_tests(
            lambda: str(self.pick_target.resolve())
        )
        self.token = ensure_token(app_mod.ROOT)
        self.headers = {"X-Memory-Token": self.token}
        self._client_cm = TestClient(app_mod.app)
        self.client = self._client_cm.__enter__()

    def tearDown(self):
        try:
            self.app_mod.set_pick_directory_fn_for_tests(None)
        except Exception:
            pass
        try:
            self._client_cm.__exit__(None, None, None)
        except Exception:
            pass
        os.environ.pop("MEMORY_CORRECTION_DB", None)
        self._tmpdir.cleanup()

    def test_api_pick_directory_autosaves(self):
        r = self.client.get("/api/pick_directory", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body.get("auto_saved"))
        self.assertEqual(
            Path(body["path"]), self.pick_target.resolve()
        )
        self.assertTrue(body["validation"]["valid"])

        s = self.client.get("/api/settings", headers=self.headers)
        self.assertEqual(s.status_code, 200)
        self.assertEqual(
            Path(s.json()["maibot_data_dir"]), self.pick_target.resolve()
        )

    def test_api_validate_endpoint(self):
        empty = Path(self._tmpdir.name) / "empty_no_db"
        empty.mkdir()
        bad = self.client.get(
            "/api/settings/maibot_data_dir/validate",
            headers=self.headers,
            params={"path": str(empty)},
        )
        self.assertEqual(bad.status_code, 200)
        self.assertFalse(bad.json()["valid"])
        self.assertEqual(bad.json()["message"], "未找到数据库文件")

        good = self.client.get(
            "/api/settings/maibot_data_dir/validate",
            headers=self.headers,
            params={"path": str(self.pick_target)},
        )
        self.assertEqual(good.status_code, 200)
        self.assertTrue(good.json()["valid"])

    def test_settings_page_has_browse_button(self):
        html = self.client.get("/")
        self.assertEqual(html.status_code, 200)
        self.assertIn("btn-browse-maibot", html.text)
        self.assertIn("浏览", html.text)
        self.assertIn("请点击右侧按钮选择 MaiBot 的 data 目录", html.text)


if __name__ == "__main__":
    unittest.main()
