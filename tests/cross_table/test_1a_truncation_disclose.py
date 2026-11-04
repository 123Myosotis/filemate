# -*- coding: utf-8 -*-
"""1A truncation disclosure: no silent truncate; facts reach prompt."""
from __future__ import annotations

import json
from pathlib import Path

from openpyxl import Workbook

from pipeline.block_cross_table.query_engine.qp_ai import (
    apply_portrait_attachments_to_tables,
)
from pipeline.block_cross_table.query_engine.table_load import (
    build_minimal_portrait,
    load_flat_xlsx,
)


def _make_n_row_xlsx(path: Path, n: int) -> None:
    wb = Workbook()
    ws = wb.active
    ws.append(["col_a", "col_b"])
    for i in range(n):
        ws.append(["v%d" % i, i])
    wb.save(path)
    wb.close()


def test_load_flat_xlsx_truncates_silently_no_more(tmp_path: Path):
    path = tmp_path / "ten.xlsx"
    _make_n_row_xlsx(path, 10)
    loaded = load_flat_xlsx(path, table_id="t", max_rows=5)
    assert loaded.get("truncated") is True
    assert loaded.get("original_rows") == 10
    assert loaded.get("loaded_rows") == 5
    assert len(loaded.get("rows") or []) == 5


def test_prompt_includes_truncation_fact(tmp_path: Path):
    path = tmp_path / "ten.xlsx"
    _make_n_row_xlsx(path, 10)
    loaded = load_flat_xlsx(path, table_id="t1", max_rows=5)
    port = build_minimal_portrait(loaded, structure_status="layers_recognized")
    assert port.get("truncated") is True
    assert "truncation_fact" in port
    assert "truncated=True (loaded 5/10)" in str(port.get("truncation_fact"))
    summaries = [
        {
            "table_id": "t1",
            "columns": ["col_a"],
            "column_details": [],
            "nrows": 5,
        }
    ]
    out = apply_portrait_attachments_to_tables(summaries, [port])
    blob = json.dumps(out, ensure_ascii=False)
    assert "truncated=True (loaded 5/10)" in blob
