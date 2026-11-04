# -*- coding: utf-8 -*-
"""
M2 memory_rag: schema, vec0, backend gate, meta health, confirm-before-write.

数值来源：test_only_numeric_overrides（测试覆盖值、非生产默认）；
CURRENT 数值槽仍为 null。
"""
from __future__ import annotations

import inspect
import sqlite3
import unittest

from pipeline.block_cross_table.memory_rag import (
    EmbeddingVectorMeta,
    MemoryRagError,
    assert_embedded_path_allowed,
    assert_external_call_allowed,
    complete_reembed_queue_item,
    drop_vec0_for_rebuild,
    ensure_vec0,
    enqueue_reembed,
    insert_binding_confirmed,
    insert_embedding_meta,
    insert_vector_row,
    invoke_external_embeddings_hook,
    load_memory_numeric_slots_from_current,
    metas_mechanically_compatible,
    open_memory_db,
    require_compatible_metas,
    require_production_embedding_dim,
    require_production_recall_top_k,
    set_backend_switch,
    stored_embedding_dim,
    transition_lifecycle,
    try_reactivate_binding,
)
from pipeline.block_cross_table.memory_rag import production_slots as prod_mod
from pipeline.block_cross_table.memory_rag.constants import (
    BACKEND_EMBEDDED,
    BACKEND_EXTERNAL,
    REEMBED_DONE,
    REEMBED_QUEUED,
    REJECT_LIFECYCLE_NOT_ACTIVE,
    STATUS_INVALIDATED,
    STATUS_REVOKED,
    TABLE_BINDING,
    TABLE_REEMBED_QUEUE,
)
from pipeline.block_cross_table.memory_rag.errors import (
    LOCUS_BACKEND_GATE,
    LOCUS_EMBEDDED_UNAVAILABLE,
    LOCUS_LIFECYCLE_ILLEGAL,
    LOCUS_META_INCOMPATIBLE,
    LOCUS_PRODUCTION_NUMERIC_NULL,
    LOCUS_WRITE_UNCONFIRMED,
)
from pipeline.synth_contract.registry import load_contract
from tests.cross_table.memory_rag_test_helpers import (
    slots_for_tests,
    test_only_numeric_overrides,
)


class TestMemoryRagM2(unittest.TestCase):
    def setUp(self):
        slots = slots_for_tests()
        self.top_k = slots["memory_recall_top_k"]
        self.dim = slots["embedding_dim"]
        self.conn = open_memory_db(embedding_dim=self.dim)

    def tearDown(self):
        self.conn.close()

    # --- numeric path ---

    def test_test_helpers_read_overrides(self):
        ov = test_only_numeric_overrides()
        self.assertEqual(ov["memory_recall_top_k"], 10)
        self.assertEqual(ov["embedding_dim"], 1536)
        self.assertEqual(self.top_k, 10)
        self.assertEqual(self.dim, 1536)

    def test_current_memory_numeric_slots_null(self):
        slots = load_memory_numeric_slots_from_current()
        for key, val in slots.items():
            self.assertIsNone(val, msg=key)
        ct = (load_contract().data.get("cross_table") or {})
        for key in ("memory_recall_top_k", "embedding_dim", "embedded_model_id"):
            self.assertIsNone(ct.get(key), msg=key)
        # QE numerics remain null (no CURRENT merge of memory or QE budgets).
        self.assertIsNone(ct.get("query_ask_budget"))

    def test_production_path_does_not_read_test_overrides(self):
        src = inspect.getsource(prod_mod)
        # Production module must not import or open the test inject path.
        self.assertNotIn("memory_rag_test_helpers", src)
        self.assertNotIn("json.loads", src)
        self.assertNotIn("Path(", src)
        self.assertIn("load_contract", src)
        with self.assertRaises(MemoryRagError) as ctx:
            require_production_embedding_dim()
        self.assertEqual(ctx.exception.fail_locus, LOCUS_PRODUCTION_NUMERIC_NULL)
        with self.assertRaises(MemoryRagError) as ctx2:
            require_production_recall_top_k()
        self.assertEqual(ctx2.exception.fail_locus, LOCUS_PRODUCTION_NUMERIC_NULL)

    # --- schema / vec0 ---

    def test_schema_and_vec0_dim(self):
        self.assertEqual(stored_embedding_dim(self.conn), self.dim)
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE_BINDING,),
        ).fetchone()
        self.assertIsNotNone(row)
        # active filter index present
        idx = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_memory_binding_active",),
        ).fetchone()
        self.assertIsNotNone(idx)

    def test_dim_change_requires_rebuild(self):
        with self.assertRaises(MemoryRagError) as ctx:
            ensure_vec0(self.conn, embedding_dim=self.dim + 1)
        self.assertIn("rebuild", ctx.exception.subreason or "")
        drop_vec0_for_rebuild(self.conn)
        ensure_vec0(self.conn, embedding_dim=self.dim + 1)
        self.assertEqual(stored_embedding_dim(self.conn), self.dim + 1)

    # --- backend gate ---

    def test_gate_embedded_rejects_external(self):
        set_backend_switch(
            self.conn,
            backend=BACKEND_EMBEDDED,
            chosen_at="t0",
            chosen_source_trace_id="tr_sw",
        )
        with self.assertRaises(MemoryRagError) as ctx:
            assert_external_call_allowed(self.conn)
        self.assertEqual(ctx.exception.fail_locus, LOCUS_BACKEND_GATE)

        def _hook():
            return {"ok": True}

        with self.assertRaises(MemoryRagError):
            invoke_external_embeddings_hook(self.conn, hook=_hook)

    def test_gate_external_allows_hook_path(self):
        set_backend_switch(
            self.conn,
            backend=BACKEND_EXTERNAL,
            chosen_at="t0",
            chosen_source_trace_id="tr_sw",
        )
        state = assert_external_call_allowed(self.conn)
        self.assertEqual(state.backend, BACKEND_EXTERNAL)

        def _hook(*, n: int):
            return {"n": n}

        result, traces = invoke_external_embeddings_hook(
            self.conn, hook=_hook, hook_kwargs={"n": 3}
        )
        self.assertEqual(result["n"], 3)
        self.assertEqual(traces[0]["kind"], "memory_external_call")

    def test_embedded_path_hard_fails_even_when_selected(self):
        set_backend_switch(
            self.conn,
            backend=BACKEND_EMBEDDED,
            chosen_at="t0",
            chosen_source_trace_id="tr_sw",
        )
        with self.assertRaises(MemoryRagError) as ctx:
            assert_embedded_path_allowed(self.conn)
        self.assertEqual(ctx.exception.fail_locus, LOCUS_EMBEDDED_UNAVAILABLE)

    # --- meta ---

    def test_meta_incompatible_refuses_mix(self):
        a = EmbeddingVectorMeta("m1", "ep1", "v1")
        b = EmbeddingVectorMeta("m1", "ep2", "v1")
        self.assertFalse(metas_mechanically_compatible(a, b))
        with self.assertRaises(MemoryRagError) as ctx:
            require_compatible_metas(a, b)
        self.assertEqual(ctx.exception.fail_locus, LOCUS_META_INCOMPATIBLE)

        meta_id = insert_embedding_meta(
            self.conn, meta=a, created_at="t0", meta_id="meta_a"
        )
        self.assertEqual(meta_id, "meta_a")
        vec = [0.0] * self.dim
        with self.assertRaises(MemoryRagError):
            insert_vector_row(
                self.conn,
                rowid=1,
                embedding=vec,
                expected_dim=self.dim,
                query_meta=a,
                row_meta=b,
            )
        # compatible path succeeds
        insert_vector_row(
            self.conn,
            rowid=1,
            embedding=vec,
            expected_dim=self.dim,
            query_meta=a,
            row_meta=a,
        )

    # --- confirm before write ---

    def test_write_refused_without_confirmation(self):
        with self.assertRaises(MemoryRagError) as ctx:
            insert_binding_confirmed(
                self.conn,
                user_confirmed=False,
                utterance_span_fingerprint="fp1",
                binding_payload={"k": "v"},
                source_trace_id="tr1",
                confirmed_at="t1",
                scope={"s": 1},
            )
        self.assertEqual(ctx.exception.fail_locus, LOCUS_WRITE_UNCONFIRMED)
        n = self.conn.execute(
            f"SELECT COUNT(*) FROM {TABLE_BINDING}"
        ).fetchone()[0]
        self.assertEqual(n, 0)

    def test_write_ok_when_confirmed(self):
        bid, traces = insert_binding_confirmed(
            self.conn,
            user_confirmed=True,
            utterance_span_fingerprint="fp1",
            binding_payload={"k": "v"},
            source_trace_id="tr1",
            confirmed_at="t1",
            scope={"s": 1},
        )
        self.assertTrue(bid)
        self.assertEqual(traces[0]["kind"], "memory_write")

    # --- lifecycle ---

    def test_lifecycle_cannot_reactivate(self):
        bid, _ = insert_binding_confirmed(
            self.conn,
            user_confirmed=True,
            utterance_span_fingerprint="fp2",
            binding_payload={},
            source_trace_id="tr2",
            confirmed_at="t2",
            scope={},
        )
        transition_lifecycle(self.conn, binding_id=bid, new_status=STATUS_INVALIDATED)
        with self.assertRaises(MemoryRagError) as ctx:
            try_reactivate_binding(self.conn, bid)
        self.assertEqual(ctx.exception.fail_locus, LOCUS_LIFECYCLE_ILLEGAL)

        # Also exercise SQL trigger if someone bypasses Python check.
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                f"UPDATE {TABLE_BINDING} SET lifecycle_status='active' WHERE binding_id=?",
                (bid,),
            )
            self.conn.commit()

    def test_lifecycle_active_to_revoked(self):
        bid, _ = insert_binding_confirmed(
            self.conn,
            user_confirmed=True,
            utterance_span_fingerprint="fp3",
            binding_payload={},
            source_trace_id="tr3",
            confirmed_at="t3",
            scope={},
        )
        traces = transition_lifecycle(
            self.conn, binding_id=bid, new_status=STATUS_REVOKED
        )
        self.assertEqual(traces[0]["kind"], "memory_revoke")

    # --- non-blocking supplements (post M2 pass; before M3) ---

    def test_lifecycle_active_to_invalidated(self):
        """invalidated is terminal; cannot return to active (M3 recall → rejected)."""
        bid, _ = insert_binding_confirmed(
            self.conn,
            user_confirmed=True,
            utterance_span_fingerprint="fp_inv",
            binding_payload={},
            source_trace_id="tr_inv",
            confirmed_at="t_inv",
            scope={},
        )
        traces = transition_lifecycle(
            self.conn, binding_id=bid, new_status=STATUS_INVALIDATED
        )
        self.assertEqual(traces[0]["kind"], "memory_invalidate")
        status = self.conn.execute(
            f"SELECT lifecycle_status FROM {TABLE_BINDING} WHERE binding_id=?",
            (bid,),
        ).fetchone()[0]
        self.assertEqual(status, STATUS_INVALIDATED)
        with self.assertRaises(MemoryRagError) as ctx:
            try_reactivate_binding(self.conn, bid)
        self.assertEqual(ctx.exception.fail_locus, LOCUS_LIFECYCLE_ILLEGAL)
        # M3 recall: non-active → rejected[] with this reason (filter not wired yet).
        self.assertEqual(REJECT_LIFECYCLE_NOT_ACTIVE, "lifecycle_not_active")

    def test_reembed_queue_enqueue_and_complete(self):
        """M2: enqueue + complete (done) link on memory_reembed_queue (not table-only)."""
        bid, _ = insert_binding_confirmed(
            self.conn,
            user_confirmed=True,
            utterance_span_fingerprint="fp_re",
            binding_payload={},
            source_trace_id="tr_re",
            confirmed_at="t_re",
            scope={},
        )
        qid, traces = enqueue_reembed(
            self.conn,
            binding_id=bid,
            old_meta_id=None,
            reason="meta_changed",
            enqueued_at="t_enq",
        )
        self.assertEqual(traces[0]["kind"], "memory_reembed")
        self.assertEqual(traces[0]["status"], REEMBED_QUEUED)
        row = self.conn.execute(
            f"SELECT status, binding_id FROM {TABLE_REEMBED_QUEUE} WHERE queue_id=?",
            (qid,),
        ).fetchone()
        self.assertEqual(row[0], REEMBED_QUEUED)
        self.assertEqual(row[1], bid)
        done_traces = complete_reembed_queue_item(
            self.conn, queue_id=qid, status=REEMBED_DONE
        )
        self.assertEqual(done_traces[0]["status"], REEMBED_DONE)
        final = self.conn.execute(
            f"SELECT status FROM {TABLE_REEMBED_QUEUE} WHERE queue_id=?",
            (qid,),
        ).fetchone()[0]
        self.assertEqual(final, REEMBED_DONE)


if __name__ == "__main__":
    unittest.main()
