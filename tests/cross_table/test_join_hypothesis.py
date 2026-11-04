# -*- coding: utf-8 -*-
"""Join hypothesis proposal, rank, questions, and orchestrate gates."""

from __future__ import annotations

import pytest

from pipeline.block_cross_table.api import bind_chat_json
from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.join_hypothesis import (
    build_join_questions,
    propose_join_hypotheses,
    rank_hypotheses,
    validate_join_hypothesis,
)
from pipeline.block_cross_table.orchestrate import run_cross_table


def _ref(fid, parent, block_id=0):
    return {
        "field_id": fid,
        "block_id": block_id,
        "parent_table_id": parent,
        "role": "column",
        "target_name": "A",
        "source_labels": ["A"],
        "source_label": "A",
        "in_dimensions": False,
        "value_samples": [],
    }


def _hyp(hid, left="f0", right="f1", join_type="left", rationale="because"):
    return {
        "hypothesis_id": hid,
        "left": {"parent_table_id": "t0", "block_id": 0, "field_id": left},
        "right": {"parent_table_id": "t1", "block_id": 1, "field_id": right},
        "join_type": join_type,
        "rationale": rationale,
    }


def test_numeric_hypothesis_id_stays_unique():
    refs = [_ref("f0", "t0"), _ref("f1", "t1", 1)]

    def chat(_prompt):
        row = _hyp("1")
        row["hypothesis_id"] = 1
        return {"hypotheses": [row]}

    bind_chat_json(chat)
    rows = propose_join_hypotheses("bind", refs, [], {})
    assert rows[0]["hypothesis_id"] == "1"
    assert validate_join_hypothesis(rows, refs)["needs_retry"] is False


def test_similar_name_groups_uses_semantic_match():
    from pipeline.block_cross_table.join_hypothesis import _similar_name_groups

    refs = [
        {"field_id": "f0", "parent_table_id": "t0",
         "target_name": "站点", "source_label": "站点"},
        {"field_id": "f1", "parent_table_id": "t1",
         "target_name": "站点名称", "source_label": "站点名称"},
        {"field_id": "f2", "parent_table_id": "t2",
         "target_name": "负责人", "source_label": "负责人"},
    ]
    groups = _similar_name_groups(refs)
    assert any(g["left"] == ["t0", "f0"] and g["right"] == ["t1", "f1"] for g in groups)
    assert not any({tuple(g["left"]), tuple(g["right"])}
                   == {("t0", "f0"), ("t2", "f2")} for g in groups)


def test_propose_calls_model_once():
    calls = {"n": 0}

    def chat(_prompt):
        calls["n"] += 1
        return {"hypotheses": []}

    bind_chat_json(chat)
    out = propose_join_hypotheses("bind", [_ref("f0", "t0"), _ref("f1", "t1")], [], {})
    assert out == []
    assert calls["n"] == 1


def test_validate_duplicate_rationale_same_table_and_join_type():
    refs = [_ref("f0", "t0"), _ref("f1", "t1")]
    dup = validate_join_hypothesis([_hyp("h"), _hyp("h")], refs)
    assert dup["needs_retry"] is True
    assert "duplicate_hypothesis_id" in dup["reason_codes"]
    empty = validate_join_hypothesis([_hyp("h", rationale="")], refs)
    assert "empty_rationale" in empty["reason_codes"]
    same = _hyp("h")
    same["right"] = {"parent_table_id": "t0", "block_id": 0, "field_id": "f0"}
    same_table = validate_join_hypothesis([same], refs)
    assert "same_table" in same_table["reason_codes"]
    bad = validate_join_hypothesis([_hyp("h", join_type="sideways")], refs)
    assert "invalid_join_type" in bad["reason_codes"]


def test_rank_orders_by_overlap_and_keeps_single():
    samples = {"f0": ["a", "b"], "f1": ["a"], "f2": ["z"]}
    ranked = rank_hypotheses([_hyp("low", right="f2"), _hyp("high", right="f1")], samples, 1)
    assert len(ranked) == 1
    assert ranked[0]["hypothesis_id"] == "high"
    assert rank_hypotheses([], samples, 3) == []
    single = rank_hypotheses([_hyp("only")], {"f0": [], "f1": []}, 3)
    assert len(single) == 1
    assert "coverage_ratio" in single[0]


def test_questions_keep_rationale_and_ratio():
    ranked = [_hyp("h")]
    ranked[0]["coverage_ratio"] = 0.5
    ranked[0]["left_label"] = "L"
    ranked[0]["right_label"] = "R"
    questions = build_join_questions(ranked, 3)
    assert questions[0]["rationale"] == "because"
    assert questions[0]["coverage_ratio"] == 0.5
    assert questions[-1]["hypothesis_id"] is None


def test_ai_confirm_required(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "config.get_param",
        lambda key, default=None: True if key == "cross_table.enabled" else default,
    )
    with pytest.raises(CrossTableError) as ei:
        run_cross_table(
            {
                "executable": [
                    {"target_name": "A", "sources_resolved": [{"block_id": 0, "row": 1, "col": 0, "cell_text": "A"}]},
                    {"target_name": "A", "sources_resolved": [{"block_id": 1, "row": 1, "col": 0, "cell_text": "A"}]},
                ],
                "block_plan": {"blocks": [{"block_id": 0, "parent_table_ids": ["t0"]}, {"block_id": 1, "parent_table_ids": ["t1"]}]},
                "tables_by_id": {"t0": {"grid": [["h"], ["a"]]}, "t1": {"grid": [["h"], ["a"]]}},
            },
            "bind",
            ai_confirm=None,
            clock=lambda: "t",
            session_dir=str(tmp_path),
        )
    assert ei.value.code == "cross_table_input_invalid"


def test_get_param_reads_contract_fields():
    import config

    assert config.get_param("cross_table.rank_top_n") == 3
    assert config.get_param("cross_table.proposal_max_calls_per_kind") == 1
    assert config.get_param("cross_table.user_retry_max_rounds") == 3
    assert isinstance(config.get_param("cross_table.trace_dir"), str)
    assert "{kind}" in config.get_param("cross_table.trace_filename_pattern")
