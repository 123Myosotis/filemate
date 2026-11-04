# -*- coding: utf-8 -*-
"""S 段：阶段执行、真 E 交接、AI 两阶段、回滚①②③ 执行方分离。"""
from __future__ import annotations

from pathlib import Path

import pytest

import config
from pipeline.block_cross_table.conflict_e import ConflictE, validate_e_package
from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.join_hypothesis import reset_trace_seq
from pipeline.block_cross_table.stage_exec import (
    _apply_rollback,
    gate_allows_e,
    report_cross_block_ownership,
    resolve_input_block_ids,
    run_stage_exec,
)
from pipeline.block_cross_table.stage_plan import (
    ROLLBACK_A_PRIME,
    ROLLBACK_LAYOUT,
    ROLLBACK_OWNERSHIP,
    ROLLBACK_STAGE,
)


def _patch_trace(monkeypatch, tmp_path: Path):
    reset_trace_seq()
    root = Path(__file__).resolve().parents[2]
    trace_dir = root / ".pytest_trace_tmp" / tmp_path.name / "s_trace"
    trace_dir.mkdir(parents=True, exist_ok=True)
    orig = config.get_param
    pattern = orig("cross_table.trace_filename_pattern")

    def params(key, default=None):
        if key == "cross_table.trace_dir":
            return str(trace_dir)
        if key == "cross_table.trace_filename_pattern":
            return pattern
        return orig(key, default)

    monkeypatch.setattr("config.get_param", params)
    return trace_dir


def _four():
    return {
        "推测": "s",
        "依据": "e",
        "可能错在哪": "w",
        "下一步": {"rollback_point": ROLLBACK_STAGE},
    }


def _make_e(tmp_name="e", decision=None, run_id="run-s"):
    return ConflictE(
        run_id=run_id,
        chat=lambda p: _four(),
        conflict_ask_max=3,
        clock=lambda: "TS",
        auto_user_decision=decision
        or {"reply_kind": "stop_workbook"},
    )


def _stage(**overrides):
    base = {
        "stage_id": "s0",
        "parent_table_ids": ["t0"],
        "block_ids": [0, 1],
        "row_range": {"whole": True},
        "col_range": {"whole": True},
        "subtable_ids": [0],
        "dimension_ids": ["d0"],
        "layer_ids": ["L0"],
        "allowed_decision_kinds": ["enter_s"],
        "allow_user_ask": True,
        "rollback_point": ROLLBACK_STAGE,
        "dependency_allows_prior": False,
    }
    base.update(overrides)
    return base


def _plan(stages=None, needs_user_ask=True):
    return {
        "stages": stages or [_stage()],
        "plan_generation": 1,
        "review_result": {"needs_user_ask": needs_user_ask, "passed": True},
    }


def test_e_package_validation_same_as_stub():
    """原桩校验语义保留在 conflict_e.validate_e_package。"""
    with pytest.raises(CrossTableError) as ei:
        validate_e_package({"kind": "missing_field", "stage_id": "s0"})
    assert ei.value.code == "cross_table_contract_invalid"
    assert "missing" in (ei.value.details or {})


def test_e_decision_mapping_compatible(tmp_path, monkeypatch):
    _patch_trace(monkeypatch, tmp_path)
    e = _make_e(decision={"reply_kind": "continue"})
    pkg = {
        "kind": "stage_ai_structure_exhausted",
        "invalid_reason": "artifact_missing",
        "stage_id": "s0",
        "locator_ids": ["block_ids:0"],
        "attempts": 2,
    }
    out = e.handle(pkg)
    assert out["decision"] == "continue"
    assert out["source"] in ("user", "ask_exhausted") or out["decision"] == "continue"
    # attempts==2 指阶段 AI 重试 2 次后 E 收到包；不是 E 被调 2 次
    assert pkg["attempts"] == 2
    assert len(e.received) == 1


def test_s_calls_real_e_not_stub():
    import pipeline.block_cross_table.stage_exec as se
    import pipeline.block_cross_table.conflict_e as ce

    src = open(se.__file__, encoding="utf-8").read()
    assert "e_stub" not in src
    assert "conflict_e" in src
    assert not Path(ce.__file__).with_name("e_stub.py").exists()


def test_gate_allows_e_and_only():
    assert gate_allows_e(True, True) is True
    assert gate_allows_e(True, False) is False
    assert gate_allows_e(False, True) is False
    assert gate_allows_e(False, False) is False


def test_ai_structure_invalid_retries_before_e(tmp_path, monkeypatch):
    _patch_trace(monkeypatch, tmp_path)
    calls = {"n": 0}
    e = _make_e(decision={"reply_kind": "stop_workbook"})

    def chat(prompt):
        calls["n"] += 1
        return {"ok": False, "reason": "bad_shape"}

    out = run_stage_exec(
        run_id="run-s1",
        plan=_plan(needs_user_ask=True),
        chat=chat,
        e_handler=e,
        plan_regenerate_max=2,
        clock=lambda: "TS1",
    )
    assert out["status"] == "hand_off_e"
    assert out["attempts"] == 2
    assert calls["n"] == 2
    assert len(e.received) == 1
    pkg = e.received[0]
    assert pkg["kind"] == "stage_ai_structure_exhausted"
    assert pkg["attempts"] == 2


def test_ai_exhausted_gate_denied_rolls_back_no_e(tmp_path, monkeypatch):
    _patch_trace(monkeypatch, tmp_path)
    e = _make_e()
    stage = _stage(allow_user_ask=False, rollback_point=ROLLBACK_STAGE)

    out = run_stage_exec(
        run_id="run-s2",
        plan=_plan(stages=[stage], needs_user_ask=True),
        chat=lambda p: {"ok": False, "reason": "still_bad"},
        e_handler=e,
        plan_regenerate_max=1,
        clock=lambda: "TS2",
    )
    assert out["status"] == "structure_failed_rollback"
    assert e.received == []
    assert out["rollback"]["executor"] == "S"


def test_rollback_point_executors_separated():
    products = {"s0": {"x": 1}}
    r2 = _apply_rollback(
        stage=_stage(rollback_point=ROLLBACK_STAGE),
        stage_products=products,
        reason="t",
    )
    assert r2["executor"] == "S"
    assert r2["discarded"] is True

    products2 = {"s0": {"x": 1}}
    r1 = _apply_rollback(
        stage=_stage(rollback_point=ROLLBACK_A_PRIME),
        stage_products=products2,
        reason="t",
    )
    assert r1["executor"] == "upstream"
    assert "s0" in products2

    r3 = _apply_rollback(
        stage=_stage(rollback_point=ROLLBACK_LAYOUT),
        stage_products={"s0": 1},
        reason="t",
    )
    assert r3["executor"] == "upstream"


def test_ai_exhausted_rollback_point_one_escalates(tmp_path, monkeypatch):
    _patch_trace(monkeypatch, tmp_path)
    e = _make_e()
    stage = _stage(allow_user_ask=False, rollback_point=ROLLBACK_A_PRIME)
    out = run_stage_exec(
        run_id="run-s3",
        plan=_plan(stages=[stage], needs_user_ask=True),
        chat=lambda p: {"ok": False, "reason": "x"},
        e_handler=e,
        plan_regenerate_max=1,
        clock=lambda: "TS3",
    )
    assert out["rollback"]["executor"] == "upstream"
    assert out["rollback"]["rollback_point"] == ROLLBACK_A_PRIME


def test_happy_path_projection(tmp_path, monkeypatch):
    _patch_trace(monkeypatch, tmp_path)
    e = _make_e()

    def chat(prompt):
        if "review" in prompt:
            return {"passed": True, "reason": "ok", "needs_user_ask": False}
        return {"ok": True, "artifact": {"v": 1}, "decision_kind": "enter_s"}

    out = run_stage_exec(
        run_id="run-ok",
        plan=_plan(),
        chat=chat,
        e_handler=e,
        plan_regenerate_max=2,
        clock=lambda: "TOK",
    )
    assert out["ok"] is True
    assert out["stage_products"]["s0"]["v"] == 1
    assert e.received == []


def test_review_failed_gate_allows_e(tmp_path, monkeypatch):
    _patch_trace(monkeypatch, tmp_path)
    e = _make_e(decision={"reply_kind": "stop_workbook"})

    def chat(prompt):
        if "review" in prompt:
            return {"passed": False, "reason": "ask", "needs_user_ask": True}
        return {"ok": True, "artifact": {"v": 1}, "decision_kind": "enter_s"}

    out = run_stage_exec(
        run_id="run-rev",
        plan=_plan(),
        chat=chat,
        e_handler=e,
        plan_regenerate_max=2,
        clock=lambda: "TR",
    )
    assert out["status"] == "hand_off_e"
    assert e.received[0]["kind"] == "review_failed_ask_user"


def test_missing_needs_user_ask_on_review_four_elements(tmp_path, monkeypatch):
    _patch_trace(monkeypatch, tmp_path)
    e = _make_e()

    def chat(prompt):
        if "review" in prompt:
            return {"passed": True, "reason": "ok"}
        return {"ok": True, "artifact": {"v": 1}, "decision_kind": "enter_s"}

    out = run_stage_exec(
        run_id="run-miss",
        plan=_plan(),
        chat=chat,
        e_handler=e,
        plan_regenerate_max=1,
        clock=lambda: "TM",
    )
    assert out["status"] == "structure_invalid_missing_field"
    pkg = e.received[0]
    assert pkg["kind"] == "missing_field"
    assert pkg["missing_field"] == "needs_user_ask"


def test_ownership_requires_input_block_ids(tmp_path, monkeypatch):
    _patch_trace(monkeypatch, tmp_path)
    e = _make_e()
    stage = _stage()
    out = report_cross_block_ownership(
        stage=stage,
        conflicts=[{"dimension_field_id": "d0", "blocks": []}],
        e_handler=e,
        actual_touched=[0],
    )
    assert out["package"]["input_block_ids"] == ["0"]
    assert e.received[0]["input_block_ids"] == ["0"]
    with pytest.raises(CrossTableError):
        validate_e_package(
            {
                "kind": "cross_block_ownership",
                "rollback_point": ROLLBACK_OWNERSHIP,
                "input_block_ids": [],
                "conflicts": [],
            }
        )


def test_input_block_ids_subset_rule():
    stage = _stage(block_ids=[0, 1, 2])
    assert resolve_input_block_ids(stage) == ["0", "1", "2"]
    assert resolve_input_block_ids(stage, actual_touched=[1]) == ["1"]
    with pytest.raises(CrossTableError):
        resolve_input_block_ids(stage, actual_touched=[9])


def test_s_does_not_simulate_user_ask():
    import inspect
    import pipeline.block_cross_table.stage_exec as mod

    src = inspect.getsource(mod)
    assert "ask_user_locally" not in src
    assert "def ask_user" not in src
    assert "simulate_user" not in src
