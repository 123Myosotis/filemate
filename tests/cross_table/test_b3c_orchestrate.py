# -*- coding: utf-8 -*-
"""B3/C local + fake-client orchestration."""

from __future__ import annotations

import pytest

from pipeline.block_cross_table.api import (
    assert_r2_keys_unchanged,
    bind_chat_json,
    derive_exact_links,
    validate_ai_proposal,
    write_decision_log,
)
from pipeline.block_cross_table.b3c import replay_decision_log
from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.orchestrate import run_cross_table


def _ref(fid, parent, role="column", block_id=0, subtable_id=None, in_dim=False, target="A"):
    return {
        "field_id": fid,
        "block_id": block_id,
        "parent_table_id": parent,
        "subtable_id": subtable_id,
        "role": role,
        "target_name": target,
        "source_labels": [target],
        "source_label": target,
        "in_dimensions": in_dim,
        "value_samples": [],
    }


def test_validate_invalid_needs_retry_synonym():
    refs = [_ref("f0", "t0"), _ref("f1", "t1")]
    syn = [{
        "candidate_id": "s0",
        "member_refs": [],
        "proposal_status": "invalid",
        "ai_raw": {},
        "decision_status": "proposed",
    }]
    out = validate_ai_proposal([], syn, refs)
    assert out["needs_retry"]["synonym"] is True
    assert out["needs_retry"]["concept"] is False


def test_validate_ambiguous_does_not_retry():
    refs = [_ref("f0", "t0"), _ref("f1", "t1")]
    syn = [{
        "candidate_id": "s0",
        "member_refs": refs,
        "proposal_status": "ambiguous",
        "ai_raw": {},
        "decision_status": "proposed",
    }]
    out = validate_ai_proposal([], syn, refs)
    assert out["needs_retry"]["synonym"] is False
    assert out["ok"] is False
    assert out["questions"]


def test_exact_links_empty_and_missing():
    keys = [
        {"hypothesis_id": "c0", "parent_table_id": "t0", "field_id": "f0", "status": "resolved", "rule": "sole_list"},
        {"hypothesis_id": "c0", "parent_table_id": "t1", "field_id": "f1", "status": "resolved", "rule": "sole_list"},
    ]
    assert derive_exact_links(keys, {"f0": [], "f1": ["x"]}, ["trim"]) == []
    with pytest.raises(CrossTableError) as ei:
        derive_exact_links(keys, {"f0": ["x"]}, ["trim"])
    assert ei.value.code == "cross_table_input_invalid"


def test_log_replay_and_r2(tmp_path):
    log = {"fingerprint": "fp", "field_refs": [_ref("f0", "t0"), _ref("f1", "t1")], "filename": "log.json"}
    written = write_decision_log(log, str(tmp_path))
    assert written["fingerprint"] == "fp"
    assert replay_decision_log(log, "fp")["fingerprint"] == "fp"
    with pytest.raises(CrossTableError) as ei:
        replay_decision_log(log, "other")
    assert ei.value.code == "cross_table_fingerprint_mismatch"
    assert assert_r2_keys_unchanged([["t0", "col:0"]], [["t0", "col:0"]])["ok"] is True
    with pytest.raises(CrossTableError) as ei2:
        assert_r2_keys_unchanged([["t0", "col:0"]], [["t0", "col:1"]])
    assert ei2.value.code == "cross_table_r2_key_mutation_forbidden"


def test_orchestrate_no_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr("config.get_param", lambda key, default=None: True if key == "cross_table.enabled" else default)
    out = run_cross_table(
        {"executable": [], "block_plan": {"blocks": []}, "tables_by_id": {}},
        "u",
        ai_confirm=lambda q: {"reply": "confirm"},
        clock=lambda: "t",
        session_dir=str(tmp_path),
    )
    assert out["no_candidate"] is True
    assert list(tmp_path.iterdir()) == []


def test_orchestrate_retry_limit(tmp_path, monkeypatch):
    def params(key, default=None):
        if key == "cross_table.enabled":
            return True
        if key == "cross_table.proposal_max_calls_per_kind":
            return 1
        if key == "cross_table.name_normalize_ops":
            return ["trim"]
        if key == "cross_table.decision_log_filename":
            return "log.json"
        if key == "cross_table.trace_dir":
            return "diagnostics/trace"
        if key == "cross_table.trace_filename_pattern":
            return "{kind}_{seq}_{ts}.json"
        if key == "cross_table.sample_preview_cap":
            return 4
        if key == "cross_table.rank_top_n":
            return 3
        if key == "cross_table.user_retry_max_rounds":
            return 3
        return default

    monkeypatch.setattr("config.get_param", params)
    monkeypatch.setattr(
        "pipeline.block_cross_table.orchestrate.load_contract",
        lambda: type("C", (), {"version": "v1.2.0", "data": {"cross_table": {
            "equality_basis": "target_name",
            "unmatched_disposition": "retain_unmatched",
            "reserved_long_columns": ["n1", "n2"],
        }}})(),
    )
    calls = {"n": 0}

    def chat(_prompt):
        calls["n"] += 1
        return None

    bind_chat_json(chat)
    exe = [{
        "target_name": "A",
        "sources_resolved": [{"block_id": 0, "row": 1, "col": 0, "cell_text": "A"}],
    }, {
        "target_name": "A",
        "sources_resolved": [{"block_id": 1, "row": 1, "col": 0, "cell_text": "A"}],
    }]
    plan = {"blocks": [
        {"block_id": 0, "parent_table_ids": ["t0"]},
        {"block_id": 1, "parent_table_ids": ["t1"]},
    ]}
    with pytest.raises(CrossTableError) as ei:
        run_cross_table(
            {"executable": exe, "block_plan": plan, "tables_by_id": {"t0": {"grid": [["h"], ["x"]]}, "t1": {"grid": [["h"], ["x"]]}}},
            "u",
            ai_confirm=lambda q: {"reply": "reject", "reply_text": "", "selected_field_id": None},
            clock=lambda: "t",
            session_dir=str(tmp_path),
        )
    assert ei.value.code == "cross_table_proposal_invalid"
    assert calls["n"] == 1


def _contract():
    return type("C", (), {"version": "v1.2.0", "data": {"cross_table": {
        "equality_basis": "target_name",
        "unmatched_disposition": "retain_unmatched",
        "reserved_long_columns": ["n1", "n2"],
    }}})()


def _params(key, default=None):
    if key == "cross_table.enabled":
        return True
    if key == "cross_table.proposal_max_calls_per_kind":
        return 1
    if key == "cross_table.name_normalize_ops":
        return ["trim"]
    if key == "cross_table.decision_log_filename":
        return "log.json"
    return default


def _bundle():
    exe = [{
        "target_name": "A",
        "sources_resolved": [{"block_id": 0, "row": 1, "col": 0, "cell_text": "A"}],
    }, {
        "target_name": "A",
        "sources_resolved": [{"block_id": 1, "row": 1, "col": 0, "cell_text": "A"}],
    }]
    plan = {"blocks": [
        {"block_id": 0, "parent_table_ids": ["t0"]},
        {"block_id": 1, "parent_table_ids": ["t1"]},
    ]}
    tables = {
        "t0": {"table_id": "t0", "block_id": 0, "grid": [["h"], ["x"], ["y"]]},
        "t1": {"table_id": "t1", "block_id": 1, "grid": [["h"], ["x"]]},
    }
    return {"executable": exe, "block_plan": plan, "tables_by_id": tables}


def test_replay_skips_model(tmp_path, monkeypatch):
    monkeypatch.setattr("config.get_param", _params)
    monkeypatch.setattr("pipeline.block_cross_table.orchestrate.load_contract", _contract)
    bind_chat_json(lambda _prompt: (_ for _ in ()).throw(AssertionError("model")))
    prior = {
        "fingerprint": "",
        "field_refs": [],
        "concept_bindings": [],
        "questions": [],
        "replies": [],
        "outputs": [],
    }
    from pipeline.block_cross_table.b3c import fingerprint_of
    fp = fingerprint_of(
        [
            {"parent_table_id": "t0", "field_id": "col:t0:0", "role": "column", "target_name": "A", "source_label": "A", "source_labels": ["A"], "block_id": 0},
            {"parent_table_id": "t1", "field_id": "col:t1:0", "role": "column", "target_name": "A", "source_label": "A", "source_labels": ["A"], "block_id": 1},
        ],
        "u",
        "v1.2.0",
        ["trim"],
    )
    prior["fingerprint"] = fp
    out = run_cross_table(
        _bundle(),
        "u",
        ai_confirm=lambda q: {"reply": "confirm"},
        clock=lambda: "t",
        session_dir=str(tmp_path),
        prior_log=prior,
    )
    assert out["replayed"] is True


