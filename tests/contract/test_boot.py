# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline.synth_contract.boot import (
    ContractLibraryError,
    assert_contract_library_ready,
    ensure_contract_library,
    reset_boot_for_tests,
)
from pipeline.synth_contract.registry import read_current_pointer


class TestBoot(unittest.TestCase):
    def setUp(self):
        reset_boot_for_tests()

    def tearDown(self):
        reset_boot_for_tests()

    def test_ensure_loads_and_idempotent(self):
        cv1 = ensure_contract_library()
        cv2 = ensure_contract_library()
        self.assertIs(cv1, cv2)
        assert_contract_library_ready()

    def test_assert_without_boot_raises(self):
        with self.assertRaises(ContractLibraryError):
            assert_contract_library_ready()

    def test_current_must_be_pure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "versions").mkdir()
            (root / "versions" / "v1.0.0.json").write_text(
                '{"contract_version":"v1.0.0","enums":{}}', encoding="utf-8"
            )
            (root / "CURRENT").write_text(" v1.0.0\n", encoding="utf-8")
            with self.assertRaises(ContractLibraryError):
                ensure_contract_library(contract_dir=str(root))

    def test_current_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "versions").mkdir()
            (root / "versions" / "v1.0.0.json").write_text(
                '{"contract_version":"v1.0.0","enums":{"a":["x"]}}', encoding="utf-8"
            )
            (root / "CURRENT").write_text("v9.9.9\n", encoding="utf-8")
            with self.assertRaises(ContractLibraryError):
                ensure_contract_library(contract_dir=str(root))

    def test_read_current_pointer_rejects_spaces(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "CURRENT").write_text("v1.0.0 \n", encoding="utf-8")
            with mock.patch(
                "pipeline.synth_contract.registry.resolve_contract_dir",
                return_value=root,
            ):
                with self.assertRaises(ValueError):
                    read_current_pointer()


if __name__ == "__main__":
    unittest.main()
