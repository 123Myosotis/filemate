# -*- coding: utf-8 -*-
"""B1: normalize_match_text."""

from __future__ import annotations

import pytest

from pipeline.block_cross_table.api import normalize_match_text
from pipeline.block_cross_table.errors import CrossTableError


def test_empty_string():
    assert normalize_match_text("", ["trim", "casefold", "width_fold"]) == ""


def test_none_returns_empty():
    assert normalize_match_text(None, ["trim"]) == ""  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [1, [], {}, 1.5])
def test_non_str_raises_input_invalid(bad):
    with pytest.raises(CrossTableError) as ei:
        normalize_match_text(bad, ["trim"])  # type: ignore[arg-type]
    assert ei.value.code == "cross_table_input_invalid"


def test_ops_trim_only():
    assert normalize_match_text("  x  y  ", ["trim"]) == "x  y"


def test_ops_casefold_only():
    assert normalize_match_text("AbC", ["casefold"]) == "abc"


def test_ops_width_fold_only():
    # fullwidth latin small a → halfwidth
    assert normalize_match_text("\uff41", ["width_fold"]) == "a"


def test_ops_all_in_order():
    assert (
        normalize_match_text("  \uff21b  ", ["trim", "casefold", "width_fold"])
        == "ab"
    )


def test_ops_order_is_sequential():
    s = "  X  "
    assert normalize_match_text(s, ["casefold", "trim"]) == normalize_match_text(
        normalize_match_text(s, ["casefold"]), ["trim"]
    )
    assert normalize_match_text(s, ["trim", "casefold"]) == normalize_match_text(
        normalize_match_text(s, ["trim"]), ["casefold"]
    )
    # width_fold then trim vs width_fold alone — composition/order matters
    spaced = normalize_match_text("\u3000x\u3000", ["width_fold"])
    folded_then_trim = normalize_match_text("\u3000x\u3000", ["width_fold", "trim"])
    assert spaced == " x "
    assert folded_then_trim == "x"
    assert spaced != folded_then_trim


def test_ops_empty_returns_unchanged():
    assert normalize_match_text("  Ab  ", []) == "  Ab  "


def test_unknown_op_raises_contract_invalid():
    with pytest.raises(CrossTableError) as ei:
        normalize_match_text("x", ["trim", "not_an_op"])  # type: ignore[list-item]
    assert ei.value.code == "cross_table_contract_invalid"


def test_no_extra_punctuation_strip():
    assert normalize_match_text("a,b", ["trim", "casefold", "width_fold"]) == "a,b"


def test_no_truncation():
    long_s = "x" * 5000
    assert normalize_match_text(long_s, ["trim", "casefold"]) == long_s
