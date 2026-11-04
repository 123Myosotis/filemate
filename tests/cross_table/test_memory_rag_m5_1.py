# -*- coding: utf-8 -*-
"""
M5.1 patch: AI meaning equivalence, token filters, proposal source_type.
CURRENT stays null. Mock chat injection — no live third-party.
"""
from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from pipeline.block_cross_table.memory_rag.bootstrap import open_memory_db
from pipeline.block_cross_table.memory_rag.constants import (
    DEDUP_CONFLICT,
    DEDUP_SILENT_SKIP,
    SOURCE_MAIBOT_CONFLICT,
    SOURCE_TXT_MINING,
    SOURCE_WIZARD,
)
from pipeline.block_cross_table.memory_rag.jargon_dedup import (
    apply_dedup_after_inference,
    classify_against_maibot,
)
from pipeline.block_cross_table.memory_rag.meaning_equivalence import (
    METHOD_AI,
    METHOD_FALLBACK,
    meanings_equivalent_smart,
)
from pipeline.block_cross_table.memory_rag.merge_settings import (
    MergeRuntimeSettings,
    load_merge_runtime_settings,
)
from pipeline.block_cross_table.memory_rag.text_tokenize import (
    accept_token,
    count_terms,
    is_pure_punctuation,
)
from tests.cross_table.memory_rag_test_helpers import slots_for_tests
from tests.cross_table.test_memory_rag_m5_cpu_merge import _make_maibot_db, _settings


class TestMemoryRagM51(unittest.TestCase):
    def setUp(self):
        self.dim = int(slots_for_tests()["embedding_dim"])
        self.conn = open_memory_db(embedding_dim=self.dim, enable_vec=True)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self.conn.close()
        self._tmpdir.cleanup()

    def test_ai_equivalence_path(self):
        calls = {"n": 0}

        def fake(prompt: str, **_k):
            calls["n"] += 1
            self.assertIn("句子 A:", prompt)
            self.assertIn("句子 B:", prompt)
            return {"is_equivalent": True}

        v = meanings_equivalent_smart(
            "甲含义说明",
            "乙措辞不同但同义",
            fallback_threshold=0.6,
            chat_json_fn=fake,
        )
        self.assertTrue(v.equivalent)
        self.assertEqual(v.method, METHOD_AI)
        self.assertEqual(calls["n"], 1)

    def test_fallback_when_ai_unavailable(self):
        def boom(_prompt, **_k):
            raise RuntimeError("no network")

        # Near-identical → silent via tolerant 0.6 fallback
        v = meanings_equivalent_smart(
            "苹果手机",
            "苹果 手机",
            fallback_threshold=0.6,
            chat_json_fn=boom,
        )
        self.assertTrue(v.equivalent)
        self.assertEqual(v.method, METHOD_FALLBACK)

        # Far meanings → conflict via fallback
        v2 = meanings_equivalent_smart(
            "完全一套说法AAA",
            "全然无关BBBXYZ",
            fallback_threshold=0.6,
            chat_json_fn=lambda *_a, **_k: None,
        )
        self.assertFalse(v2.equivalent)
        self.assertEqual(v2.method, METHOD_FALLBACK)

    def test_classify_uses_ai_mock(self):
        rows = [{"meaning": "MaiBot解释"}]
        disp, m, _, meta = classify_against_maibot(
            content="词",
            meaning="本地解释不同",
            maibot_rows=rows,
            fallback_threshold=0.6,
            chat_json_fn=lambda *_a, **_k: {"is_equivalent": False},
        )
        self.assertEqual(disp, DEDUP_CONFLICT)
        self.assertEqual(m, "MaiBot解释")
        self.assertIn("equivalence_methods", meta)

        disp2, _, _, meta2 = classify_against_maibot(
            content="词",
            meaning="本地解释不同",
            maibot_rows=rows,
            fallback_threshold=0.6,
            chat_json_fn=lambda *_a, **_k: {"is_equivalent": True},
        )
        self.assertEqual(disp2, DEDUP_SILENT_SKIP)
        self.assertEqual(meta2.get("equivalence_method"), METHOD_AI)

    def test_token_filters_len_punct_stopwords(self):
        self.assertFalse(accept_token("的", min_token_len=2, stopwords=("的", "了")))
        self.assertFalse(accept_token("a", min_token_len=2))
        self.assertTrue(is_pure_punctuation("…"))
        self.assertTrue(is_pure_punctuation("！！"))
        self.assertFalse(accept_token("！！", min_token_len=2))
        self.assertTrue(accept_token("联调", min_token_len=2, stopwords=("的",)))

        counts = count_terms(
            "的 了 是 联调词 联调词！！",
            min_token_len=2,
            stopwords=("的", "了", "是"),
            cjk_n_min=2,
            cjk_n_max=3,
        )
        self.assertNotIn("的", counts)
        self.assertNotIn("了", counts)
        self.assertTrue(any("联调" in k or k == "联调词" for k in counts))

    def test_proposal_source_type_txt_and_conflict(self):
        from web.memory_correction.service import list_proposals, propose_memory_edit

        r = propose_memory_edit(
            self.conn,
            utterance_span_fingerprint="fp",
            binding_payload={"term": "t", "description": "d"},
            source_type=SOURCE_TXT_MINING,
        )
        self.assertEqual(r["source_type"], SOURCE_TXT_MINING)
        props = list_proposals(self.conn, status="proposed")
        self.assertEqual(props[0]["source_type"], SOURCE_TXT_MINING)

        _make_maibot_db(self.tmp / "MaiBot.db", [("冲突源", "旧")])
        d = apply_dedup_after_inference(
            self.conn,
            content="冲突源",
            meaning="新且不同",
            settings=_settings(maibot_data_dir=str(self.tmp)),
            chat_json_fn=lambda *_a, **_k: {"is_equivalent": False},
        )
        self.assertEqual(d.disposition, DEDUP_CONFLICT)
        from pipeline.block_cross_table.memory_rag.jargon_dedup import resolve_conflict

        out = resolve_conflict(
            self.conn,
            conflict_id=d.conflict_id,
            resolution="keep_filemate",
        )
        row = self.conn.execute(
            "SELECT source_type FROM memory_edit_proposal WHERE proposal_id=?",
            (out["proposal_id"],),
        ).fetchone()
        self.assertEqual(row[0], SOURCE_MAIBOT_CONFLICT)

    def test_schema_source_type_default_wizard(self):
        cols = {
            r[1]
            for r in self.conn.execute(
                "PRAGMA table_info(memory_edit_proposal)"
            ).fetchall()
        }
        self.assertIn("source_type", cols)
        from web.memory_correction.service import propose_memory_edit

        r = propose_memory_edit(
            self.conn,
            utterance_span_fingerprint="w",
            binding_payload={"term": "手工", "description": "描述"},
        )
        self.assertEqual(r["source_type"], SOURCE_WIZARD)

    def test_settings_fallback_default(self):
        s = load_merge_runtime_settings()
        self.assertAlmostEqual(s.meaning_fallback_threshold, 0.6, places=5)
        self.assertIn("的", s.jargon_stopwords)

    def test_current_still_null(self):
        from pipeline.block_cross_table.memory_rag.production_slots import (
            load_memory_numeric_slots_from_current,
        )

        slots = load_memory_numeric_slots_from_current()
        self.assertIsNone(slots.get("embedding_dim"))


if __name__ == "__main__":
    unittest.main()
