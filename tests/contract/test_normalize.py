# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from pipeline.synth_contract.normalize import (
    apply_ops,
    compare_with_null_policy,
    is_null,
    normalize_key,
    validate_normalize_spec,
)
from pipeline.synth_contract.registry import load_contract


class TestNormalize(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cv = load_contract("v1.0.0")

    def test_ops(self):
        allowed = set(self.cv.data["normalize_ops"])
        self.assertEqual(
            apply_ops("  Ab C  ", ["trim", "casefold", "collapse_ws"], allowed),
            "ab c",
        )

    def test_null_tokens(self):
        tokens = self.cv.data["null_tokens_default"]
        self.assertTrue(is_null("", tokens))
        self.assertTrue(is_null("N/A", tokens))
        self.assertFalse(is_null("x", tokens))

    def test_null_policy(self):
        tokens = ["", "na"]
        self.assertTrue(
            compare_with_null_policy(None, None, "eq", "eq_null", tokens)
        )
        self.assertFalse(
            compare_with_null_policy(None, "a", "eq", "eq_null", tokens)
        )
        self.assertIsNone(
            compare_with_null_policy(None, "a", "eq", "reject_null", tokens)
        )
        self.assertFalse(
            compare_with_null_policy(None, "a", "eq", "exclude_null", tokens)
        )

    def test_spec_validation(self):
        spec = {
            "spec_id": "n1",
            "scope": {"file_id": "f", "sheet": "S", "region": "r0"},
            "ops": ["trim", "casefold"],
            "null_policy": "eq_null",
        }
        self.assertTrue(validate_normalize_spec(self.cv, spec).ok)
        bad = dict(spec, ops=["invented_op"])
        self.assertFalse(validate_normalize_spec(self.cv, bad).ok)

    def test_normalize_key(self):
        # executor-safe: string ops only; no is_null side effects
        self.assertEqual(
            normalize_key("  Foo ", ["trim", "casefold"], self.cv),
            "foo",
        )
        self.assertEqual(normalize_key("—", ["trim"], self.cv), "—")


if __name__ == "__main__":
    unittest.main()
