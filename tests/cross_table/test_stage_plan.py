# -*- coding: utf-8 -*-
"""B 段：计划-画像一致性、无此结构、空层列表、trace、跨块上报。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config
from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.join_hypothesis import reset_trace_seq
from pipeline.block_cross_table.portrait_store_fs import FilePortraitStore
from pipeline.block_cross_table.portrait_write import (
    STRUCTURE_STATUS_LAYERS_RECOGNIZED,
    STRUCTURE_STATUS_UPSTREAM_ABSENT,
    write_block_portrait,
)
from pipeline.block_cross_table.stage_plan import (
    ROLLBACK_OWNERSHIP,
    ROLLBACK_STAGE,
    STAGE_ROLLBACK_POINTS,
    detect_cross_block_ownership_inconsistency,
    require_plan_regenerate_max,
    run_stage_plan,
    validate_stage_plan_against_portrait,
    build_portrait_index,
)
from pipeline.block_cross_table.stage_plan_trace import require_run_id


def _repo_dirs(tmp_path: Path):
    root = Path(__file__).resolve().parents[2]
    trace = root / ".pytest_trace_tmp" / tmp_path.name / "b_trace"
    portrait = root / ".pytest_trace_tmp" / tmp_path.name / "b_portrait"
    trace.mkdir(parents=True, exist_ok=True)
    portrait.mkdir(parents=True, exist_ok=True)
    return trace, portrait


def _patch_trace(monkeypatch, tmp_path: Path):
    reset_trace_seq()
    trace_dir, portrait_dir = _repo_dirs(tmp_path)
    orig = config.get_param
    pattern = orig("cross_table.trace_filename_pattern")

    def params(key, default=None):
        if key == "cross_table.trace_dir":
            return str(trace_dir)
        if key == "cross_table.trace_filename_pattern":
            return pattern
        return orig(key, default)

    monkeypatch.setattr("config.get_param", params)
    return trace_dir, FilePortraitStore(portrait_dir, clear=True)


def _ok_layer(**overrides):
    base = {
        "rect_to_layer": {"r0": "L0"},
        "layer_order": ["L0", "L1"],
        "layers": [{"layer_id": "L0"}],
        "subtable_boundaries": [{"subtable_id": 0}],
        "nesting_facts": [],
        "group_label_declaration": [],
        "dimension_membership": [
            {"dimension_field_id": "d0", "subtable_id": 0, "layer_ids": ["L0"]}
        ],
        "header_completeness": "complete",
    }
    base.update(overrides)
    return base


def _seed_ok(store, block_id, layer=None):
    write_block_portrait(
        {
            "status": "ok",
            "block_id": block_id,
            "layer_result": layer or _ok_layer(),
        },
        store,
    )


def _seed_absent(store, block_id):
    write_block_portrait({"status": "no_structure", "block_id": block_id}, store)


def _valid_stage(block_id=0, **overrides):
    stage = {
        "stage_id": "s0",
        "parent_table_ids": ["t0"],
        "block_ids": [block_id],
        "row_range": {"whole": True},
        "col_range": {"whole": True},
        "subtable_ids": [0],
        "dimension_ids": ["d0"],
        "layer_ids": ["L0"],
        "allowed_decision_kinds": ["enter_s"],
        "allow_user_ask": False,
        "rollback_point": ROLLBACK_STAGE,
        "dependency_allows_prior": False,
    }
    stage.update(overrides)
    return stage


def test_require_run_id_and_regen_max_contract_invalid():
    with pytest.raises(CrossTableError) as ei:
        require_run_id("")
    assert ei.value.code == "cross_table_audit_structure_invalid"
    with pytest.raises(CrossTableError) as ei2:
        require_plan_regenerate_max(None)
    assert ei2.value.code == "cross_table_contract_invalid"


def test_empty_dimension_layer_ids_not_error(tmp_path, monkeypatch):
    trace_dir, store = _patch_trace(monkeypatch, tmp_path)
    layer = _ok_layer(
        dimension_membership=[
            {"dimension_field_id": "d0", "subtable_id": 0, "layer_ids": []}
        ]
    )
    _seed_ok(store, 0, layer)
    portraits = {str(k): store.get(k) for k in store.written_block_ids()}
    assert portraits["0"]["dimension_membership"][0]["layer_ids"] == []

    stage = _valid_stage(dimension_ids=["d0"], layer_ids=["L0"])
    index = build_portrait_index(portraits)
    from pipeline.block_cross_table.stage_plan import read_allowed_decision_kinds

    reasons = validate_stage_plan_against_portrait(
        {"stages": [stage]},
        index,
        allowed_decision_kinds=read_allowed_decision_kinds(),
    )
    assert reasons == []


def test_upstream_absent_skips_layer_slice_but_plan_ok(tmp_path, monkeypatch):
    trace_dir, store = _patch_trace(monkeypatch, tmp_path)
    _seed_absent(store, 0)
    _seed_ok(store, 1)

    def chat(prompt: str):
        if "stage plan generate" in prompt:
            return {
                "stages": [
                    _valid_stage(
                        block_id=1,
                        stage_id="s1",
                        layer_ids=["L0"],
                        subtable_ids=[0],
                        dimension_ids=["d0"],
                    )
                ]
            }
        return {"passed": True, "reason": "ok", "needs_user_ask": False}

    out = run_stage_plan(
        run_id="run-b-absent",
        store=store,
        chat=chat,
        user_utterance="u",
        capability_text="cap",
        plan_regenerate_max=3,
        clock=lambda: "TB1",
    )
    assert out["ok"] is True
    assert store.get(0)["structure_status"] == STRUCTURE_STATUS_UPSTREAM_ABSENT
    assert "layers" not in store.get(0)


def test_plan_unknown_layer_rejected_then_regen(tmp_path, monkeypatch):
    trace_dir, store = _patch_trace(monkeypatch, tmp_path)
    _seed_ok(store, 0)
    state = {"n": 0}

    def chat(prompt: str):
        if "stage plan generate" in prompt:
            state["n"] += 1
            if state["n"] == 1:
                return {"stages": [_valid_stage(layer_ids=["L_UNKNOWN"])]}
            return {"stages": [_valid_stage(layer_ids=["L0"])]}
        return {"passed": True, "reason": "ok"}

    out = run_stage_plan(
        run_id="run-b-regen",
        store=store,
        chat=chat,
        user_utterance="u",
        capability_text="cap",
        plan_regenerate_max=3,
        clock=lambda: "TB2",
    )
    assert out["ok"] is True
    assert out["attempts"] == 2
    assert state["n"] == 2


def test_plan_consistency_unknown_block(tmp_path, monkeypatch):
    _, store = _patch_trace(monkeypatch, tmp_path)
    _seed_ok(store, 0)
    portraits = {str(k): store.get(k) for k in store.written_block_ids()}
    index = build_portrait_index(portraits)
    from pipeline.block_cross_table.stage_plan import read_allowed_decision_kinds

    reasons = validate_stage_plan_against_portrait(
        {"stages": [_valid_stage(block_ids=[99])]},
        index,
        allowed_decision_kinds=read_allowed_decision_kinds(),
    )
    assert any(r.startswith("unknown_block:") for r in reasons)


def test_generate_and_review_trace_land(tmp_path, monkeypatch):
    trace_dir, store = _patch_trace(monkeypatch, tmp_path)
    _seed_ok(store, 0)

    def chat(prompt: str):
        if "stage plan generate" in prompt:
            return {"stages": [_valid_stage()]}
        return {"passed": True, "reason": "ok"}

    out = run_stage_plan(
        run_id="run-b-trace",
        store=store,
        chat=chat,
        user_utterance="u",
        capability_text="cap",
        plan_regenerate_max=2,
        clock=lambda: "TB3",
    )
    assert out["ok"] is True
    gens = list(trace_dir.glob("stage_plan_generate_*.json"))
    revs = list(trace_dir.glob("stage_plan_review_*.json"))
    assert len(gens) >= 1
    assert len(revs) >= 1
    meta_g = json.loads(gens[0].read_text(encoding="utf-8"))["meta"]
    assert meta_g["run_id"] == "run-b-trace"
    assert meta_g["call_kind"] == "stage_plan_generate"
    assert meta_g["kind_seq"] == 1
    assert meta_g["plan_generation"] == 1
    meta_r = json.loads(revs[0].read_text(encoding="utf-8"))["meta"]
    assert meta_r["call_kind"] == "stage_plan_review"


def test_missing_run_id_no_trace(tmp_path, monkeypatch):
    _, store = _patch_trace(monkeypatch, tmp_path)
    _seed_ok(store, 0)
    with pytest.raises(CrossTableError) as ei:
        run_stage_plan(
            run_id="",
            store=store,
            chat=lambda _p: {},
            user_utterance="u",
            capability_text="cap",
            plan_regenerate_max=1,
        )
    assert ei.value.code == "cross_table_audit_structure_invalid"


def test_cross_block_ownership_report_no_adjudication(tmp_path, monkeypatch):
    _, store = _patch_trace(monkeypatch, tmp_path)
    _seed_ok(
        store,
        0,
        _ok_layer(
            dimension_membership=[
                {"dimension_field_id": "d0", "subtable_id": 0, "layer_ids": ["L0"]}
            ]
        ),
    )
    _seed_ok(
        store,
        1,
        _ok_layer(
            dimension_membership=[
                {"dimension_field_id": "d0", "subtable_id": 0, "layer_ids": ["L1"]}
            ]
        ),
    )
    report = detect_cross_block_ownership_inconsistency(
        {str(k): store.get(k) for k in store.written_block_ids()}
    )
    assert report is not None
    assert report["rollback_point"] == ROLLBACK_OWNERSHIP
    assert report["adjudication"] is None

    def chat(_prompt: str):
        raise AssertionError("model must not be called when ownership conflict")

    out = run_stage_plan(
        run_id="run-b-own",
        store=store,
        chat=chat,
        user_utterance="u",
        capability_text="cap",
        plan_regenerate_max=2,
        clock=lambda: "TB4",
    )
    assert out["ok"] is False
    assert out["status"] == "cross_block_ownership"
    assert out["report"]["adjudication"] is None


def test_delivery_requires_s_declarations(tmp_path, monkeypatch):
    """缺 allow_user_ask / dependency → 一致性失败（B 侧，不扔给 S）。"""
    _, store = _patch_trace(monkeypatch, tmp_path)
    _seed_ok(store, 0)
    portraits = {str(k): store.get(k) for k in store.written_block_ids()}
    index = build_portrait_index(portraits)
    from pipeline.block_cross_table.stage_plan import read_allowed_decision_kinds

    bad = _valid_stage()
    del bad["allow_user_ask"]
    reasons = validate_stage_plan_against_portrait(
        {"stages": [bad]},
        index,
        allowed_decision_kinds=read_allowed_decision_kinds(),
    )
    assert any("allow_user_ask" in r for r in reasons)


def test_b_does_not_call_a_prime():
    import inspect
    import pipeline.block_cross_table.stage_plan as mod

    src = inspect.getsource(mod)
    assert "run_layer_recognize_block" not in src
    assert "write_block_portrait" not in src


def test_stage_rollback_point_ownership_forbidden(tmp_path, monkeypatch):
    """澄清1：阶段声明回滚点为④ → 结构无效（提案无效类语义=reasons 非空）。"""
    _, store = _patch_trace(monkeypatch, tmp_path)
    _seed_ok(store, 0)
    portraits = {str(k): store.get(k) for k in store.written_block_ids()}
    index = build_portrait_index(portraits)
    from pipeline.block_cross_table.stage_plan import read_allowed_decision_kinds

    assert ROLLBACK_OWNERSHIP not in STAGE_ROLLBACK_POINTS
    reasons = validate_stage_plan_against_portrait(
        {"stages": [_valid_stage(rollback_point=ROLLBACK_OWNERSHIP)]},
        index,
        allowed_decision_kinds=read_allowed_decision_kinds(),
    )
    assert "rollback_point_ownership_forbidden_in_stage" in reasons

    state = {"n": 0}

    def chat(prompt: str):
        if "stage plan generate" in prompt:
            state["n"] += 1
            if state["n"] == 1:
                return {
                    "stages": [_valid_stage(rollback_point=ROLLBACK_OWNERSHIP)]
                }
            return {"stages": [_valid_stage(rollback_point=ROLLBACK_STAGE)]}
        return {"passed": True, "reason": "ok"}

    out = run_stage_plan(
        run_id="run-b-rb4",
        store=store,
        chat=chat,
        user_utterance="u",
        capability_text="cap",
        plan_regenerate_max=3,
        clock=lambda: "TB5",
    )
    assert out["ok"] is True
    assert out["attempts"] == 2
    assert out["plan"]["stages"][0]["rollback_point"] == ROLLBACK_STAGE


def test_plan_regenerate_exhausted_behavior(tmp_path, monkeypatch):
    """澄清3 路径乙：达 plan_regenerate_max 仍失败 → conflict 包交 I→E。"""
    _, store = _patch_trace(monkeypatch, tmp_path)
    _seed_ok(store, 0)

    def chat(prompt: str):
        if "stage plan generate" in prompt:
            return {"stages": [_valid_stage(layer_ids=["L_UNKNOWN"])]}
        return {"passed": True, "reason": "ok"}

    out = run_stage_plan(
        run_id="run-b-exh",
        store=store,
        chat=chat,
        user_utterance="u",
        capability_text="cap",
        plan_regenerate_max=2,
        clock=lambda: "TB6",
    )
    assert out["ok"] is False
    assert out["status"] == "conflict"
    assert out["attempts"] == 2
    conflict = out["conflict"]
    assert conflict["kind"] == "stage_plan_regenerate_exhausted"
    assert "plan_regenerate_exhausted" in conflict["reason_codes"]
    assert conflict["plan_regenerate_max"] == 2
    assert conflict["hand_off"] == "I_to_E"
    assert any("unknown_layer" in r for r in conflict["reasons"])
    # I 可解析：必含 reason_codes / hand_off / attempts
    for key in ("reason_codes", "hand_off", "attempts", "kind"):
        assert key in conflict
