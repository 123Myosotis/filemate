# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from pipeline.synth_contract.scope import (
    Scope,
    parse_scope,
    scope_covers,
    scopes_disjoint,
)


class TestScope(unittest.TestCase):
    def test_parse_requires_sheet(self):
        self.assertIsNone(parse_scope({"file_id": "f1"}))
        s = parse_scope({"file_id": "f1", "sheet": "S1"})
        self.assertIsNotNone(s)
        self.assertEqual(s.sheet, "S1")

    def test_parse_requires_region_when_asked(self):
        self.assertIsNone(
            parse_scope({"file_id": "f1", "sheet": "S1"}, require_upto="region")
        )
        s = parse_scope(
            {"file_id": "f1", "sheet": "S1", "region": "r0"},
            require_upto="region",
        )
        self.assertEqual(s.region, "r0")

    def test_covers_and_disjoint(self):
        a = Scope("f", "S", "r1")
        b = Scope("f", "S", "r1", "c1")
        c = Scope("f", "S", "r2")
        self.assertTrue(scope_covers(a, b, "region"))
        self.assertFalse(scope_covers(a, c, "region"))
        self.assertTrue(scopes_disjoint(a, c, "region"))
        self.assertFalse(scopes_disjoint(a, b, "region"))

    def test_no_default_invention(self):
        # empty strings must not pass region completeness
        self.assertFalse(
            Scope("f", "S", "").is_complete_upto("region")
        )


if __name__ == "__main__":
    unittest.main()
