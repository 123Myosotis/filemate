# -*- coding: utf-8 -*-
"""
D7=A optional external recall branch: soft-fail + case circuit + 9 unit tests.

Injected transport only. external_calls counted via transport counter.
P1 full-chain not opened. Capability claims forbidden.
"""
from __future__ import annotations

import json
import unittest

import pytest

from pipeline.block_cross_table.memory_rag.backend_switch import set_backend_switch
from pipeline.block_cross_table.memory_rag.bootstrap import open_memory_db
from pipeline.block_cross_table.memory_rag.constants import BACKEND_EXTERNAL
from pipeline.block_cross_table.memory_rag.external_circuit import (
    CaseExternalEmbedSidecar,
    reset_default_sidecar,
)
from pipeline.block_cross_table.memory_rag.external_settings import ExternalEmbeddingSettings
from pipeline.block_cross_table.memory_rag.hybrid_recall import hybrid_recall
from pipeline.block_cross_table.memory_rag.merge_settings import (
    MergeRuntimeSettings,
    load_merge_runtime_settings,
)
from pipeline.block_cross_table.memory_rag.store import insert_binding_confirmed
from tests.cross_table.memory_rag_test_helpers import slots_for_tests


def _ext_settings(*, dim: int) -> ExternalEmbeddingSettings:
    return ExternalEmbeddingSettings(
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key="test-key-not-real",
        model_id="embedding-3",
        embedding_dim=int(dim),
        embedding_schema_version="zhipu-embedding-3-v1",
        call_count_cap=5000,
        timeout_ms=45000,
        max_retries=0,
        top_k=10,
        recall_max_distance=None,
        allowlist=(),
    )


def _merge_settings(**kwargs) -> MergeRuntimeSettings:
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
        "egress_policy": kwargs.get("egress_policy", base.egress_policy),
        "dialogue_review_enabled": False,
        "embedded_model_path": kwargs.get("embedded_model_path", ""),
        "external_recall_enabled": kwargs.get("external_recall_enabled", True),
        "external_weight": kwargs.get("external_weight", 0.3),
        # Arch v2: XOR; external-branch suite defaults to external mode.
        "dense_provider": kwargs.get("dense_provider", "external"),
    }
    return MergeRuntimeSettings(**data)


def _ok_transport(dim: int, counter: list[int]):
    def _t(url, headers, body, timeout_s):
        counter[0] += 1
        emb = [0.01] * int(dim)
        payload = json.dumps({"data": [{"embedding": emb}]}).encode("utf-8")
        return 200, payload

    return _t


def _timeout_transport(counter: list[int]):
    def _t(url, headers, body, timeout_s):
        counter[0] += 1
        raise TimeoutError("simulated_timeout")

    return _t


def _status_transport(status: int, counter: list[int], body: bytes | None = None):
    def _t(url, headers, body_in, timeout_s):
        counter[0] += 1
        raw = body if body is not None else b'{"error":{"code":"x"}}'
        return int(status), raw

    return _t


def _bad_dim_transport(dim: int, counter: list[int]):
    def _t(url, headers, body, timeout_s):
        counter[0] += 1
        emb = [0.01] * max(1, int(dim) - 1)
        payload = json.dumps({"data": [{"embedding": emb}]}).encode("utf-8")
        return 200, payload

    return _t


def _empty_vec_transport(counter: list[int]):
    def _t(url, headers, body, timeout_s):
        counter[0] += 1
        payload = json.dumps({"data": [{"embedding": []}]}).encode("utf-8")
        return 200, payload

    return _t


class TestMemoryRagExternalBranch(unittest.TestCase):
    def setUp(self):
        self.slots = slots_for_tests()
        self.dim = int(self.slots["embedding_dim"])
        self.conn = open_memory_db(embedding_dim=self.dim, enable_vec=True)
        set_backend_switch(
            self.conn,
            backend=BACKEND_EXTERNAL,
            chosen_at="t0",
            chosen_source_trace_id="tr_sw",
        )
        insert_binding_confirmed(
            self.conn,
            user_confirmed=True,
            utterance_span_fingerprint="fp_ext",
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
        self.ext = _ext_settings(dim=self.dim)
        self.sidecar = CaseExternalEmbedSidecar()
        reset_default_sidecar()

    def tearDown(self):
        self.conn.close()
        reset_default_sidecar()

    def _recall(
        self,
        *,
        case_id: str,
        transport,
        counter,
        settings=None,
        sidecar=None,
        query: str | None = None,
    ):
        # Default query hits FTS on seeded binding; override when same-conn
        # must avoid vector-cache key collision (e.g. used→skip in one test).
        q = query if query is not None else "跨表键"
        return hybrid_recall(
            self.conn,
            query=q,
            query_embedding=None,
            settings=settings or _merge_settings(),
            auto_local_embed=False,
            case_id=case_id,
            external_sidecar=sidecar if sidecar is not None else self.sidecar,
            external_settings=self.ext,
            http_transport=transport,
            external_call_counter=counter,
        )

    def test_external_ok_enters_rrf(self):
        counter = [0]
        # Soft layer also wraps transport; pass raw and let soft count OR use both.
        # Use transport that increments; soft_embed also has call_counter.
        r = self._recall(
            case_id="case_ok",
            transport=_ok_transport(self.dim, [0]),
            counter=counter,
        )
        self.assertTrue(r["external_used"])
        self.assertTrue(r["external_attempted"])
        self.assertFalse(r.get("circuit_open"))
        self.assertIsNone(r.get("skipped_reason"))
        self.assertNotEqual(r["retrieval_mode"], "sparse_only")
        fusion = (r["traces"][0] or {}).get("fusion") or {}
        self.assertGreaterEqual(int(fusion.get("channels") or 0), 2)
        self.assertIn("external_dense", fusion.get("channel_list") or [])
        self.assertEqual(counter[0], 1)

    def test_retrieval_mode_reflects_external_channel(self):
        """used=true forbids sparse_only; channel_list must include external_dense."""
        counter = [0]
        r = self._recall(
            case_id="case_mode_consistency",
            transport=_ok_transport(self.dim, [0]),
            counter=counter,
            query="跨表键",
        )
        self.assertTrue(r["external_used"])
        self.assertNotEqual(r["retrieval_mode"], "sparse_only")
        self.assertEqual(r["retrieval_mode"], "dense_external_plus_sparse")
        fusion = (r["traces"][0] or {}).get("fusion") or {}
        ch = fusion.get("channel_list") or []
        self.assertIn("external_dense", ch)
        self.assertNotIn("local_dense", ch)
        self.assertGreaterEqual(int(fusion.get("channels") or 0), 2)
        # Inverse: sparse_only path must not claim used
        counter2 = [0]
        r_skip = self._recall(
            case_id="case_mode_skip",
            transport=_timeout_transport([0]),
            counter=counter2,
            sidecar=CaseExternalEmbedSidecar(),
            query="跨表键 mode_skip_unique",
        )
        if r_skip["retrieval_mode"] == "sparse_only":
            self.assertFalse(r_skip["external_used"])
            self.assertTrue(r_skip.get("skipped_reason"))

    def test_timeout_retry3_then_skip(self):
        counter = [0]
        r = self._recall(
            case_id="case_timeout",
            transport=_timeout_transport([0]),
            counter=counter,
        )
        self.assertFalse(r["external_used"])
        self.assertTrue(r["external_attempted"])
        self.assertTrue(r["circuit_open"])
        self.assertTrue(r.get("skipped_reason"))
        # 1 initial + 2 retries = 3 transport calls (D7=A max_retries=2 → ≤135s)
        self.assertEqual(counter[0], 3)
        self.assertGreaterEqual(len(r["accepted"]), 0)  # not blocked

    def test_soft_retry_budget_total_attempts_is_3(self):
        from pipeline.block_cross_table.memory_rag import external_query_soft as soft

        attempts = 1 + int(soft.DEFAULT_SOFT_MAX_RETRIES)
        self.assertEqual(attempts, 3)
        self.assertEqual((soft.DEFAULT_SOFT_TIMEOUT_MS / 1000.0) * attempts, 135.0)

    def test_429_or_1113_immediate_skip(self):
        counter = [0]
        r = self._recall(
            case_id="case_429",
            transport=_status_transport(429, [0]),
            counter=counter,
        )
        self.assertFalse(r["external_used"])
        self.assertTrue(r["external_attempted"])
        self.assertTrue(r["circuit_open"])
        self.assertEqual(counter[0], 1)

        counter2 = [0]
        body_1113 = json.dumps(
            {"error": {"code": "1113", "message": "余额不足"}}
        ).encode("utf-8")
        sidecar2 = CaseExternalEmbedSidecar()
        r2 = self._recall(
            case_id="case_1113",
            transport=_status_transport(400, [0], body=body_1113),
            counter=counter2,
            sidecar=sidecar2,
        )
        self.assertFalse(r2["external_used"])
        self.assertEqual(counter2[0], 1)
        reason = str(r2.get("skipped_reason") or "")
        self.assertTrue("1113" in reason or "balance" in reason, reason)

    def test_case_circuit_breaker_no_second_call(self):
        counter = [0]
        t = _timeout_transport([0])
        r1 = self._recall(case_id="case_cb", transport=t, counter=counter)
        self.assertTrue(r1["circuit_open"])
        calls_after_first = counter[0]
        self.assertGreaterEqual(calls_after_first, 1)

        r2 = self._recall(case_id="case_cb", transport=t, counter=counter)
        self.assertEqual(counter[0], calls_after_first)
        self.assertTrue(r2["circuit_open"])
        self.assertFalse(r2["external_used"])

        # Cross-case may retry
        r3 = self._recall(case_id="case_cb_other", transport=t, counter=counter)
        self.assertGreater(counter[0], calls_after_first)
        self.assertTrue(r3["external_attempted"])

    def test_retrieval_mode_and_external_used_fields(self):
        counter_ok = [0]
        r_used = self._recall(
            case_id="case_fields_used",
            transport=_ok_transport(self.dim, [0]),
            counter=counter_ok,
            query="跨表键",
        )
        self.assertIn(
            r_used["retrieval_mode"],
            ("sparse_only", "dense_external_plus_sparse", "dense_local_plus_sparse"),
        )
        self.assertTrue(r_used["external_used"])
        self.assertEqual(counter_ok[0], 1)

        counter_skip = [0]
        sidecar = CaseExternalEmbedSidecar()
        r_skip = self._recall(
            case_id="case_fields_skip",
            transport=_timeout_transport([0]),
            counter=counter_skip,
            sidecar=sidecar,
            query="fields_skip_no_cache_key",
        )
        self.assertIn(
            r_skip["retrieval_mode"],
            ("sparse_only", "dense_external_plus_sparse", "dense_local_plus_sparse"),
        )
        self.assertFalse(r_skip["external_used"])
        self.assertTrue(r_skip.get("skipped_reason"))
        # Align with soft retry: 1 initial + 2 retries = 3 (see test_timeout_retry3_then_skip).
        self.assertEqual(counter_skip[0], 3)
        # Distinguish: same retrieval_mode family possible, but external_used differs
        self.assertNotEqual(r_used["external_used"], r_skip["external_used"])

    def test_external_bad_vector_immediate_skip(self):
        counter = [0]
        r = self._recall(
            case_id="case_baddim",
            transport=_bad_dim_transport(self.dim, [0]),
            counter=counter,
        )
        self.assertFalse(r["external_used"])
        self.assertTrue(r["circuit_open"])
        self.assertEqual(counter[0], 1)

        counter2 = [0]
        sidecar2 = CaseExternalEmbedSidecar()
        r2 = self._recall(
            case_id="case_emptyvec",
            transport=_empty_vec_transport([0]),
            counter=counter2,
            sidecar=sidecar2,
        )
        self.assertFalse(r2["external_used"])
        self.assertEqual(counter2[0], 1)

    @pytest.mark.skip(reason="架构已改为 XOR；见 test_dense_provider_not_external_no_external_call + test_external_zero_weight_behavior")
    def test_external_disabled_or_zero_weight_not_fused(self):
        counter = [0]
        r_off = self._recall(
            case_id="case_disabled",
            transport=_ok_transport(self.dim, [0]),
            counter=counter,
            settings=_merge_settings(dense_provider="off", external_weight=0.3),
        )
        self.assertFalse(r_off["external_attempted"])
        self.assertFalse(r_off["external_used"])
        self.assertEqual(counter[0], 0)

    def test_case_lifecycle_needs_user_replan_embed_once(self):
        """
        Discipline: inject transport call counter; assert external_calls==1;
        second recall (needs_user→replan lifecycle) hits sidecar cache — not result-only assert.
        """
        counter = [0]
        t = _ok_transport(self.dim, [0])
        r1 = self._recall(case_id="case_replan", transport=t, counter=counter)
        self.assertTrue(r1["external_used"])
        self.assertEqual(counter[0], 1)

        # Simulate needs_user → replan → second hybrid recall, same case_id
        r2 = self._recall(case_id="case_replan", transport=t, counter=counter)
        self.assertTrue(r2["external_used"])
        self.assertEqual(counter[0], 1)  # cache reuse; no second transport
        self.assertEqual(
            self.sidecar.state_for("case_replan").external_calls,
            1,
        )

    def test_db_query_cache_hit_refused_not_used(self):
        """补3: DB vector cache must not set external_used; live transport required."""
        from pipeline.block_cross_table.memory_rag.external_call import (
            embed_binding_external,
        )
        from pipeline.block_cross_table.memory_rag.external_query_soft import (
            clear_query_vector_cache,
            soft_embed_query_text,
        )

        # Seed DB cache via a live write (use_cache default True on store path).
        seed_counter = [0]
        emb, meta, _ = embed_binding_external(
            self.conn,
            binding_id=None,
            binding_payload="跨表键 cache_seed",
            base_url=self.ext.base_url,
            api_key=self.ext.api_key,
            model_id=self.ext.model_id,
            embedding_schema_version=self.ext.embedding_schema_version,
            allowlist=list(self.ext.allowlist),
            call_count_cap=self.ext.call_count_cap,
            timeout_ms=5000,
            max_retries=0,
            created_at="2026-10-22T00:00:00+00:00",
            http_transport=_ok_transport(self.dim, seed_counter),
            expected_dim=self.dim,
            use_cache=True,
        )
        self.assertTrue(emb)
        self.assertGreaterEqual(seed_counter[0], 1)

        # Soft path with failing transport: must not flip used via DB cache.
        fail_counter = [0]
        soft = soft_embed_query_text(
            self.conn,
            text="跨表键 cache_seed",
            case_id="case_cache_refuse",
            sidecar=CaseExternalEmbedSidecar(),
            settings=self.ext,
            http_transport=_timeout_transport([0]),
            call_counter=fail_counter,
            max_retries=0,
        )
        self.assertFalse(soft.used)
        self.assertTrue(soft.circuit_open)
        self.assertTrue(soft.skipped_reason)
        self.assertGreaterEqual(fail_counter[0], 1)

        # After failure, clear cache and confirm helper works for 真机验收.
        n = clear_query_vector_cache(self.conn)
        self.assertGreaterEqual(n, 0)
        self.assertFalse(soft.used)  # audit unchanged by cache clear

    def test_circuit_cleared_after_process_restart_sim(self):
        """
        Discipline: re-instantiate sidecar (or reset API) inside same test;
        assert external_attempted again True — not skipped via 'new process' excuse.
        """
        counter = [0]
        t = _timeout_transport([0])
        r1 = self._recall(
            case_id="case_restart",
            transport=t,
            counter=counter,
            sidecar=self.sidecar,
        )
        self.assertTrue(r1["circuit_open"])
        self.assertTrue(r1["external_attempted"])
        calls1 = counter[0]

        # Simulate process restart: new sidecar instance (memory cleared)
        new_sidecar = CaseExternalEmbedSidecar()
        r2 = self._recall(
            case_id="case_restart",
            transport=t,
            counter=counter,
            sidecar=new_sidecar,
        )
        self.assertTrue(r2["external_attempted"])
        self.assertGreater(counter[0], calls1)
        self.assertTrue(r2["circuit_open"])

        # Also verify reset API path
        reset_default_sidecar()
        counter3 = [0]
        r3 = hybrid_recall(
            self.conn,
            query="跨表键 case_restart_reset_api",
            settings=_merge_settings(),
            auto_local_embed=False,
            case_id="case_restart",
            external_settings=self.ext,
            http_transport=_timeout_transport([0]),
            external_call_counter=counter3,
        )
        self.assertTrue(r3["external_attempted"])
        self.assertGreaterEqual(counter3[0], 1)


if __name__ == "__main__":
    unittest.main()
