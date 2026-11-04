# -*- coding: utf-8 -*-
"""S6: dense_provider WebUI payload visibility + persist other side + masked echo."""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from pipeline.block_cross_table.memory_rag import dense_provider as dp
from pipeline.block_cross_table.memory_rag.external_embedding_config import (
    PARAM_API_KEY,
    PARAM_BASE_URL,
    PARAM_MODEL,
    PARAM_WEIGHT,
    save_external_embedding_patch,
)
from pipeline.block_cross_table.memory_rag.merge_settings import (
    PARAM_EMBEDDED_MODEL_PATH,
    save_merge_settings_patch,
)


class _MemCfg:
    def __init__(self):
        self.params: dict = {}

    def get_param(self, key, default=None):
        return self.params[key] if key in self.params else default

    def set_param(self, key, value):
        self.params[key] = value

    def get_api_key(self):
        return self.params.get("api.key") or ""

    def set_api_key(self, key):
        self.params["api.key"] = key


def _payload_with_cfg(cfg: _MemCfg) -> dict:
    with patch(
        "pipeline.block_cross_table.memory_rag.dense_provider.config", cfg
    ), patch(
        "pipeline.block_cross_table.memory_rag.external_embedding_config.config", cfg
    ), patch(
        "pipeline.block_cross_table.memory_rag.merge_settings.config", cfg
    ), patch.dict(os.environ, {}, clear=False):
        for k in (
            "MEMORY_RAG_EXTERNAL_EMBEDDING_API_KEY",
            "SILICONFLOW_API_KEY",
        ):
            os.environ.pop(k, None)
        from web.memory_correction import app as webapp

        conn = MagicMock()
        return webapp._settings_payload(conn)


class TestDenseProviderWebUI(unittest.TestCase):
    def setUp(self):
        self.cfg = _MemCfg()
        self.cfg.set_param(PARAM_BASE_URL, "https://api.example.com/v1")
        self.cfg.set_param(PARAM_MODEL, "vendor/model-x")
        self.cfg.set_param(PARAM_WEIGHT, 0.3)
        self.cfg.set_param(PARAM_API_KEY, "ABCDEFGHIJKLMNOPQRSTUVWX")
        self.cfg.set_param(PARAM_EMBEDDED_MODEL_PATH, "C:/models/local-bge")
        self.cfg.set_param(dp.PARAM_DENSE_PROVIDER, "off")

    def test_webui_dense_provider_off_hides_both(self):
        self.cfg.set_param(dp.PARAM_DENSE_PROVIDER, "off")
        payload = _payload_with_cfg(self.cfg)
        self.assertEqual(payload["dense_provider"], "off")
        self.assertTrue(payload["ui"]["show_off_hint"])
        self.assertFalse(payload["ui"]["show_local_path"])
        self.assertFalse(payload["ui"]["show_external_fields"])

    def test_webui_dense_provider_local_shows_local_only(self):
        self.cfg.set_param(dp.PARAM_DENSE_PROVIDER, "local")
        payload = _payload_with_cfg(self.cfg)
        self.assertEqual(payload["dense_provider"], "local")
        self.assertTrue(payload["ui"]["show_local_path"])
        self.assertFalse(payload["ui"]["show_external_fields"])
        self.assertFalse(payload["ui"]["show_off_hint"])
        self.assertEqual(payload["embedded_model_path"], "C:/models/local-bge")

    def test_webui_dense_provider_external_shows_external_only(self):
        self.cfg.set_param(dp.PARAM_DENSE_PROVIDER, "external")
        payload = _payload_with_cfg(self.cfg)
        self.assertEqual(payload["dense_provider"], "external")
        self.assertTrue(payload["ui"]["show_external_fields"])
        self.assertFalse(payload["ui"]["show_local_path"])
        self.assertTrue(payload["api_key_masked"].endswith("QRSTUVWX"))
        self.assertTrue(payload["api_key_masked"].startswith("*"))

    def test_dense_provider_webui_echo_masked_key(self):
        self.cfg.set_param(dp.PARAM_DENSE_PROVIDER, "external")
        payload = _payload_with_cfg(self.cfg)
        masked = payload["api_key_masked"]
        self.assertEqual(masked, ("*" * 16) + "QRSTUVWX")
        self.assertNotIn("ABCDEFGH", masked)

    def test_dense_provider_switch_persists_other_side_config(self):
        with patch(
            "pipeline.block_cross_table.memory_rag.dense_provider.config", self.cfg
        ), patch(
            "pipeline.block_cross_table.memory_rag.external_embedding_config.config",
            self.cfg,
        ), patch(
            "pipeline.block_cross_table.memory_rag.merge_settings.config", self.cfg
        ):
            save_merge_settings_patch({"dense_provider": "local"})
            self.assertEqual(self.cfg.get_param(PARAM_BASE_URL), "https://api.example.com/v1")
            self.assertEqual(self.cfg.get_param(PARAM_API_KEY), "ABCDEFGHIJKLMNOPQRSTUVWX")
            save_merge_settings_patch({"dense_provider": "external"})
            self.assertEqual(
                self.cfg.get_param(PARAM_EMBEDDED_MODEL_PATH), "C:/models/local-bge"
            )
            # switch again; external fields still present
            save_merge_settings_patch({"dense_provider": "off"})
            self.assertEqual(self.cfg.get_param(PARAM_MODEL), "vendor/model-x")
            self.assertEqual(
                self.cfg.get_param(PARAM_EMBEDDED_MODEL_PATH), "C:/models/local-bge"
            )


if __name__ == "__main__":
    unittest.main()
