# -*- coding: utf-8 -*-
"""
M3 memory_rag: recall/conflict/C14/external/allowlist.

数值来源：test_only_numeric_overrides（测试覆盖值、非生产默认）；
CURRENT 数值槽仍为 null。
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from pipeline.block_cross_table.memory_rag import (
    EmbeddingVectorMeta,
    MemoryRagError,
    assemble_qp_input_with_memory_candidates,
    build_egress_body,
    build_memory_conflict_package,
    detect_multi_binding_conflict,
    embed_binding_external,
    evaluate_endpoint_allowlist,
    get_call_count,
    inject_c14_all_targets,
    insert_binding_confirmed,
    insert_embedding_meta,
    open_memory_db,
    recall_bindings,
    require_endpoint_allowlisted,
    set_backend_switch,
    transition_lifecycle,
)
from pipeline.block_cross_table.memory_rag.constants import (
    ALLOWLIST_MODE_EMPTY,
    ALLOWLIST_MODE_ENFORCED,
    BACKEND_EXTERNAL,
    EMBEDDING_SCHEMA_VERSION_M3,
    EXTERNAL_CALL_STATUS_CACHE_HIT,
    REJECT_INCOMPATIBLE_META,
    REJECT_LIFECYCLE_NOT_ACTIVE,
    REJECT_TOP_K_TRUNCATED,
    STATUS_INVALIDATED,
)
from pipeline.block_cross_table.memory_rag.errors import (
    LOCUS_BACKEND_GATE,
    LOCUS_EXTERNAL_CALL_FAILED,
    LOCUS_WRITE_UNCONFIRMED,
)
from pipeline.block_cross_table.memory_rag.payload_text import cache_key_for
from pipeline.block_cross_table.memory_rag.store import enqueue_reembed
from pipeline.block_cross_table.query_engine import load_op_catalog_from_current
from pipeline.synth_contract.registry import load_contract
from tests.cross_table.memory_rag_test_helpers import slots_for_tests


def _ok_transport_factory(dim: int, bodies_out: list):
    def _transport(url, headers, body, timeout_s):
        bodies_out.append(json.loads(body.decode("utf-8")))
        emb = [0.01] * int(dim)
        payload = json.dumps({"data": [{"embedding": emb}]}).encode("utf-8")
        return 200, payload

    return _transport


def _fail_transport(url, headers, body, timeout_s):
    raise TimeoutError("simulated_timeout")


class TestMemoryRagM3(unittest.TestCase):
    def setUp(self):
        self.slots = slots_for_tests()
        self.dim = self.slots["embedding_dim"]
        self.top_k = self.slots["memory_recall_top_k"]
        self.conn = open_memory_db(embedding_dim=self.dim)
        set_backend_switch(
            self.conn,
            backend=BACKEND_EXTERNAL,
            chosen_at="t0",
            chosen_source_trace_id="tr_sw",
        )

    def tearDown(self):
        self.conn.close()

    def _confirm(self, *, fp: str, payload: dict, bid: str | None = None, meta_ref=None):
        return insert_binding_confirmed(
            self.conn,
            user_confirmed=True,
            utterance_span_fingerprint=fp,
            binding_payload=payload,
            source_trace_id="tr_w",
            confirmed_at="t_w",
            scope={"k": 1},
            embedding_meta_ref=meta_ref,
            binding_id=bid,
        )

    # --- write ---

    def test_confirm_write_and_reject_unconfirmed(self):
        bid, _ = self._confirm(fp="fp_a", payload={"x": 1})
        self.assertTrue(bid)
        with self.assertRaises(MemoryRagError) as ctx:
            insert_binding_confirmed(
                self.conn,
                user_confirmed=False,
                utterance_span_fingerprint="fp_b",
                binding_payload={},
                source_trace_id="tr",
                confirmed_at="t",
                scope={},
            )
        self.assertEqual(ctx.exception.fail_locus, LOCUS_WRITE_UNCONFIRMED)

    # --- recall ---

    def test_recall_accepted_rejected_meta_lifecycle_topk(self):
        meta_ok = EmbeddingVectorMeta("m", "ep", EMBEDDING_SCHEMA_VERSION_M3)
        meta_bad = EmbeddingVectorMeta("m2", "ep", EMBEDDING_SCHEMA_VERSION_M3)
        mid_ok = insert_embedding_meta(self.conn, meta=meta_ok, created_at="t")
        mid_bad = insert_embedding_meta(self.conn, meta=meta_bad, created_at="t")
        self._confirm(fp="fp1", payload={"a": 1}, bid="b1", meta_ref=mid_ok)
        self._confirm(fp="fp2", payload={"a": 2}, bid="b2", meta_ref=mid_bad)
        b3, _ = self._confirm(fp="fp3", payload={"a": 3}, bid="b3", meta_ref=mid_ok)
        transition_lifecycle(self.conn, binding_id=b3, new_status=STATUS_INVALIDATED)
        for i in range(4, 16):
            self._confirm(
                fp="fp%d" % i,
                payload={"a": i},
                bid="b%d" % i,
                meta_ref=mid_ok,
            )
        out = recall_bindings(self.conn, top_k=3, query_meta=meta_ok)
        reasons = {r["reject_reason"] for r in out["rejected"]}
        self.assertIn(REJECT_LIFECYCLE_NOT_ACTIVE, reasons)
        self.assertIn(REJECT_INCOMPATIBLE_META, reasons)
        self.assertIn(REJECT_TOP_K_TRUNCATED, reasons)
        self.assertLessEqual(len(out["accepted"]), 3)
        self.assertEqual(out["traces"][0]["kind"], "memory_recall")

    # --- conflict ---

    def test_conflict_package_to_e_no_silent_overwrite(self):
        self._confirm(fp="same_fp", payload={"a": 1}, bid="c1")
        self._confirm(fp="same_fp", payload={"a": 2}, bid="c2")
        recalled = recall_bindings(self.conn, top_k=10)
        conflict = detect_multi_binding_conflict(
            recalled["accepted"], source_trace_id="tr_c"
        )
        self.assertIsNotNone(conflict)
        self.assertTrue(conflict["package"]["silent_overwrite_forbidden"])
        self.assertEqual(conflict["e_route"]["status"], "needs_user")
        self.assertEqual(conflict["traces"][0]["kind"], "memory_conflict")
        pack = build_memory_conflict_package(
            candidate_binding_ids=["c1", "c2"],
            conflict_kind="multi_binding",
            source_trace_id="tr_c2",
        )
        self.assertIn("e_entry", pack["e_route"])

    # --- C.14 ---

    def test_c14_inject_e_qr_qp(self):
        self._confirm(fp="fp", payload={"t": 1}, bid="i1")
        recalled = recall_bindings(self.conn, top_k=10)
        pack = inject_c14_all_targets(
            accepted_candidates=recalled["accepted"], top_k=self.top_k
        )
        self.assertEqual(set(pack["by_target"].keys()), {"E", "QR", "QP"})
        kinds = [t["kind"] for t in pack["traces"]]
        self.assertEqual(kinds.count("memory_c14_inject"), 3)
        env = assemble_qp_input_with_memory_candidates(
            utterance="u",
            portraits=[{"structure_status": "layers_recognized"}],
            catalog=load_op_catalog_from_current(),
            memory_recall_candidates=pack["by_target"]["QP"]["memory_recall_candidates"],
        )
        self.assertIn("memory_recall_candidates", env)
        self.assertTrue(env["memory_recall_candidates"])

    # --- allowlist ---

    def test_allowlist_empty_exemption_and_enforced_reject(self):
        mode, allowed = evaluate_endpoint_allowlist("https://api.example.com", [])
        self.assertEqual(mode, ALLOWLIST_MODE_EMPTY)
        self.assertTrue(allowed)
        mode2 = require_endpoint_allowlisted("https://api.example.com", [])
        self.assertEqual(mode2, ALLOWLIST_MODE_EMPTY)
        with self.assertRaises(MemoryRagError) as ctx:
            require_endpoint_allowlisted(
                "https://evil.example.com",
                ["https://api.example.com"],
            )
        self.assertEqual(ctx.exception.fail_locus, LOCUS_BACKEND_GATE)
        self.assertEqual(ctx.exception.subreason, "endpoint_not_allowlisted")
        mode3, ok = evaluate_endpoint_allowlist(
            "https://api.example.com",
            ["https://api.example.com"],
        )
        self.assertEqual(mode3, ALLOWLIST_MODE_ENFORCED)
        self.assertTrue(ok)

    # --- egress body ---

    def test_egress_body_only_payload_text(self):
        body = build_egress_body(model_id="text-embedding-3-small", binding_payload={"k": "v"})
        self.assertEqual(set(body.keys()), {"model", "input"})
        self.assertNotIn("scope", body)
        self.assertNotIn("utterance", body)
        captured = []
        emb, meta, traces = embed_binding_external(
            self.conn,
            binding_id="e1",
            binding_payload={"k": "v"},
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model_id="text-embedding-3-small",
            embedding_schema_version=EMBEDDING_SCHEMA_VERSION_M3,
            allowlist=[],
            call_count_cap=self.slots["memory_external_call_count_cap"],
            timeout_ms=self.slots["memory_external_call_timeout_ms"],
            max_retries=self.slots["memory_external_call_max_retries"],
            created_at="t",
            http_transport=_ok_transport_factory(self.dim, captured),
            expected_dim=self.dim,
        )
        self.assertEqual(len(emb), self.dim)
        self.assertEqual(set(captured[0].keys()), {"model", "input"})
        self.assertEqual(traces[0]["allowlist_mode"], ALLOWLIST_MODE_EMPTY)
        self.assertEqual(traces[0]["status"], "ok")

    # --- cache / call_count / retry ---

    def test_cache_hit_does_not_bump_call_count(self):
        captured = []
        transport = _ok_transport_factory(self.dim, captured)
        kwargs = dict(
            binding_id="cache1",
            binding_payload={"same": True},
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model_id="text-embedding-3-small",
            embedding_schema_version=EMBEDDING_SCHEMA_VERSION_M3,
            allowlist=[],
            call_count_cap=self.slots["memory_external_call_count_cap"],
            timeout_ms=30000,
            max_retries=2,
            created_at="t",
            http_transport=transport,
            expected_dim=self.dim,
        )
        _, _, t1 = embed_binding_external(self.conn, **kwargs)
        c1 = get_call_count(self.conn)
        self.assertEqual(t1[0]["status"], "ok")
        _, _, t2 = embed_binding_external(self.conn, **kwargs)
        self.assertEqual(t2[0]["status"], EXTERNAL_CALL_STATUS_CACHE_HIT)
        self.assertEqual(get_call_count(self.conn), c1)
        self.assertEqual(len(captured), 1)

    def test_retries_exhausted_fail_locus(self):
        with self.assertRaises(MemoryRagError) as ctx:
            embed_binding_external(
                self.conn,
                binding_id="fail1",
                binding_payload={"x": 1},
                base_url="https://api.example.com/v1",
                api_key="sk-test",
                model_id="m",
                embedding_schema_version=EMBEDDING_SCHEMA_VERSION_M3,
                allowlist=[],
                call_count_cap=5000,
                timeout_ms=30000,
                max_retries=2,
                created_at="t",
                http_transport=_fail_transport,
                expected_dim=self.dim,
            )
        self.assertEqual(ctx.exception.fail_locus, LOCUS_EXTERNAL_CALL_FAILED)
        self.assertEqual(ctx.exception.subreason, "retries_exhausted")
        self.assertEqual(ctx.exception.detail.get("status"), "failed")

    def test_call_count_cap_rejects(self):
        # Force counter near cap via repeated failing? Use ok transport with tiny cap.
        captured = []
        transport = _ok_transport_factory(self.dim, captured)
        for i in range(2):
            embed_binding_external(
                self.conn,
                binding_id="cap%d" % i,
                binding_payload={"i": i},
                base_url="https://api.example.com/v1",
                api_key="sk-test",
                model_id="m",
                embedding_schema_version=EMBEDDING_SCHEMA_VERSION_M3,
                allowlist=[],
                call_count_cap=2,
                timeout_ms=30000,
                max_retries=0,
                created_at="t",
                http_transport=transport,
                expected_dim=self.dim,
            )
        with self.assertRaises(MemoryRagError) as ctx:
            embed_binding_external(
                self.conn,
                binding_id="cap_over",
                binding_payload={"i": 99},
                base_url="https://api.example.com/v1",
                api_key="sk-test",
                model_id="m",
                embedding_schema_version=EMBEDDING_SCHEMA_VERSION_M3,
                allowlist=[],
                call_count_cap=2,
                timeout_ms=30000,
                max_retries=0,
                created_at="t",
                http_transport=transport,
                expected_dim=self.dim,
            )
        self.assertEqual(ctx.exception.subreason, "call_count_cap")

    def test_allowlist_mode_on_enforced_ok_trace(self):
        captured = []
        _, _, traces = embed_binding_external(
            self.conn,
            binding_id="al1",
            binding_payload={"z": 1},
            base_url="https://api.example.com/v1",
            api_key="sk",
            model_id="m",
            embedding_schema_version=EMBEDDING_SCHEMA_VERSION_M3,
            allowlist=["https://api.example.com/v1"],
            call_count_cap=5000,
            timeout_ms=30000,
            max_retries=0,
            created_at="t",
            http_transport=_ok_transport_factory(self.dim, captured),
            expected_dim=self.dim,
        )
        self.assertEqual(traces[0]["allowlist_mode"], ALLOWLIST_MODE_ENFORCED)

    def test_current_still_null(self):
        ct = load_contract().data.get("cross_table") or {}
        self.assertIsNone(ct.get("memory_recall_top_k"))
        self.assertIsNone(ct.get("memory_external_call_count_cap"))

    # --- non-blocking supplements (post M3 pass; before M4) ---

    def test_call_count_increments_per_successful_call(self):
        """Each successful network call +1; cache hit does not increment."""
        captured = []
        transport = _ok_transport_factory(self.dim, captured)
        self.assertEqual(get_call_count(self.conn), 0)
        base_kw = dict(
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model_id="text-embedding-3-small",
            embedding_schema_version=EMBEDDING_SCHEMA_VERSION_M3,
            allowlist=[],
            call_count_cap=5000,
            timeout_ms=30000,
            max_retries=0,
            created_at="t",
            http_transport=transport,
            expected_dim=self.dim,
        )
        _, _, t1 = embed_binding_external(
            self.conn, binding_id="inc1", binding_payload={"n": 1}, **base_kw
        )
        self.assertEqual(t1[0]["status"], "ok")
        self.assertEqual(get_call_count(self.conn), 1)
        _, _, t2 = embed_binding_external(
            self.conn, binding_id="inc2", binding_payload={"n": 2}, **base_kw
        )
        self.assertEqual(t2[0]["status"], "ok")
        self.assertEqual(get_call_count(self.conn), 2)
        _, _, t3 = embed_binding_external(
            self.conn, binding_id="inc1b", binding_payload={"n": 1}, **base_kw
        )
        self.assertEqual(t3[0]["status"], EXTERNAL_CALL_STATUS_CACHE_HIT)
        self.assertEqual(get_call_count(self.conn), 2)
        self.assertEqual(len(captured), 2)

    def test_cache_key_miss_on_model_id_change_enqueues_reembed(self):
        """
        Cache key = payload_fp + model_id + endpoint_fingerprint + schema_version.
        Changing model_id must not hit cache; enqueue reembed for meta change path.
        """
        payload = {"same_payload": True}
        k1 = cache_key_for(
            binding_payload=payload,
            model_id="model-a",
            endpoint_fingerprint="https://api.example.com/v1",
            embedding_schema_version=EMBEDDING_SCHEMA_VERSION_M3,
        )
        k2 = cache_key_for(
            binding_payload=payload,
            model_id="model-b",
            endpoint_fingerprint="https://api.example.com/v1",
            embedding_schema_version=EMBEDDING_SCHEMA_VERSION_M3,
        )
        self.assertNotEqual(k1, k2)
        captured = []
        transport = _ok_transport_factory(self.dim, captured)
        kw = dict(
            binding_id="meta_chg",
            binding_payload=payload,
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            embedding_schema_version=EMBEDDING_SCHEMA_VERSION_M3,
            allowlist=[],
            call_count_cap=5000,
            timeout_ms=30000,
            max_retries=0,
            created_at="t",
            http_transport=transport,
            expected_dim=self.dim,
        )
        _, _, t1 = embed_binding_external(self.conn, model_id="model-a", **kw)
        self.assertEqual(t1[0]["status"], "ok")
        _, _, t2 = embed_binding_external(self.conn, model_id="model-b", **kw)
        self.assertEqual(t2[0]["status"], "ok")
        self.assertNotEqual(t2[0]["status"], EXTERNAL_CALL_STATUS_CACHE_HIT)
        self.assertEqual(len(captured), 2)
        qid, traces = enqueue_reembed(
            self.conn,
            binding_id="meta_chg",
            old_meta_id=None,
            reason="model_id_changed",
            enqueued_at="t_re",
        )
        self.assertTrue(qid)
        self.assertEqual(traces[0]["kind"], "memory_reembed")
        self.assertEqual(traces[0]["reason"], "model_id_changed")

    def test_real_http_loopback_openai_compatible(self):
        """Real HTTP socket to loopback OpenAI-compatible embeddings (path exercised)."""
        dim = self.dim
        state = {"hits": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                state["hits"] += 1
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                body = json.loads(raw.decode("utf-8"))
                assert set(body.keys()) == {"model", "input"}
                assert "scope" not in body
                payload = json.dumps(
                    {"data": [{"embedding": [0.02] * dim}]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt, *args):  # noqa: A003
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%s/v1" % port
            emb, meta, traces = embed_binding_external(
                self.conn,
                binding_id="http1",
                binding_payload={"loopback": True},
                base_url=base,
                api_key="sk-loopback",
                model_id="text-embedding-3-small",
                embedding_schema_version=EMBEDDING_SCHEMA_VERSION_M3,
                allowlist=[],
                call_count_cap=5000,
                timeout_ms=30000,
                max_retries=2,
                created_at="t",
                http_transport=None,  # real urllib
                expected_dim=dim,
            )
            self.assertEqual(len(emb), dim)
            self.assertEqual(state["hits"], 1)
            self.assertEqual(traces[0]["status"], "ok")
            self.assertEqual(traces[0]["allowlist_mode"], ALLOWLIST_MODE_EMPTY)
            self.assertEqual(traces[0]["model_id"], "text-embedding-3-small")
            # Desensitized sample for evidence (no api_key).
            self._http_sample = {
                "endpoint": "http://127.0.0.1:%s/v1" % port,
                "model_id": traces[0]["model_id"],
                "status": traces[0]["status"],
                "allowlist_mode": traces[0]["allowlist_mode"],
                "call_count": traces[0]["call_count"],
                "endpoint_fingerprint": meta.endpoint_fingerprint,
            }
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
