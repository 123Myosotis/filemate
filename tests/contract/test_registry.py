# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from pipeline.synth_contract.registry import (
    list_versions,
    load_contract,
    resolve_contract_dir,
    verify_contract_hash,
)


class TestRegistry(unittest.TestCase):
    def test_load_v1(self):
        cv = load_contract("v1.0.0")
        self.assertEqual(cv.version, "v1.0.0")
        self.assertTrue(cv.file_hash)
        self.assertIn("binding", cv.enum_values("global_judgment_type"))
        self.assertTrue(cv.accepts_enum("based_on", "full"))
        self.assertFalse(cv.accepts_enum("based_on", "not_a_real_value"))

    def test_hash_verify(self):
        cv = load_contract("v1.0.0")
        self.assertTrue(verify_contract_hash(cv, cv.file_hash))
        self.assertFalse(verify_contract_hash(cv, "deadbeef"))

    def test_hash_mismatch_raises(self):
        with self.assertRaises(ValueError):
            load_contract("v1.0.0", expected_hash="0" * 64)

    def test_list_versions(self):
        vers = list_versions()
        self.assertIn("v1.0.0", vers)

    def test_confidence_threshold_bucket(self):
        cv = load_contract("v1.0.0")
        thr = cv.confidence_threshold("binding")
        self.assertIsNotNone(thr)
        self.assertGreater(thr, 0.0)
        self.assertLess(thr, 1.0)
        self.assertTrue(cv.forbid_degenerate_confidence())

    def test_resolve_dir(self):
        d = resolve_contract_dir()
        self.assertTrue((d / "versions").is_dir())


if __name__ == "__main__":
    unittest.main()
