# -*- coding: utf-8 -*-
"""Arch v2 XOR dense_provider: S2/S3/S4 behavioral tests."""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import pytest

from pipeline.block_cross_table.memory_rag import dense_provider as dp
from pipeline.block_cross_table.memory_rag.backend_switch import set_backend_switch
from pipeline.block_cross_table.memory_rag.bootstrap import open_memory_db
from pipeline.block_cross_table.memory_rag.constants import BACKEND_EXTERNAL
from pipeline.block_cross_table.memory_rag.external_circuit import (
    CaseExternalEmbedSidecar,
    reset_default_sidecar,
)
from pipeline.block_cross_table.memory_rag.external_embedding_config import (
    ENV_API_KEY,
    PARAM_API_KEY,
    load_external_embedding_public,
    mask_api_key,
    resolve_external_api_key,
    save_external_embedding_patch,
)
from pipeline.block_cross_table.memory_rag.external_settings import ExternalEmbeddingSettings
from pipeline.block_cross_table.memory_rag.hybrid_recall import hybrid_recall
from pipeline.block_cross_table.memory_rag.merge_settings import (
    MergeRuntimeSettings,
    load_merge_runtime_settings,
)
from pipeline.block_cross_table.memory_rag.store import insert_binding_confirmed
from tests.cross_table.memory_rag_test_helpers import slots_for_tests


class _MemCfg:
    def __init__(self):
        self.params: dict = {}

    def get_param(self, key, default=None):
        return self.params[key] if key in self.params else default

    def set_param(self, key, value):
        self.params[key] = value


def _merge(**kwargs) -> MergeRuntimeSettings:
    base = load_merge_runtime_settings()
    data = {
        "maibot_data_dir": "",
        "meaning_similarity_threshold": base.meaning_similarity_threshold,
        "meaning_fallback_threshold": base.meaning_fallback_threshold,
        "jargon_inference_thresholds": base.jargon_inference_thresholds,
        "jargon_stopwords": base.jargon_stopwords,
        "rrf_k": base.rrf_k,
        "vector_weight": kwargs.get("vector_weight", base.vector_weight),
        "bm25_weight": kwargs.get("bm25_weight", base.bm25_weight),
        "sparse_top_k": base.sparse_top_k,
        "dense_top_k": base.dense_top_k,
        "hybrid_final_top_k": base.hybrid_final_top_k,
        "self_learning_engine": base.self_learning_engine,
        "meaning_equivalence_mode": base.meaning_equivalence_mode,
        "egress_policy": base.egress_policy,
        "dialogue_review_enabled": False,
        "embedded_model_path": kwargs.get("embedded_model_path", ""),
        "external_recall_enabled": kwargs.get("external_recall_enabled", False),
        "external_weight": kwargs.get("external_weight", 0.3),
        "dense_provider": kwargs.get("dense_provider", "off"),
    }
    return MergeRuntimeSettings(**data)


def _ext(dim: int) -> ExternalEmbeddingSettings:
    return ExternalEmbeddingSettings(
        base_url="https://example.test/v1",
        api_key="test-key-not-real",
        model_id="embedding-x",
        embedding_dim=int(dim),
        embedding_schema_version="ext-v1",
        call_count_cap=5000,
        timeout_ms=45000,
        max_retries=0,
        top_k=10,
        recall_max_distance=None,
        allowlist=(),
    )


def _ok_transport(dim: int, counter: list[int]):
    def _t(url, headers, body, timeout_s):
        counter[0] += 1
        emb = [0.01] * int(dim)
        return 200, json.dumps({"data": [{"embedding": emb}]}).encode("utf-8")

    return _t


def _timeout_transport(counter: list[int]):
    def _t(url, headers, body, timeout_s):
        counter[0] += 1
        raise TimeoutError("sim")

    return _t


class TestDenseProviderXorRecall(unittest.TestCase):
    def setUp(self):
        self.slots = slots_for_tests()
        self.dim = int(self.slots["embedding_dim"])
        self.conn = open_memory_db(embedding_dim=self.dim, enable_vec=True)
        set_backend_switch(
            self.conn,
            backend=BACKEND_EXTERNAL,
            chosen_at="t0",
            chosen_source_trace_id="tr",
        )
        insert_binding_confirmed(
            self.conn,
            user_confirmed=True,
            utterance_span_fingerprint="fp_xor",
            binding_payload={
                "term": "跨表键",
                "description": "用于连接两张表的键",
                "values": [],
                "scope": "",
            },
            source_trace_id="t1",
            confirmed_at="2026-10-22T00:00:00+00:00",
            scope={},
        )
        self.ext = _ext(self.dim)
        self.sidecar = CaseExternalEmbedSidecar()
        reset_default_sidecar()

    def tearDown(self):
        self.conn.close()
        reset_default_sidecar()

    def _run(self, *, provider: str, transport, counter, weight=0.3, path=""):
        return hybrid_recall(
            self.conn,
            query="跨表键",
            settings=_merge(
                dense_provider=provider,
                external_weight=weight,
                embedded_model_path=path,
            ),
            case_id="xor_case",
            external_sidecar=self.sidecar,
            external_settings=self.ext,
            http_transport=transport,
            external_call_counter=counter,
            dense_provider=provider,
        )

    def test_external_zero_weight_behavior(self):
        counter = [0]
        r = self._run(
            provider="external",
            transport=_ok_transport(self.dim, [0]),
            counter=counter,
            weight=0.0,
        )
        self.assertFalse(r["external_attempted"])
        self.assertFalse(r["external_used"])
        self.assertEqual(r["retrieval_mode"], "sparse_only")
        self.assertEqual(counter[0], 0)

    def test_dense_provider_local_only_no_external_call(self):
        counter = [0]
        r = self._run(
            provider="local",
            transport=_ok_transport(self.dim, [0]),
            counter=counter,
            path="",  # no local model → local_fail → sparse
        )
        self.assertEqual(counter[0], 0)
        self.assertFalse(r["external_attempted"])
        self.assertNotIn("external_dense", r.get("channel_list") or [])

    def test_dense_provider_external_only_local_not_loaded(self):
        # Patch local loader — must never be called in external mode.
        calls = {"n": 0}

        def _boom(*a, **k):
            calls["n"] += 1
            return [0.1] * self.dim, None

        counter = [0]
        with patch(
            "pipeline.block_cross_table.memory_rag.hybrid_recall._try_local_query_embedding",
            _boom,
        ):
            r = self._run(
                provider="external",
                transport=_ok_transport(self.dim, [0]),
                counter=counter,
                path="C:/fake/model",
            )
        self.assertEqual(calls["n"], 0)
        self.assertTrue(r["external_used"])
        self.assertNotIn("local_dense", r.get("channel_list") or [])

    def test_dense_provider_off_sparse_only(self):
        counter = [0]
        r = self._run(
            provider="off",
            transport=_ok_transport(self.dim, [0]),
            counter=counter,
        )
        self.assertEqual(r["retrieval_mode"], "sparse_only")
        self.assertFalse(r["external_attempted"])
        self.assertEqual(counter[0], 0)

    def test_dense_provider_not_external_no_external_call(self):
        for provider in ("off", "local"):
            with self.subTest(provider=provider):
                counter = [0]
                r = self._run(
                    provider=provider,
                    transport=_ok_transport(self.dim, [0]),
                    counter=counter,
                )
                self.assertEqual(counter[0], 0, provider)
                self.assertFalse(r["external_attempted"], provider)

    def test_local_fail_falls_to_sparse_only_not_external(self):
        counter = [0]
        r = self._run(
            provider="local",
            transport=_ok_transport(self.dim, [0]),
            counter=counter,
            path="C:/does/not/exist/model",
        )
        self.assertEqual(r["retrieval_mode"], "sparse_only")
        self.assertEqual(counter[0], 0)
        self.assertNotIn("external_dense", r.get("channel_list") or [])

    def test_external_fail_falls_to_sparse_only_not_local(self):
        counter = [0]
        calls = {"n": 0}

        def _boom(*a, **k):
            calls["n"] += 1
            return [0.1] * self.dim, None

        with patch(
            "pipeline.block_cross_table.memory_rag.hybrid_recall._try_local_query_embedding",
            _boom,
        ):
            r = self._run(
                provider="external",
                transport=_timeout_transport([0]),
                counter=counter,
                path="C:/fake/model",
            )
        self.assertEqual(r["retrieval_mode"], "sparse_only")
        self.assertEqual(calls["n"], 0)
        self.assertFalse(r["external_used"])

    def test_new_runs_never_emit_hybrid_parallel(self):
        for provider in ("off", "local", "external"):
            with self.subTest(provider=provider):
                counter = [0]
                r = self._run(
                    provider=provider,
                    transport=_ok_transport(self.dim, [0]),
                    counter=counter,
                )
                self.assertNotEqual(r["retrieval_mode"], "hybrid_parallel")
                self.assertNotIn(
                    "hybrid_parallel",
                    dp.RETRIEVAL_MODES_WRITABLE,
                )

    def test_retrieval_mode_never_hybrid_parallel_across_all_states(self):
        for state in dp.FALLBACK_STATE_TO_MODE:
            mode = dp.retrieval_mode_for_state(state)
            self.assertNotEqual(mode, "hybrid_parallel", state)
            self.assertIn(mode, dp.RETRIEVAL_MODES_WRITABLE)


@pytest.mark.parametrize(
    "provider",
    ["off", "local", "external"],
    ids=["off", "local", "external"],
)
def test_channel_list_at_most_one_dense(provider):
    slots = slots_for_tests()
    dim = int(slots["embedding_dim"])
    conn = open_memory_db(embedding_dim=dim, enable_vec=True)
    try:
        set_backend_switch(
            conn,
            backend=BACKEND_EXTERNAL,
            chosen_at="t0",
            chosen_source_trace_id="tr",
        )
        insert_binding_confirmed(
            conn,
            user_confirmed=True,
            utterance_span_fingerprint="fp_ch",
            binding_payload={
                "term": "跨表键",
                "description": "用于连接两张表的键",
                "values": [],
                "scope": "",
            },
            source_trace_id="t1",
            confirmed_at="2026-10-22T00:00:00+00:00",
            scope={},
        )
        counter = [0]
        r = hybrid_recall(
            conn,
            query="跨表键",
            settings=_merge(dense_provider=provider, external_weight=0.3),
            case_id="ch",
            external_sidecar=CaseExternalEmbedSidecar(),
            external_settings=_ext(dim),
            http_transport=_ok_transport(dim, [0]),
            external_call_counter=counter,
            dense_provider=provider,
        )
        ch = r.get("channel_list") or []
        dp.assert_at_most_one_dense(ch)
        dense_n = sum(1 for c in ch if c in ("local_dense", "external_dense"))
        assert dense_n <= 1
        assert r["retrieval_mode"] != "hybrid_parallel"
    finally:
        conn.close()
        reset_default_sidecar()


class TestApiKeyPriorityAndMask(unittest.TestCase):
    def setUp(self):
        self.cfg = _MemCfg()
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop(ENV_API_KEY, None)
        os.environ.pop("SILICONFLOW_API_KEY", None)
        self._p = patch(
            "pipeline.block_cross_table.memory_rag.external_embedding_config.config",
            self.cfg,
        )
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._env.stop()

    def test_api_key_priority_config_over_env(self):
        self.cfg.set_param(PARAM_API_KEY, "config-key-wins-12345678")
        os.environ[ENV_API_KEY] = "env-key-should-lose-xxxxxxxx"
        key, src = resolve_external_api_key()
        self.assertEqual(key, "config-key-wins-12345678")
        self.assertEqual(src, "config")

    def test_api_key_masked_output_consistent(self):
        self.assertEqual(mask_api_key(""), "")
        self.assertEqual(mask_api_key("short"), "short")
        self.assertEqual(mask_api_key("abcdefgh"), "abcdefgh")
        long_k = "ABCDEFGHIJKLMNOPQRSTUVWX"
        masked = mask_api_key(long_k)
        self.assertTrue(masked.endswith("QRSTUVWX"))
        self.assertTrue(masked.startswith("*"))
        self.assertNotIn("ABCDEFGH", masked)
        self.cfg.set_param(PARAM_API_KEY, long_k)
        pub = load_external_embedding_public()
        self.assertEqual(pub.api_key_masked, masked)


if __name__ == "__main__":
    unittest.main()
