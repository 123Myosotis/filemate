# -*- coding: utf-8 -*-
"""B2: copy_executable_with_bindings."""

from __future__ import annotations

from pipeline.block_cross_table.api import copy_executable_with_bindings


def _item():
    source = {"block_id": 0, "field_id": "f0", "col": 0, "cell_text": "A"}
    return {
        "target_name": "A",
        "sources_resolved": [source],
    }, source


def _binding(hypothesis_id, status, field_id="f0"):
    return {
        "hypothesis_id": hypothesis_id,
        "decision_status": status,
        "member_refs": [
            {"field_id": field_id, "block_id": 0, "parent_table_id": "t0"}
        ],
    }


def test_empty():
    assert copy_executable_with_bindings([], []) == []


def test_confirmed_writes_id():
    item, _source = _item()
    copied = copy_executable_with_bindings([item], [_binding("c0", "confirmed")])
    assert copied[0]["join_hypothesis_ids"] == ["c0"]


def test_proposed_not_written():
    item, _source = _item()
    copied = copy_executable_with_bindings([item], [_binding("c0", "proposed")])
    assert copied[0]["join_hypothesis_ids"] == []


def test_two_confirmed():
    item, _source = _item()
    copied = copy_executable_with_bindings(
        [item],
        [_binding("c0", "confirmed"), _binding("c1", "confirmed")],
    )
    assert copied[0]["join_hypothesis_ids"] == ["c0", "c1"]


def test_unreferenced_empty_ids():
    item, _source = _item()
    copied = copy_executable_with_bindings(
        [item], [_binding("c0", "confirmed", field_id="f9")]
    )
    assert copied[0]["join_hypothesis_ids"] == []


def test_original_item_identity():
    item, _source = _item()
    copied = copy_executable_with_bindings([item], [_binding("c0", "confirmed")])
    assert id(copied[0]) != id(item)
    assert "join_hypothesis_ids" not in item


def test_source_element_identity():
    item, source = _item()
    copied = copy_executable_with_bindings([item], [_binding("c0", "confirmed")])
    assert id(copied[0]["sources_resolved"][0]) == id(source)


def test_sources_list_is_new():
    item, _source = _item()
    copied = copy_executable_with_bindings([item], [_binding("c0", "confirmed")])
    assert id(copied[0]["sources_resolved"]) != id(item["sources_resolved"])


def test_new_item_is_new_dict():
    item, _source = _item()
    copied = copy_executable_with_bindings([item], [])
    assert id(copied[0]) != id(item)
    assert copied[0]["target_name"] == item["target_name"]
