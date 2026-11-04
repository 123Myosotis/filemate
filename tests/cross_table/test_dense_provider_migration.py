# -*- coding: utf-8 -*-
"""S1: dense_provider legacy migration + conflict banner."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from pipeline.block_cross_table.memory_rag import dense_provider as dp


class _MemCfg:
    def __init__(self):
        self._d: dict = {}

    def get_param(self, key, default=None):
        return self._d[key] if key in self._d else default

    def set_param(self, key, value):
        self._d[key] = value


class TestDenseProviderMigration(unittest.TestCase):
    def setUp(self):
        self.cfg = _MemCfg()
        self._p = patch(
            "pipeline.block_cross_table.memory_rag.dense_provider.config", self.cfg
        )
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_migration_conflict_emits_audit_and_banner(self):
        self.cfg.set_param(dp.LEGACY_AUTO_LOCAL_EMBED, True)
        self.cfg.set_param(dp.LEGACY_EXTERNAL_ENABLED, True)
        res = dp.migrate_legacy_dense_provider(persist=True)
        self.assertEqual(res.dense_provider, "local")
        self.assertTrue(res.migrated)
        self.assertEqual(res.migration_conflict, "both_enabled→local")
        self.assertTrue(res.show_migration_banner)
        self.assertEqual(
            self.cfg.get_param(dp.PARAM_DENSE_PROVIDER), "local"
        )
        self.assertEqual(
            self.cfg.get_param(dp.PARAM_MIGRATION_CONFLICT), "both_enabled→local"
        )
        self.assertEqual(
            self.cfg.get_param(dp.PARAM_MIGRATED_FROM), dp.MIGRATED_FROM_LABEL
        )
        view = dp.public_dense_provider_view()
        self.assertTrue(view["show_migration_banner"])
        self.assertIn("本地", view["migration_banner_text"] or "")

    def test_migration_banner_acknowledged_clears_flag(self):
        self.cfg.set_param(dp.LEGACY_AUTO_LOCAL_EMBED, True)
        self.cfg.set_param(dp.LEGACY_EXTERNAL_ENABLED, True)
        dp.migrate_legacy_dense_provider(persist=True)
        res = dp.acknowledge_migration_conflict(choice="external")
        self.assertEqual(res.dense_provider, "external")
        self.assertTrue(res.migration_conflict_acknowledged)
        self.assertFalse(res.show_migration_banner)
        self.assertTrue(self.cfg.get_param(dp.PARAM_MIGRATION_ACK))

    def test_migration_non_conflict_no_banner(self):
        self.cfg.set_param(dp.LEGACY_AUTO_LOCAL_EMBED, False)
        self.cfg.set_param(dp.LEGACY_EXTERNAL_ENABLED, True)
        res = dp.migrate_legacy_dense_provider(persist=True)
        self.assertEqual(res.dense_provider, "external")
        self.assertIsNone(res.migration_conflict)
        self.assertFalse(res.show_migration_banner)


import pytest


@pytest.mark.parametrize(
    "case_id,auto_l,ext,expect,conflict",
    [
        ("local_only", True, False, "local", None),
        ("external_only", False, True, "external", None),
        ("both_enabled_conflict_local", True, True, "local", "both_enabled→local"),
        ("both_off", False, False, "off", None),
    ],
    ids=[
        "local_only",
        "external_only",
        "both_enabled_conflict_local",
        "both_off",
    ],
)
def test_dense_provider_legacy_auto_local_embed_migration(
    case_id, auto_l, ext, expect, conflict
):
    cfg = _MemCfg()
    with patch(
        "pipeline.block_cross_table.memory_rag.dense_provider.config", cfg
    ):
        cfg.set_param(dp.LEGACY_AUTO_LOCAL_EMBED, auto_l)
        cfg.set_param(dp.LEGACY_EXTERNAL_ENABLED, ext)
        res = dp.migrate_legacy_dense_provider(persist=True)
        assert res.dense_provider == expect
        assert res.migration_conflict == conflict
        if conflict:
            assert cfg.get_param(dp.PARAM_MIGRATION_CONFLICT) == conflict


if __name__ == "__main__":
    unittest.main()
