# -*- coding: utf-8 -*-
"""S0: dense_provider enum / migration table / fallback state machine contracts."""
from __future__ import annotations

import unittest

from pipeline.block_cross_table.memory_rag import dense_provider as dp


class TestDenseProviderS0Contracts(unittest.TestCase):
    def test_enum_writable_providers(self):
        self.assertEqual(dp.DENSE_PROVIDERS, frozenset({"off", "local", "external"}))
        self.assertEqual(dp.DEFAULT_DENSE_PROVIDER, "off")

    def test_migration_table_four_cells(self):
        self.assertEqual(dp.MIGRATION_TABLE[(True, False)], ("local", None))
        self.assertEqual(dp.MIGRATION_TABLE[(False, True)], ("external", None))
        self.assertEqual(
            dp.MIGRATION_TABLE[(True, True)], ("local", "both_enabled→local")
        )
        self.assertEqual(dp.MIGRATION_TABLE[(False, False)], ("off", None))

    def test_fallback_state_machine_constants(self):
        self.assertEqual(dp.FALLBACK_STATE_TO_MODE["off"], "sparse_only")
        self.assertEqual(
            dp.FALLBACK_STATE_TO_MODE["local_success"], "dense_local_plus_sparse"
        )
        self.assertEqual(dp.FALLBACK_STATE_TO_MODE["local_fail"], "sparse_only")
        self.assertEqual(
            dp.FALLBACK_STATE_TO_MODE["external_success"],
            "dense_external_plus_sparse",
        )
        self.assertEqual(dp.FALLBACK_STATE_TO_MODE["external_fail"], "sparse_only")
        self.assertEqual(
            dp.FALLBACK_STATE_TO_MODE["external_weight_zero"], "sparse_only"
        )

    def test_hybrid_parallel_readable_not_writable(self):
        self.assertIn("hybrid_parallel", dp.RETRIEVAL_MODES_READABLE)
        self.assertNotIn("hybrid_parallel", dp.RETRIEVAL_MODES_WRITABLE)
        with self.assertRaises(ValueError):
            dp.assert_writable_retrieval_mode("hybrid_parallel")

    def test_assert_at_most_one_dense(self):
        dp.assert_at_most_one_dense(["sparse"])
        dp.assert_at_most_one_dense(["local_dense", "sparse"])
        dp.assert_at_most_one_dense(["external_dense", "sparse"])
        with self.assertRaises(AssertionError):
            dp.assert_at_most_one_dense(["local_dense", "external_dense", "sparse"])

    def test_mask_api_key_tail8_rules(self):
        self.assertEqual(dp.mask_api_key_tail8(""), "")
        self.assertEqual(dp.mask_api_key_tail8("short"), "short")
        self.assertEqual(dp.mask_api_key_tail8("12345678"), "12345678")
        self.assertEqual(
            dp.mask_api_key_tail8("abcdefghijklmnop"),
            "********ijklmnop",
        )


if __name__ == "__main__":
    unittest.main()
