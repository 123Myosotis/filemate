# -*- coding: utf-8 -*-
"""QP prompt: fill_summary / comments appended mechanically (no live AI)."""
from __future__ import annotations

import json

from pipeline.block_cross_table.query_engine.qp_ai import (
    apply_portrait_attachments_to_tables,
)


def _tables_base():
    return [{"table_id": "t1", "columns": ["a"], "column_details": [], "nrows": 1}]


def test_prompt_body_includes_fill_when_present():
    portraits = [
        {
            "table_id": "t1",
            "fill_summary": {
                "color_count": 3,
                "color_distribution": {"FF0000": 1, "92D050": 2, "FFC000": 3},
            },
        }
    ]
    out = apply_portrait_attachments_to_tables(_tables_base(), portraits)
    blob = json.dumps(out, ensure_ascii=False)
    assert "fill_summary" in out[0]
    assert out[0]["fill_summary"]["color_count"] == 3
    assert "FF0000" in blob and "92D050" in blob and "FFC000" in blob


def test_prompt_body_includes_comments_when_present():
    portraits = [
        {
            "table_id": "t1",
            "comments": [
                {"row": 1, "col": 0, "text": "fixture_note: verify_passthrough", "len": 30}
            ],
        }
    ]
    out = apply_portrait_attachments_to_tables(_tables_base(), portraits)
    c0 = out[0]["comments"][0]
    assert c0["row"] == 1 and c0["col"] == 0
    assert c0["text_head"].startswith("fixture_note")
    assert "text_head" in c0 and "text_len" in c0


def test_prompt_body_no_attachment_when_absent():
    portraits = [{"table_id": "t1", "structure_status": "layers_recognized"}]
    out = apply_portrait_attachments_to_tables(_tables_base(), portraits)
    blob = json.dumps(out, ensure_ascii=False)
    assert "fill_summary" not in out[0]
    assert "comments" not in out[0]
    assert "fill_summary" not in blob
    assert "comments" not in blob


def test_1b_prompt_includes_layout_and_anchors():
    portraits = [
        {
            "table_id": "t1",
            "layout": "matrix",
            "merge_anchors": [
                {
                    "row": 0,
                    "col": 0,
                    "span_rows": 1,
                    "span_cols": 4,
                    "is_header_merge": True,
                }
            ],
            "synthetic_column_count": 25,
            "header_rows_candidate": 2,
        }
    ]
    out = apply_portrait_attachments_to_tables(_tables_base(), portraits)
    blob = json.dumps(out, ensure_ascii=False)
    assert out[0]["layout"] == "matrix"
    assert out[0]["synthetic_column_count"] == 25
    assert "merge_anchors" in blob
    assert '"span_cols": 4' in blob or '"span_cols":4' in blob
