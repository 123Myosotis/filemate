# -*- coding: utf-8 -*-
"""E 真四段：收包、追问上限、用户四类回复、交接包、trace。"""
from __future__ import annotations

from pathlib import Path

import pytest

import config
from pipeline.block_cross_table.conflict_e import (
    DECISION_CONTINUE,
    DECISION_CORRECT,
    DECISION_ROLLBACK,
    DECISION_STOP,
    ConflictE,
    require_conflict_ask_max,
    run_conflict_e,
    validate_e_envelope,
    validate_e_package,
)
from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.join_hypothesis import reset_trace_seq
from pipeline.block_cross_table.stage_plan import ROLLBACK_OWNERSHIP, ROLLBACK_STAGE


def _patch_trace(monkeypatch, tmp_path: Path):
    reset_trace_seq()
    root = Path(__file__).resolve().parents[2]
    trace_dir = root / ".pytest_trace_tmp" / tmp_path.name / "e_trace"
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
        "推测": "spec",
        "依据": "ev",
        "可能错在哪": "alt",
        "下一步": {"rollback_point": ROLLBACK_STAGE},
    }


def _env(body, **over):
    base = {
        "run_id": "run-e1",
        "entry_kind": "from_s_stage",
        "conflict_session_seq": 1,
        "trigger_source": "S",
        "body": body,
    }
    base.update(over)
    return base


def test_require_conflict_ask_max_contract_invalid():
    with pytest.raises(CrossTableError) as ei:
        require_conflict_ask_max(None)
    assert ei.value.code == "cross_table_contract_invalid"
    with pytest.raises(CrossTableError):
        require_conflict_ask_max(0)


def test_validate_envelope_and_ownership_requires_ids():
    with pytest.raises(CrossTableError):
        validate_e_envelope({"run_id": "r"})
    body = {
        "kind": "cross_block_ownership",
        "rollback_point": ROLLBACK_OWNERSHIP,
        "input_block_ids": [],
        "conflicts": [],
        "adjudication": None,
    }
    with pytest.raises(CrossTableError):
        validate_e_package(body)


def test_four_part_then_continue(tmp_path, monkeypatch):
    _patch_trace(monkeypatch, tmp_path)
    body = {
        "kind": "missing_field",
        "missing_field": "needs_user_ask",
        "review_raw": {"passed": True},
        "stage_id": "s0",
        "locator_ids": ["b:0"],
    }

    def chat(_p):
        return _four()

    def user(_fp):
        return {"reply_kind": DECISION_CONTINUE}

    out = run_conflict_e(
        envelope=_env(body),
        chat=chat,
        user_callback=user,
        conflict_ask_max=2,
        clock=lambda: "TE1",
    )
    assert out["decision"] == DECISION_CONTINUE
    assert out["resume_hint"] == "stage_restart_like_rollback_2"
    assert out["trigger_source"] == "S"
    assert len(out["trace_paths"]) >= 2


def test_ask_exhausted_stop_workbook(tmp_path, monkeypatch):
    _patch_trace(monkeypatch, tmp_path)
    body = {
        "kind": "stage_ai_structure_exhausted",
        "invalid_reason": "x",
        "stage_id": "s0",
        "locator_ids": [],
        "attempts": 2,
    }
    calls = {"n": 0}

    def chat(_p):
        calls["n"] += 1
        return {"推测": "only"}  # incomplete

    out = run_conflict_e(
        envelope=_env(body),
        chat=chat,
        user_callback=lambda fp: {"reply_kind": DECISION_STOP},
        conflict_ask_max=2,
        clock=lambda: "TE2",
    )
    assert out["decision"] == DECISION_STOP
    assert out["source"] == "ask_exhausted"
    assert out["status"] == "ask_exhausted"
    assert calls["n"] == 2  # 用尽后模型调用次数 == 上限


def test_user_rollback_and_correct(tmp_path, monkeypatch):
    _patch_trace(monkeypatch, tmp_path)
    body = {
        "kind": "review_failed_ask_user",
        "stage_id": "s0",
        "locator_ids": ["a"],
        "review_raw": {"passed": False},
        "trigger": "review_failed_ask_user",
    }
    state = {"n": 0}

    def chat(_p):
        return _four()

    def user(fp):
        state["n"] += 1
        if state["n"] == 1:
            return {"reply_kind": DECISION_CORRECT, "text": "应绑定账户号而非户名"}
        return {
            "reply_kind": DECISION_ROLLBACK,
            "rollback_point": ROLLBACK_STAGE,
        }

    out = run_conflict_e(
        envelope=_env(body),
        chat=chat,
        user_callback=user,
        conflict_ask_max=5,
        clock=lambda: "TE3",
    )
    assert out["decision"] == DECISION_ROLLBACK
    assert out["executor_hint"] == "S"
    assert out["rollback_point"] == ROLLBACK_STAGE


def test_i_recognize_forced_path(tmp_path, monkeypatch):
    _patch_trace(monkeypatch, tmp_path)
    body = {
        "kind": "conflict",
        "reason_codes": ["ask_budget_exhausted_review"],
        "status": "conflict",
    }

    out = run_conflict_e(
        envelope=_env(
            body,
            entry_kind="from_i_recognize",
            trigger_source="I",
        ),
        chat=lambda p: _four(),
        user_callback=lambda fp: {"reply_kind": DECISION_STOP},
        conflict_ask_max=2,
        clock=lambda: "TE4",
    )
    assert out["decision"] == DECISION_STOP
    assert out["trigger_source"] == "I"


def test_ownership_rollback_scope_has_ids(tmp_path, monkeypatch):
    _patch_trace(monkeypatch, tmp_path)
    body = {
        "kind": "cross_block_ownership",
        "rollback_point": ROLLBACK_OWNERSHIP,
        "input_block_ids": ["0", "1"],
        "conflicts": [{"dimension_field_id": "d0"}],
        "adjudication": None,
    }

    def four_own():
        return {
            "推测": "s",
            "依据": "e",
            "可能错在哪": "w",
            "下一步": {"rollback_point": ROLLBACK_OWNERSHIP},
        }

    out = run_conflict_e(
        envelope=_env(body, entry_kind="from_b_via_i_ownership", trigger_source="I"),
        chat=lambda p: four_own(),
        user_callback=lambda fp: {
            "reply_kind": DECISION_ROLLBACK,
            "rollback_point": ROLLBACK_OWNERSHIP,
        },
        conflict_ask_max=2,
        clock=lambda: "TE5",
    )
    assert out["scope"]["input_block_ids"] == ["0", "1"]
    assert out["executor_hint"] == "I"


def test_conflict_e_adapter_for_s(tmp_path, monkeypatch):
    _patch_trace(monkeypatch, tmp_path)
    e = ConflictE(
        run_id="run-ad",
        chat=lambda p: _four(),
        conflict_ask_max=2,
        clock=lambda: "TE6",
        auto_user_decision={"reply_kind": DECISION_CONTINUE},
    )
    out = e.handle(
        {
            "kind": "missing_field",
            "missing_field": "allow_user_ask",
            "review_raw": None,
            "stage_id": "s0",
            "locator_ids": [],
        }
    )
    assert out["decision"] == DECISION_CONTINUE
    assert e.received[0]["kind"] == "missing_field"


def test_e_does_not_parse_correction_locally():
    import inspect
    import pipeline.block_cross_table.conflict_e as mod

    src = inspect.getsource(mod)
    assert "user_correction_verbatim" in src
    assert "parse_correction" not in src
    assert "是否歧义" not in src or "不做" in src
