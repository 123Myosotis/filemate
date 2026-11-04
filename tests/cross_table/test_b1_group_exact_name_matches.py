# -*- coding: utf-8 -*-
"""B1: group_exact_name_matches."""

from __future__ import annotations

import pytest

from pipeline.block_cross_table.api import group_exact_name_matches
from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.types import FieldRef

_OPS = ["trim", "casefold", "width_fold"]


def _ref(
    field_id: str,
    parent_table_id: str,
    *,
    target_name: str = "",
    source_labels: list[str] | None = None,
    role: str = "group",
    block_id: int = 0,
) -> FieldRef:
    labels = list(source_labels or [])
    source_label = labels[0] if len(labels) == 1 else ""
    return FieldRef(
        field_id=field_id,
        block_id=block_id,
        parent_table_id=parent_table_id,
        subtable_id=None,
        role=role,
        target_name=target_name,
        source_labels=labels,
        source_label=source_label,
        in_dimensions=False,
        value_samples=[],
    )


def test_two_tables_same_target_name():
    refs = [
        _ref("f0", "t0", target_name="A"),
        _ref("f1", "t1", target_name="A"),
    ]
    groups = group_exact_name_matches(refs, "target_name", _OPS)
    assert len(groups) == 1
    assert groups[0]["matched_on"] == "target_name"
    assert {m["parent_table_id"] for m in groups[0]["member_refs"]} == {"t0", "t1"}


def test_two_tables_same_source_label():
    refs = [
        _ref("f0", "t0", source_labels=["A"]),
        _ref("f1", "t1", source_labels=["A"]),
    ]
    groups = group_exact_name_matches(refs, "source_label", _OPS)
    assert len(groups) == 1
    assert groups[0]["matched_on"] == "source_label"


def test_either_two_groups_not_merged():
    refs = [
        _ref("f0", "t0", target_name="A", source_labels=["B"]),
        _ref("f1", "t1", target_name="A", source_labels=["B"]),
    ]
    groups = group_exact_name_matches(refs, "either", _OPS)
    assert len(groups) == 2
    matched = {g["matched_on"] for g in groups}
    assert matched == {"target_name", "source_label"}


def test_single_table_no_group():
    refs = [
        _ref("f0", "t0", target_name="A"),
        _ref("f1", "t0", target_name="A"),
    ]
    assert group_exact_name_matches(refs, "target_name", _OPS) == []


def test_empty_strings_not_grouped():
    refs = [
        _ref("f0", "t0", target_name=""),
        _ref("f1", "t1", target_name=""),
        _ref("f2", "t0", source_labels=[]),
        _ref("f3", "t1", source_labels=[]),
    ]
    assert group_exact_name_matches(refs, "either", _OPS) == []


def test_multi_source_labels_skip_source_label_branch():
    refs = [
        _ref("f0", "t0", target_name="A", source_labels=["x", "y"]),
        _ref("f1", "t1", target_name="A", source_labels=["x", "y"]),
    ]
    groups = group_exact_name_matches(refs, "either", _OPS)
    assert len(groups) == 1
    assert groups[0]["matched_on"] == "target_name"


def test_three_tables_one_group():
    refs = [
        _ref("f0", "t0", target_name="A"),
        _ref("f1", "t1", target_name="A"),
        _ref("f2", "t2", target_name="A"),
    ]
    groups = group_exact_name_matches(refs, "target_name", _OPS)
    assert len(groups) == 1
    assert len(groups[0]["member_refs"]) == 3


def test_illegal_equality_basis():
    with pytest.raises(CrossTableError) as ei:
        group_exact_name_matches(
            [_ref("f0", "t0", target_name="A")],
            "nope",  # type: ignore[arg-type]
            _OPS,
        )
    assert ei.value.code == "cross_table_contract_invalid"


def test_candidate_id_stable():
    refs = [
        _ref("f0", "t0", target_name="A"),
        _ref("f1", "t1", target_name="A"),
    ]
    a = group_exact_name_matches(refs, "target_name", _OPS)
    b = group_exact_name_matches(refs, "target_name", _OPS)
    assert a[0]["candidate_id"] == b[0]["candidate_id"]


def test_each_group_has_two_parent_tables():
    refs = [
        _ref("f0", "t0", target_name="A"),
        _ref("f1", "t1", target_name="A"),
        _ref("f2", "t0", target_name="B"),
        _ref("f3", "t1", target_name="B"),
    ]
    groups = group_exact_name_matches(refs, "target_name", _OPS)
    assert len(groups) == 2
    for g in groups:
        parents = {m["parent_table_id"] for m in g["member_refs"]}
        assert len(parents) >= 2
