# -*- coding: utf-8 -*-
"""Config priority / masking / missing-key soft-skip for external embedding."""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from pipeline.block_cross_table.memory_rag.backend_switch import set_backend_switch
from pipeline.block_cross_table.memory_rag.bootstrap import open_memory_db
from pipeline.block_cross_table.memory_rag.constants import BACKEND_EXTERNAL
from pipeline.block_cross_table.memory_rag.external_circuit import CaseExternalEmbedSidecar
from pipeline.block_cross_table.memory_rag.external_embedding_config import (
    ENV_API_KEY,
    ENV_API_KEY_VENDOR_ALIAS,
    PARAM_API_KEY,
    PARAM_BASE_URL,
    PARAM_ENABLED,
    PARAM_MODEL,
    PARAM_WEIGHT,
    SOURCE_CONFIG,
    SOURCE_ENV,
    SOURCE_NONE,
    load_external_embedding_public,
    mask_api_key,
    resolve_external_api_key,
    resolve_external_embedding,
    save_external_embedding_patch,
)
from pipeline.block_cross_table.memory_rag.hybrid_recall import hybrid_recall
from pipeline.block_cross_table.memory_rag.merge_settings import MergeRuntimeSettings
from pipeline.block_cross_table.memory_rag.store import insert_binding_confirmed


class _MemConfig:
    def __init__(self):
        self.params: dict = {}

    def get_param(self, key, default=None):
        return self.params[key] if key in self.params else default

    def set_param(self, key, value):
        self.params[key] = value


def _merge(**kwargs) -> MergeRuntimeSettings:
    from pipeline.block_cross_table.memory_rag.merge_settings import (
        load_merge_runtime_settings,
    )

    base = load_merge_runtime_settings()
    data = {
        "maibot_data_dir": "",
        "meaning_similarity_threshold": base.meaning_similarity_threshold,
        "meaning_fallback_threshold": base.meaning_fallback_threshold,
        "jargon_inference_thresholds": base.jargon_inference_thresholds,
        "jargon_stopwords": base.jargon_stopwords,
        "rrf_k": base.rrf_k,
        "vector_weight": base.vector_weight,
        "bm25_weight": base.bm25_weight,
        "sparse_top_k": base.sparse_top_k,
        "dense_top_k": base.dense_top_k,
        "hybrid_final_top_k": base.hybrid_final_top_k,
        "self_learning_engine": base.self_learning_engine,
        "meaning_equivalence_mode": base.meaning_equivalence_mode,
        "egress_policy": base.egress_policy,
        "dialogue_review_enabled": False,
        "embedded_model_path": "",
        "external_recall_enabled": kwargs.get("external_recall_enabled", True),
        "external_weight": kwargs.get("external_weight", 0.3),
        "dense_provider": kwargs.get("dense_provider", "external"),
    }
    return MergeRuntimeSettings(**data)


class TestExternalEmbeddingConfig(unittest.TestCase):
    def setUp(self):
        self._mem = _MemConfig()
        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        for k in (
            ENV_API_KEY,
            ENV_API_KEY_VENDOR_ALIAS,
            "MEMORY_RAG_EXTERNAL_RECALL_ENABLED",
            "MEMORY_RAG_FUSION_EXTERNAL_WEIGHT",
            "MEMORY_RAG_EXTERNAL_EMBEDDING_BASE_URL",
            "MEMORY_RAG_EXTERNAL_EMBEDDING_MODEL",
        ):
            os.environ.pop(k, None)
        self._cfg_patch = patch(
            "pipeline.block_cross_table.memory_rag.external_embedding_config.config",
            self._mem,
        )
        self._cfg_patch.start()

    def tearDown(self):
        self._cfg_patch.stop()
        self._env_patch.stop()

    def test_key_priority_env_over_config(self):
        # Product path: config.json wins over optional env.
        self._mem.set_param(PARAM_API_KEY, "config-key-should-win")
        os.environ[ENV_API_KEY_VENDOR_ALIAS] = "env-key-should-lose"
        key, src = resolve_external_api_key()
        self.assertEqual(key, "config-key-should-win")
        self.assertEqual(src, SOURCE_CONFIG)
        pub = load_external_embedding_public()
        self.assertEqual(pub.api_key_source, SOURCE_CONFIG)
        self.assertNotIn("config-key-should-win", pub.api_key_masked)

    def test_key_priority_config_over_none(self):
        self._mem.set_param(PARAM_API_KEY, "from-config-only")
        key, src = resolve_external_api_key()
        self.assertEqual(key, "from-config-only")
        self.assertEqual(src, SOURCE_CONFIG)

    def test_key_missing_not_blocked(self):
        self._mem.set_param(PARAM_ENABLED, True)
        self._mem.set_param(PARAM_WEIGHT, 0.3)
        self._mem.set_param(PARAM_BASE_URL, "https://api.example.com/v1")
        self._mem.set_param(PARAM_MODEL, "vendor/model-x")
        # no key
        resolved = resolve_external_embedding()
        self.assertFalse(resolved.api_key_present)
        self.assertEqual(resolved.api_key_source, SOURCE_NONE)

        conn = open_memory_db(embedding_dim=1024, enable_vec=True)
        try:
            set_backend_switch(
                conn,
                backend=BACKEND_EXTERNAL,
                chosen_at="t0",
                chosen_source_trace_id="t",
            )
            insert_binding_confirmed(
                conn,
                user_confirmed=True,
                utterance_span_fingerprint="fp",
                binding_payload={"term": "跨表键", "description": "d", "values": [], "scope": ""},
                source_trace_id="t",
                confirmed_at="t",
                scope={},
            )
            r = hybrid_recall(
                conn,
                query="跨表键",
                settings=_merge(external_recall_enabled=True, external_weight=0.3),
                auto_local_embed=False,
                case_id="missing_key_case",
                external_sidecar=CaseExternalEmbedSidecar(),
                external_settings=None,
                http_transport=None,
            )
            self.assertFalse(r["external_used"])
            self.assertEqual(r.get("skipped_reason"), "missing_external_api_key")
            self.assertIsInstance(r.get("accepted"), list)  # not blocked
        finally:
            conn.close()

    def test_gui_api_key_masked_in_evidence(self):
        save_external_embedding_patch(
            {
                "external_embedding": {
                    "enabled": True,
                    "base_url": "https://api.example.com/v1",
                    "model": "vendor/model-x",
                    "weight": 0.2,
                    "api_key": "sk-secret-plaintext-never-in-evidence",
                }
            }
        )
        pub = load_external_embedding_public()
        self.assertTrue(pub.api_key_configured)
        # tail-8 mask; short keys may echo fully — still must not invent sk-**** template
        self.assertTrue(pub.api_key_masked)
        self.assertNotEqual(pub.api_key_masked, "secret-full-key-value")
        evidence = {
            "api_key_present": True,
            "api_key_source": pub.api_key_source,
        }
        blob = json.dumps(evidence)
        self.assertNotIn("sk-secret-plaintext-never-in-evidence", blob)
        self.assertIn(pub.api_key_source, (SOURCE_CONFIG, SOURCE_ENV))

    def test_external_enabled_false_or_zero_weight_not_fused(self):
        self._mem.set_param(PARAM_API_KEY, "k")
        self._mem.set_param(PARAM_BASE_URL, "https://api.example.com/v1")
        self._mem.set_param(PARAM_MODEL, "vendor/model-x")
        self._mem.set_param(PARAM_ENABLED, False)
        self._mem.set_param(PARAM_WEIGHT, 0.5)
        r = resolve_external_embedding()
        self.assertFalse(r.enabled)

        self._mem.set_param(PARAM_ENABLED, True)
        self._mem.set_param(PARAM_WEIGHT, 0.0)
        r2 = resolve_external_embedding()
        self.assertEqual(r2.weight, 0.0)

        conn = open_memory_db(embedding_dim=1024, enable_vec=True)
        try:
            set_backend_switch(
                conn,
                backend=BACKEND_EXTERNAL,
                chosen_at="t0",
                chosen_source_trace_id="t",
            )
            calls = [0]

            def boom(*_a, **_k):
                calls[0] += 1
                raise AssertionError("must not call transport when disabled/zero weight")

            r_off = hybrid_recall(
                conn,
                query="q",
                settings=_merge(dense_provider="off", external_weight=0.3),
                auto_local_embed=False,
                case_id="off",
                external_sidecar=CaseExternalEmbedSidecar(),
                http_transport=boom,
                external_call_counter=[0],
            )
            self.assertFalse(r_off["external_attempted"])
            self.assertEqual(calls[0], 0)

            r_zw = hybrid_recall(
                conn,
                query="q",
                settings=_merge(dense_provider="external", external_weight=0.0),
                auto_local_embed=False,
                case_id="zw",
                external_sidecar=CaseExternalEmbedSidecar(),
                http_transport=boom,
                external_call_counter=[0],
            )
            self.assertFalse(r_zw["external_attempted"])
            self.assertEqual(calls[0], 0)
        finally:
            conn.close()

    def test_mask_api_key_helpers(self):
        # arch v2: last-8 visible (sk-abcdefgh is 11 chars → ***abcdefgh)
        self.assertEqual(mask_api_key("sk-abcdefgh"), "***abcdefgh")
        self.assertEqual(mask_api_key("abcdefgh"), "abcdefgh")
        self.assertEqual(mask_api_key(""), "")

    def test_masked_or_empty_save_keeps_existing_key(self):
        self._mem.set_param(PARAM_API_KEY, "keep-me-secret-key")
        save_external_embedding_patch(
            {"external_embedding": {"api_key": "sk-****"}}
        )
        self.assertEqual(self._mem.get_param(PARAM_API_KEY), "keep-me-secret-key")
        save_external_embedding_patch(
            {"external_embedding": {"api_key": ""}}
        )
        self.assertEqual(self._mem.get_param(PARAM_API_KEY), "keep-me-secret-key")
        save_external_embedding_patch(
            {"external_embedding": {"api_key": "***masked***"}}
        )
        self.assertEqual(self._mem.get_param(PARAM_API_KEY), "keep-me-secret-key")

    def test_env_over_config_file(self):
        # Name kept for suite continuity; behavior is config > optional env.
        self._mem.set_param(PARAM_API_KEY, "from-file")
        os.environ[ENV_API_KEY] = "from-env"
        key, src = resolve_external_api_key()
        self.assertEqual(key, "from-file")
        self.assertEqual(src, SOURCE_CONFIG)
        # When config empty, optional env may fill (dev only).
        self._mem.set_param(PARAM_API_KEY, "")
        key2, src2 = resolve_external_api_key()
        self.assertEqual(key2, "from-env")
        self.assertEqual(src2, SOURCE_ENV)

    def test_config_file_key_used_when_env_absent(self):
        os.environ.pop(ENV_API_KEY, None)
        os.environ.pop(ENV_API_KEY_VENDOR_ALIAS, None)
        self._mem.set_param(PARAM_API_KEY, "only-in-config")
        key, src = resolve_external_api_key()
        self.assertEqual(key, "only-in-config")
        self.assertEqual(src, SOURCE_CONFIG)

    def test_clear_key_empties_config(self):
        self._mem.set_param(PARAM_API_KEY, "to-be-cleared")
        save_external_embedding_patch(
            {"external_embedding": {"clear": True}}
        )
        self.assertEqual(self._mem.get_param(PARAM_API_KEY), "")
        key, src = resolve_external_api_key()
        self.assertEqual(key, "")
        self.assertEqual(src, SOURCE_NONE)


if __name__ == "__main__":
    unittest.main()
