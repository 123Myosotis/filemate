# -*- coding: utf-8 -*-
"""
M4.2 UI 汉化。

页眉：不改 API/字段名/鉴权；真实第三方 embeddings 未验证；CURRENT 数值槽仍 null。
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

# Bare English UI labels that must not appear without Chinese wrapping.
_BARE_PATTERNS = [
    r"<title>\s*memory_correction\s*</title>",
    r"<h1>\s*memory_correction\s*</h1>",
    r"<h2>\s*propose\s*</h2>",
    r"<h2>\s*confirm\s*</h2>",
    r"<h2>\s*bindings\s*</h2>",
    r"<h2>\s*replay\s*</h2>",
    r"<th>\s*binding_id\s*</th>",
    r"<th>\s*fingerprint\s*</th>",
    r"<th>\s*status\s*</th>",
    r"<th>\s*payload\s*</th>",
    r"<th>\s*actions\s*</th>",
    r">\s*propose\s*</button>",
    r">\s*confirm\s*</button>",
    r">\s*revoke\s*</button>",
    r">\s*replay\s*</button>",
]


class TestMemoryRagM42(unittest.TestCase):
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

    def test_index_contains_chinese_labels(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.text
        # M4.5：泛化知识录入；保留列表区中文。
        for needle in (
            "提交修改提议",
            "确认前不落库",
            "已生效知识",
            "撤销",
            "按记录回放",
            "记忆纠错",
            "业务术语",
            "术语描述",
            "绑定编号",
            "待确认提议",
            "操作记录",
            "新建知识",
        ):
            self.assertIn(needle, html, msg="missing Chinese label: %s" % needle)
        self.assertNotIn("系统数据上下文", html)
        self.assertIn("<title>记忆纠错</title>", html)

    def test_index_has_no_bare_english_labels(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.text
        for pat in _BARE_PATTERNS:
            self.assertIsNone(
                re.search(pat, html, flags=re.IGNORECASE),
                msg="bare English UI label still present: %s" % pat,
            )
        # English originals may appear only inside Chinese parentheses.
        for eng in (
            "propose",
            "confirm",
            "bindings",
            "revoke",
            "replay",
            "binding_id",
            "proposal_id",
            "trace_id",
            "fingerprint",
            "payload",
            "actions",
            "utterance_span_fingerprint",
            "binding_payload_text",
            "scope_text",
        ):
            # Whole-token match so fingerprint does not hit inside utterance_span_fingerprint.
            pat = r"(?<![A-Za-z0-9_])" + re.escape(eng) + r"(?![A-Za-z0-9_])"
            for m in re.finditer(pat, html, flags=re.IGNORECASE):
                start = max(0, m.start() - 2)
                window = html[start : m.end() + 2]
                ok = ("（" in window[:2] and "）" in html[m.end() : m.end() + 2]) or (
                    "(" in window[:2] and ")" in html[m.end() : m.end() + 2]
                )
                ctx_before = html[max(0, m.start() - 48) : m.start()]
                if any(
                    x in ctx_before
                    for x in (
                        "/api/",
                        'name="',
                        "name='",
                        'id="',
                        "id='",
                        "class=",
                        "data-binding-id",
                        "X-Memory-Token",
                        "token_header",
                        "aria-labelledby",
                    )
                ):
                    continue
                if "hx-" in ctx_before:
                    continue
                self.assertTrue(
                    ok,
                    msg="English term %r not wrapped in parentheses at %r"
                    % (eng, window),
                )


if __name__ == "__main__":
    unittest.main()
