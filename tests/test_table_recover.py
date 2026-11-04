# -*- coding: utf-8 -*-
"""P2：table_recover（结构恢复 / 五段式兜底）契约回归。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pipeline.stage3_processor import table_normalizer, table_recover


@pytest.fixture(autouse=True)
def _enable_recover(monkeypatch):
    """默认 llm_recover=False（本地优先）；这些用例显式开启以测恢复逻辑。"""
    monkeypatch.setattr(table_recover, "_enabled", lambda: True)


def _five() -> dict:
    return {
        "speculation": "推测",
        "evidence": "依据",
        "what_might_be_wrong": "可能问题",
        "next_step": "下一步",
        "please_confirm_or_correct": "请确认或纠正",
    }


def _table(grid, name="t"):
    return {"name": name, "grid": grid, "merges": []}


def test_recover_no_api_key():
    rec = table_recover.recover(_table([["1", "2"], ["3", "4"]]), api_key="")
    assert rec == {"status": "unavailable", "reason": "no_api_key"}


def test_recover_recovered():
    table = _table([["1", "2", "3"], ["4", "5", "6"]])
    with patch(
        "pipeline.stage3_processor.table_recover._chat_json",
        return_value={"status": "recovered", "header_row": 0,
                      "columns": ["设备", "负责人", "状态"]},
    ):
        rec = table_recover.recover(table, api_key="k")
    assert rec["status"] == "recovered"
    out = rec["table"]
    assert out["recognized"] is True
    assert out["shape"] == "recovered"
    assert out["columns"] == ["设备", "负责人", "状态"]
    assert out["rows"] == [{"设备": "4", "负责人": "5", "状态": "6"}]


def test_recover_needs_user():
    table = _table([["1", "2"], ["3", "4"]])
    with patch(
        "pipeline.stage3_processor.table_recover._chat_json",
        return_value={"status": "needs_user", **_five()},
    ):
        rec = table_recover.recover(table, api_key="k")
    assert rec["status"] == "needs_user"
    assert rec["five_part"] == _five()


def test_recover_incomplete_five_part():
    table = _table([["1", "2"], ["3", "4"]])
    with patch(
        "pipeline.stage3_processor.table_recover._chat_json",
        return_value={"status": "needs_user", "speculation": "s"},
    ):
        rec = table_recover.recover(table, api_key="k")
    assert rec == {"status": "unavailable", "reason": "five_part_incomplete"}


def test_recover_bad_columns_count():
    table = _table([["1", "2", "3"], ["4", "5", "6"]])
    with patch(
        "pipeline.stage3_processor.table_recover._chat_json",
        return_value={"status": "recovered", "header_row": 0, "columns": ["a", "b"]},
    ):
        rec = table_recover.recover(table, api_key="k")
    assert rec == {"status": "unavailable", "reason": "bad_columns_count"}


def test_recover_bad_header_row():
    table = _table([["1", "2"], ["3", "4"]])
    with patch(
        "pipeline.stage3_processor.table_recover._chat_json",
        return_value={"status": "recovered", "header_row": 9, "columns": ["a", "b"]},
    ):
        rec = table_recover.recover(table, api_key="k")
    assert rec == {"status": "unavailable", "reason": "bad_header_row"}


def test_recover_bad_status():
    table = _table([["1", "2"], ["3", "4"]])
    with patch(
        "pipeline.stage3_processor.table_recover._chat_json",
        return_value={"status": "whatever"},
    ):
        rec = table_recover.recover(table, api_key="k")
    assert rec == {"status": "unavailable", "reason": "bad_status"}


def test_rebuild_from_header():
    table = {
        "name": "台账",
        "grid": [
            ["2024年度设备台账", "", "", ""],
            ["设备", "负责人", "状态", "备注"],
            ["A", "张三", "运行", "x"],
        ],
        "merges": [(0, 0, 0, 3)],
    }
    out = table_normalizer.rebuild_from_header(
        table, 1, ["设备", "负责人", "状态", "备注"])
    assert out["shape"] == "recovered"
    assert out["recognized"] is True
    assert out["columns"] == ["设备", "负责人", "状态", "备注"]
    assert out["rows"] == [{"设备": "A", "负责人": "张三", "状态": "运行", "备注": "x"}]
