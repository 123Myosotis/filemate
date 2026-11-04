# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path

from pipeline.synth_contract.report_render import render_report


def _summary():
    return {
        "schema_version": "d3-summary-1",
        "contract_version": "v1.1.0",
        "contract_hash": "abc",
        "triage": "simple",
        "fired_signals": [],
        "primary_table": "t",
        "ops_present": ["project"],
        "row_count_out": 1,
        "column_count_out": 1,
        "sample_ref": {"sample_coords": ["row:9"]},
        "evidence_path": "evidence/v1.1.0/d4unit000001.json",
        "human_text": "unit",
    }


class TestReportRender(unittest.TestCase):
    def test_return_keys(self):
        dest = Path(tempfile.mkdtemp(prefix="d4unit"))
        got = render_report(_summary(), {}, dest_root=str(dest))
        self.assertEqual(
            tuple(got),
            ("report_status", "report_html_path", "report_text_path", "evidence_path"),
        )
        self.assertEqual(got["report_status"], "written")

    def test_meta_failure_keeps_bodies(self):
        dest = Path(tempfile.mkdtemp(prefix="d4meta"))
        got = render_report(_summary(), {}, dest_root=str(dest), fail_parts=["meta"])
        self.assertEqual(got["report_status"], "write_failed")
        self.assertIsNotNone(got["report_html_path"])
        self.assertIsNotNone(got["report_text_path"])
        self.assertTrue((dest / got["report_html_path"]).is_file())
        self.assertTrue((dest / got["report_text_path"]).is_file())
        self.assertFalse((dest / "report" / "v1.1.0" / "d4unit000001.meta.json").exists())

    def test_html_failure_keeps_text(self):
        dest = Path(tempfile.mkdtemp(prefix="d4html"))
        got = render_report(_summary(), {}, dest_root=str(dest), fail_parts=["html"])
        self.assertEqual(got["report_status"], "write_failed")
        self.assertIsNone(got["report_html_path"])
        self.assertIsNotNone(got["report_text_path"])
        self.assertTrue((dest / got["report_text_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
