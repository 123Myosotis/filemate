# -*- coding: utf-8 -*-
"""P0：table_normalizer（表格规范化 / 二次制表）契约回归。"""

from __future__ import annotations

from pipeline.stage1_parser import file_parser
from pipeline.stage3_processor.table_normalizer import (
    consistency_issues,
    normalize,
    normalize_second_pass,
)


def _t(table: dict) -> dict:
    return normalize(dict(table))


def test_tidy_csv_style_trims_values():
    r = _t({
        "name": "设备表", "columns": ["设备", "负责人"],
        "rows": [{"设备": "A", "负责人": " 张三 "}],
    })
    assert r["shape"] == "tidy"
    assert r["recognized"] is True
    assert r["columns"] == ["设备", "负责人"]
    assert r["rows"][0]["负责人"] == "张三"  # 值被 trim


def test_title_row_then_header_is_promoted():
    r = _t({
        "name": "台账", "grid": [
            ["2024年度设备台账", "", "", ""],
            ["设备", "负责人", "状态", "备注"],
            ["A", "张三", "运行", "x"],
        ], "merges": [(0, 0, 0, 3)],
    })
    assert r["shape"] == "tidy"
    assert r["recognized"] is True
    assert r["columns"] == ["设备", "负责人", "状态", "备注"]
    assert r["rows"] == [{"设备": "A", "负责人": "张三", "状态": "运行", "备注": "x"}]


def test_duplicate_columns_deduped_and_empty_column_dropped():
    r = _t({
        "name": "t", "columns": ["设备", "设备", "", "备注"],
        "rows": [{"设备": "A", "备注": "x"}],
    })
    # 空列整列为空 → 被去除；重名「设备」→「设备_2」
    assert r["columns"] == ["设备", "设备_2", "备注"]


def test_headerless_numeric_first_row_kept_raw():
    r = _t({"name": "裸表", "grid": [["1", "2", "3"], ["4", "5", "6"]], "merges": []})
    assert r["shape"] == "headerless"
    assert r["recognized"] is False
    assert r["columns"] == ["列1", "列2", "列3"]
    assert len(r["rows"]) == 2  # 首行当数据保留


def test_multi_header_flattened_to_compound_names():
    r = _t({
        "name": "两级表", "grid": [
            ["变电站", "变电站", "线路", "线路"],
            ["负责人", "值班人", "负责人", "值班人"],
            ["张三", "李四", "王五", "赵六"],
        ], "merges": [(0, 0, 0, 1), (0, 2, 0, 3)],
    })
    assert r["shape"] == "multi_header"
    assert r["recognized"] is True
    assert r["columns"] == ["变电站·负责人", "变电站·值班人", "线路·负责人", "线路·值班人"]
    assert r["rows"][0]["变电站·负责人"] == "张三"


def test_matrix_flattened_to_long():
    grid = [
        ["", "华东", "华东", "华东", "华北", "华北", "华北"],
        ["", "站点1", "站点2", "站点3", "站点4", "站点5", "站点6"],
        ["负责人", "张三", "李四", "王五", "赵六", "钱七", "孙八"],
        ["值班人", "周九", "吴十", "郑一", "王二", "李三", "张四"],
    ]
    r = _t({"name": "值班矩阵", "grid": grid, "merges": [(0, 1, 0, 3), (0, 4, 0, 6)]})
    assert r["shape"] == "wide_matrix"
    assert r["recognized"] is True
    assert len(r["rows"]) >= 5
    assert r["columns"][-1] == "条目"


def test_empty_table():
    r = _t({"name": "空", "columns": [], "rows": []})
    assert r["shape"] == "empty"
    assert r["recognized"] is False
    assert r["columns"] == []
    assert r["rows"] == []


def test_parse_csv_attaches_grid_and_normalizes(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("设备,负责人\nA,张三\n", encoding="utf-8")
    parsed = file_parser.parse_file(str(p))
    assert parsed["kind"] == "table"
    assert parsed["table"]["grid"] == [["设备", "负责人"], ["A", "张三"]]
    r = normalize(parsed["table"])
    assert r["shape"] == "tidy"
    assert r["recognized"] is True
    assert r["columns"] == ["设备", "负责人"]
    assert r["rows"] == [{"设备": "A", "负责人": "张三"}]


def test_second_pass_picks_later_header():
    """第一遍因首行数字带判 headerless；第二遍把第 1 行文本当表头。"""
    r = normalize_second_pass({
        "name": "t",
        "grid": [["2024", "2025"], ["设备", "负责人"], ["A", "张三"]],
        "merges": [],
    })
    assert r["recognized"] is True
    assert r["shape"] == "tidy"
    assert r["columns"] == ["设备", "负责人"]
    assert r["rows"] == [{"设备": "A", "负责人": "张三"}]


def test_second_pass_header_at_row_2():
    r = normalize_second_pass({
        "name": "t",
        "grid": [["1", "2"], ["3", "4"], ["设备", "负责人"], ["A", "张三"]],
        "merges": [],
    })
    assert r["recognized"] is True
    assert r["columns"] == ["设备", "负责人"]


def test_second_pass_pure_numbers_stays_headerless():
    r = normalize_second_pass({
        "name": "t",
        "grid": [["1", "2", "3"], ["4", "5", "6"]],
        "merges": [],
    })
    assert r["recognized"] is False
    assert r["shape"] == "headerless"


def test_second_pass_tidy_passthrough():
    r = normalize_second_pass({
        "name": "t",
        "grid": [["设备", "负责人"], ["A", "张三"]],
        "merges": [],
    })
    assert r["recognized"] is True
    assert r["columns"] == ["设备", "负责人"]


def test_consistency_tidy_clean():
    assert consistency_issues({
        "columns": ["设备", "负责人"],
        "rows": [{"设备": "A", "负责人": "张三"}],
        "recognized": True,
    }) == []


def test_consistency_placeholder_recognized():
    issues = consistency_issues({
        "columns": ["列1", "列2"],
        "rows": [{"列1": "A", "列2": "B"}],
        "recognized": True,
    })
    assert any("占位" in x for x in issues)


def test_consistency_empty_and_dup_columns():
    issues = consistency_issues({
        "columns": ["设备", "", "设备"],
        "rows": [{"设备": "A", "": "B"}],
        "recognized": True,
    })
    assert any("空列名" in x for x in issues)
    assert any("重复列名" in x for x in issues)


def test_consistency_single_column():
    issues = consistency_issues({
        "columns": ["设备"],
        "rows": [{"设备": "A"}],
        "recognized": True,
    })
    assert any("1 列" in x for x in issues)
