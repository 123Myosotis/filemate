# -*- coding: utf-8 -*-
"""I 节编排：块循环分流、入口重试、E、恢复、持久化画像、决策 trace。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config
from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.join_hypothesis import reset_trace_seq
from pipeline.block_cross_table.layer_recognize import project_upstream_rectangles
from pipeline.block_cross_table.layer_recognize_trace import reset_block_id_registry
from pipeline.block_cross_table.portrait_store_fs import FilePortraitStore
from pipeline.block_cross_table.portrait_write import (
    STRUCTURE_STATUS_LAYERS_RECOGNIZED,
    STRUCTURE_STATUS_UPSTREAM_ABSENT,
)
from pipeline.block_cross_table.routing_i import (
    PHASE_READY_FOR_B,
    PHASE_STOPPED_AT_E,
    RESUME_CONTINUE,
    RESUME_RERUN_OWNERSHIP,
    RESUME_ROLLBACK_A_PRIME,
    RESUME_ROLLBACK_LAYOUT,
    RoutingI,
    require_run_id,
)


def _block(bid=0, layout="matrix"):
    return {
        "block_id": bid,
        "layout": layout,
        "parent_table_ids": ["t0"],
        "context": {
            "merges": [
                {"r0": 0, "c0": 0, "r1": 2, "c1": 4},
                {"r0": 1, "c0": 0, "r1": 2, "c1": 2},
                {"r0": 1, "c0": 2, "r1": 2, "c1": 4},
            ]
        },
    }


def _draft(bid=0):
    return {
        "block_id": bid,
        "subtable_ids": [0],
        "fields": [
            {
                "field_id": "f0",
                "role": "group",
                "subtable_id": 0,
                "segments": [
                    {"group_index": 0, "col_range": [0, 2], "value_rows": [1]},
                    {"group_index": 1, "col_range": [2, 4], "value_rows": [1]},
                ],
            },
            {
                "field_id": "f1",
                "role": "list",
                "subtable_id": 0,
                "segments": [
                    {"group_index": 0, "col_range": [0, 2], "value_rows": [2, 3]},
                ],
            },
        ],
    }


def _tables():
    return {
        "t0": {
            "grid": [
                ["TOP", "TOP", "TOP", "TOP"],
                ["G0", "G0", "G1", "G1"],
                ["1", "2", "3", "4"],
                ["5", "6", "7", "8"],
            ]
        }
    }


def _good_proposal(block, draft, header="complete"):
    rects = project_upstream_rectangles(block, draft)
    mapping = {
        r["rect_id"]: ("L0" if r["rect_id"] == "merge:0" else "L1") for r in rects
    }
    return {
        "rectangle_layer_map": mapping,
        "layer_order": ["L0", "L1"],
        "group_label_decls": [
            {"field_id": "f0", "kind": "not_a_layer", "layer_ids": []}
        ],
        "dimension_membership": [],
        "header_completeness": header,
    }


def _repo_dirs(tmp_path: Path):
    root = Path(__file__).resolve().parents[2]
    trace = root / ".pytest_trace_tmp" / tmp_path.name / "trace"
    portrait = root / ".pytest_trace_tmp" / tmp_path.name / "portrait"
    trace.mkdir(parents=True, exist_ok=True)
    portrait.mkdir(parents=True, exist_ok=True)
    return trace, portrait


def _patch_env(monkeypatch, tmp_path: Path):
    reset_trace_seq()
    reset_block_id_registry()
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
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    return trace_dir, portrait_dir


def _make_i(monkeypatch, tmp_path, blocks, drafts, chat, run_id="run-i-1", **kwargs):
    trace_dir, portrait_dir = _patch_env(monkeypatch, tmp_path)
    store = FilePortraitStore(portrait_dir, clear=True)
    session = RoutingI(
        run_id=run_id,
        blocks=blocks,
        drafts_by_block=drafts,
        tables_by_id=_tables(),
        user_utterance="u",
        review_input_capacity=100_000,
        portrait_store=store,
        chat=chat,
        clock=lambda: "20261020T130000",
        **kwargs,
    )
    return session, store, trace_dir


# --- run_id ---


def test_require_run_id_missing_audit():
    with pytest.raises(CrossTableError) as ei:
        require_run_id("")
    assert ei.value.code == "cross_table_audit_structure_invalid"
    with pytest.raises(CrossTableError) as ei2:
        require_run_id(None)
    assert ei2.value.code == "cross_table_audit_structure_invalid"


def test_routing_i_rejects_missing_run_id(tmp_path, monkeypatch):
    _, portrait_dir = _patch_env(monkeypatch, tmp_path)
    with pytest.raises(CrossTableError) as ei:
        RoutingI(
            run_id="",
            blocks=[_block()],
            drafts_by_block={0: _draft()},
            tables_by_id=_tables(),
            user_utterance="u",
            review_input_capacity=100_000,
            portrait_store=FilePortraitStore(portrait_dir),
            chat=lambda _p: {},
        )
    assert ei.value.code == "cross_table_audit_structure_invalid"


# --- happy path ---


def test_all_blocks_ok_ready_for_b_and_persistent_portrait(tmp_path, monkeypatch):
    b0, d0 = _block(0), _draft(0)
    b1, d1 = _block(1), _draft(1)
    prop0 = _good_proposal(b0, d0)
    prop1 = _good_proposal(b1, d1)

    def chat(prompt: str):
        if "layer recognition" in prompt:
            # 顺序：先块0后块1
            if not hasattr(chat, "n"):
                chat.n = 0
            chat.n += 1
            return prop0 if chat.n <= 1 else prop1
        return {"passed": True, "reason": "ok"}

    session, store, trace_dir = _make_i(
        monkeypatch,
        tmp_path,
        [b0, b1],
        {0: d0, 1: d1},
        chat,
    )
    out = session.run()
    assert out["phase"] == PHASE_READY_FOR_B
    assert out["completed_block_ids"] == [0, 1]
    assert store.get(0)["structure_status"] == STRUCTURE_STATUS_LAYERS_RECOGNIZED
    assert store.get(1)["structure_status"] == STRUCTURE_STATUS_LAYERS_RECOGNIZED
    # 持久化：新 store 实例仍可读
    store2 = FilePortraitStore(store.root)
    assert store2.get(0) is not None
    assert "dimension_membership" in store2.get(0)
    # 决策 trace 含 enter_b
    kinds = []
    for p in trace_dir.glob("orchestrate_decision_*.json"):
        kinds.append(json.loads(p.read_text(encoding="utf-8"))["meta"]["decision_kind"])
    assert "enter_b" in kinds
    assert "continue_next_block" in kinds


def test_no_structure_block_writes_upstream_absent(tmp_path, monkeypatch):
    b0 = _block(0, layout="ordered")  # 无矩阵 → no_structure
    session, store, _ = _make_i(
        monkeypatch,
        tmp_path,
        [b0],
        {0: None},
        chat=lambda _p: {},
    )
    out = session.run()
    assert out["phase"] == PHASE_READY_FOR_B
    assert store.get(0)["structure_status"] == STRUCTURE_STATUS_UPSTREAM_ABSENT
    assert "layers" not in store.get(0)


# --- fail split ---


def test_conflict_direct_to_e_no_entry_exhausted_flag(tmp_path, monkeypatch):
    b0, d0 = _block(0), _draft(0)
    prop = _good_proposal(b0, d0)
    prop["layer_order"] = []  # 提案失败 → 最终冲突（预算内）

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return prop
        return {"passed": True, "reason": "ok"}

    # 压低 ask 使提案重试耗尽进 conflict
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 2
    )
    session, store, _ = _make_i(
        monkeypatch, tmp_path, [b0], {0: d0}, chat
    )
    # 恢复 patch：_make_i 已 patch 为 8，再压到 2
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 2
    )
    out = session.run()
    assert out["phase"] == PHASE_STOPPED_AT_E
    assert out["e_payload"]["kind"] == "conflict"
    assert out["e_payload"]["entry_retry_exhausted"] is False
    assert store.get(0) is None


def test_review_failed_retries_per_block_then_e(tmp_path, monkeypatch):
    b0, d0 = _block(0), _draft(0)
    prop = _good_proposal(b0, d0)

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return prop
        return {"passed": False, "reason": "no"}

    session, store, trace_dir = _make_i(
        monkeypatch, tmp_path, [b0], {0: d0}, chat
    )
    assert session.entry_retry_max == 3
    out = session.run()
    assert out["phase"] == PHASE_STOPPED_AT_E
    assert out["e_payload"]["kind"] == "review_failed_exhausted"
    assert out["e_payload"]["appended_reason"] == "entry_retry_exhausted"
    assert out["entry_retries"]["0"] == 3
    # 决策含 retry_a_prime 与 hand_off_e
    kinds = [
        json.loads(p.read_text(encoding="utf-8"))["meta"]["decision_kind"]
        for p in trace_dir.glob("orchestrate_decision_*.json")
    ]
    assert kinds.count("retry_a_prime") == 3
    assert "hand_off_e" in kinds
    assert "stop_workbook" in kinds


def test_review_failed_and_conflict_use_separate_handlers():
    assert RoutingI._handle_review_failed is not RoutingI._handle_conflict
    assert RoutingI._handle_review_failed.__name__ == "_handle_review_failed"
    assert RoutingI._handle_conflict.__name__ == "_handle_conflict"


def test_entry_retries_independent_across_blocks(tmp_path, monkeypatch):
    """图 3：每块各自累计；块 0 的 2 次不并进块 1。"""
    b0, d0 = _block(0), _draft(0)
    b1, d1 = _block(1), _draft(1)
    prop0 = _good_proposal(b0, d0)
    prop1 = _good_proposal(b1, d1)
    propose_n = {"n": 0}

    def chat(prompt: str):
        if "layer recognition" in prompt:
            propose_n["n"] += 1
            return prop0 if propose_n["n"] <= 3 else prop1
        # 每块前 2 次入口复核否，第 3 次通过 → 各累计 2
        n = propose_n["n"]
        if n in (1, 2, 4, 5):
            return {"passed": False, "reason": "no"}
        return {"passed": True, "reason": "ok"}

    session, store, _ = _make_i(
        monkeypatch, tmp_path, [b0, b1], {0: d0, 1: d1}, chat
    )
    out = session.run()
    assert out["phase"] == PHASE_READY_FOR_B
    assert out["entry_retries"] == {"0": 2, "1": 2}
    assert sum(out["entry_retries"].values()) == 4
    # 不是整表单桶：两键各自为 2，而非 {"0": 4} 或共享计数
    assert out["entry_retries"]["0"] == 2
    assert out["entry_retries"]["1"] == 2
    assert store.get(0) is not None and store.get(1) is not None


# --- workbook stop + resume ---


def test_continue_keeps_prior_portrait_after_stop(tmp_path, monkeypatch):
    b0, d0 = _block(0), _draft(0)
    b1, d1 = _block(1), _draft(1)
    prop0 = _good_proposal(b0, d0)
    prop1 = _good_proposal(b1, d1)
    calls = {"n": 0}

    def chat2(prompt: str):
        if "layer recognition" in prompt:
            calls["n"] += 1
            if calls["n"] == 1:
                return prop0
            return prop1
        if calls["n"] == 1:
            return {"passed": True, "reason": "ok"}
        return {"passed": False, "reason": "no"}

    session, store, _ = _make_i(
        monkeypatch, tmp_path, [b0, b1], {0: d0, 1: d1}, chat2
    )
    out = session.run()
    assert out["phase"] == PHASE_STOPPED_AT_E
    assert out["stopped_block_id"] == 1
    assert store.get(0) is not None  # 前序保留
    assert store.get(1) is None
    prior_path = store._slice_path(0)
    assert prior_path.is_file()
    mtime_before = prior_path.stat().st_mtime

    def chat3(prompt: str):
        if "layer recognition" in prompt:
            return prop1
        return {"passed": True, "reason": "ok"}

    session.chat = chat3
    out2 = session.resume(RESUME_CONTINUE)
    assert out2["phase"] == PHASE_READY_FOR_B
    assert store.get(0) is not None
    assert store.get(1) is not None
    assert prior_path.is_file()
    assert prior_path.stat().st_mtime >= mtime_before


def test_rollback_to_a_prime_discards_portraits_and_reruns(tmp_path, monkeypatch):
    b0, d0 = _block(0), _draft(0)
    prop = _good_proposal(b0, d0)

    def chat_ok(prompt: str):
        if "layer recognition" in prompt:
            return prop
        return {"passed": True, "reason": "ok"}

    session, store, _ = _make_i(
        monkeypatch, tmp_path, [b0], {0: d0}, chat_ok
    )
    out = session.run()
    assert out["phase"] == PHASE_READY_FOR_B
    assert store.get(0) is not None

    # 模拟停在 E 后再回滚：先造冲突停
    def chat_fail(prompt: str):
        if "layer recognition" in prompt:
            return prop
        return {"passed": False, "reason": "no"}

    session2, store2, _ = _make_i(
        monkeypatch, tmp_path, [b0], {0: d0}, chat_fail, run_id="run-rb"
    )
    session2.run()
    assert session2.phase == PHASE_STOPPED_AT_E

    session2.chat = chat_ok
    out3 = session2.resume(RESUME_ROLLBACK_A_PRIME)
    assert out3["phase"] == PHASE_READY_FOR_B
    assert store2.get(0) is not None
    assert session2._entry_retries.get("0", 0) == 0


def test_rollback_to_layout_uses_new_blocks(tmp_path, monkeypatch):
    b0, d0 = _block(0), _draft(0)
    prop0 = _good_proposal(b0, d0)

    def chat_fail(prompt: str):
        if "layer recognition" in prompt:
            return prop0
        return {"passed": False, "reason": "no"}

    session, store, _ = _make_i(
        monkeypatch, tmp_path, [b0], {0: d0}, chat_fail
    )
    session.run()
    assert session.phase == PHASE_STOPPED_AT_E

    b9, d9 = _block(9), _draft(9)
    prop9 = _good_proposal(b9, d9)

    def chat_ok(prompt: str):
        if "layer recognition" in prompt:
            return prop9
        return {"passed": True, "reason": "ok"}

    session.chat = chat_ok
    session.drafts_by_block = {9: d9}
    out = session.resume(RESUME_ROLLBACK_LAYOUT, new_blocks=[b9])
    assert out["phase"] == PHASE_READY_FOR_B
    assert store.get(0) is None
    assert store.get(9) is not None


def test_cross_block_ownership_rerun_discards_and_resets(tmp_path, monkeypatch):
    b0, d0 = _block(0), _draft(0)
    b1, d1 = _block(1), _draft(1)
    prop0 = _good_proposal(b0, d0)
    prop1 = _good_proposal(b1, d1)

    def chat(prompt: str):
        if "layer recognition" in prompt:
            if not hasattr(chat, "n"):
                chat.n = 0
            chat.n += 1
            return prop0 if chat.n % 2 == 1 else prop1
        return {"passed": True, "reason": "ok"}

    session, store, trace_dir = _make_i(
        monkeypatch, tmp_path, [b0, b1], {0: d0, 1: d1}, chat
    )
    session.run()
    assert store.get(0) is not None
    path0 = store._slice_path(0)
    assert path0.is_file()

    chat.n = 0
    out = session.resume(RESUME_RERUN_OWNERSHIP)
    assert out["phase"] == PHASE_READY_FOR_B
    assert store.get(0) is not None
    assert store.get(1) is not None
    kinds = [
        json.loads(p.read_text(encoding="utf-8"))["meta"]["decision_kind"]
        for p in trace_dir.glob("orchestrate_decision_*.json")
    ]
    assert "rerun_all_a_prime" in kinds


# --- portrait write fail ---


def test_portrait_field_missing_stops_workbook(tmp_path, monkeypatch):
    b0, d0 = _block(0), _draft(0)
    prop = _good_proposal(b0, d0)

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return prop
        return {"passed": True, "reason": "ok"}

    session, store, _ = _make_i(
        monkeypatch, tmp_path, [b0], {0: d0}, chat
    )

    def boom(package, store, contract_data=None):
        return {
            "ok": False,
            "failure_code": "field_missing",
            "rewrite_allowed": False,
            "error_class": "cross_table_contract_invalid",
            "detail": "forced",
            "block_id": package.get("block_id"),
        }

    monkeypatch.setattr(
        "pipeline.block_cross_table.routing_i.write_block_portrait", boom
    )
    out = session.run()
    assert out["phase"] == PHASE_STOPPED_AT_E
    assert out["e_payload"]["failure_code"] == "field_missing"
    assert out["e_payload"]["rewrite_forbidden"] is True


def test_portrait_mapping_undefined_stops_workbook(tmp_path, monkeypatch):
    """I 侧三码齐：mapping_undefined 与 field_missing 同规则，非 write_rejected。"""
    b0, d0 = _block(0), _draft(0)
    prop = _good_proposal(b0, d0)

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return prop
        return {"passed": True, "reason": "ok"}

    session, store, _ = _make_i(
        monkeypatch, tmp_path, [b0], {0: d0}, chat
    )

    def boom(package, store, contract_data=None):
        return {
            "ok": False,
            "failure_code": "mapping_undefined",
            "rewrite_allowed": False,
            "error_class": "cross_table_contract_invalid",
            "detail": "forced mapping undefined",
            "block_id": package.get("block_id"),
        }

    monkeypatch.setattr(
        "pipeline.block_cross_table.routing_i.write_block_portrait", boom
    )
    out = session.run()
    assert out["phase"] == PHASE_STOPPED_AT_E
    assert out["e_payload"]["failure_code"] == "mapping_undefined"
    assert out["e_payload"]["rewrite_forbidden"] is True
    assert (
        out["e_payload"]["write_result"]["error_class"]
        == "cross_table_contract_invalid"
    )
    assert out["e_payload"]["write_result"]["error_class"] != "cross_table_input_invalid"
    assert store.get(0) is None


def test_portrait_write_rejected_user_confirm_then_ok(tmp_path, monkeypatch):
    b0, d0 = _block(0), _draft(0)
    prop = _good_proposal(b0, d0)

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return prop
        return {"passed": True, "reason": "ok"}

    rejects = {"n": 0}

    def confirm(_last):
        return True

    session, store, _ = _make_i(
        monkeypatch,
        tmp_path,
        [b0],
        {0: d0},
        chat,
        portrait_user_confirm=confirm,
    )
    real_write = __import__(
        "pipeline.block_cross_table.portrait_write", fromlist=["write_block_portrait"]
    ).write_block_portrait

    def flaky(package, st, contract_data=None):
        rejects["n"] += 1
        if rejects["n"] == 1:
            return {
                "ok": False,
                "failure_code": "write_rejected",
                "rewrite_allowed": True,
                "error_class": "cross_table_input_invalid",
                "detail": "once",
                "block_id": package.get("block_id"),
            }
        return real_write(package, st, contract_data=contract_data)

    monkeypatch.setattr(
        "pipeline.block_cross_table.routing_i.write_block_portrait", flaky
    )
    out = session.run()
    assert out["phase"] == PHASE_READY_FOR_B
    assert store.get(0) is not None


def test_handlers_not_shared_source():
    import inspect
    from pipeline.block_cross_table import routing_i as mod

    src_rf = inspect.getsource(mod.RoutingI._handle_review_failed)
    src_cf = inspect.getsource(mod.RoutingI._handle_conflict)
    assert "entry_retry" in src_rf
    assert "entry_retry_exhausted" not in src_cf or "False" in src_cf
    assert "retry_a_prime" not in src_cf


# --- trigger_source 分桶 ---


def test_decision_trace_user_trigger_has_rationale(tmp_path, monkeypatch):
    """用户主动继续 → trigger_source=user，decision_params 含原文。"""
    b0, d0 = _block(0), _draft(0)
    prop = _good_proposal(b0, d0)

    def chat_fail(prompt: str):
        if "layer recognition" in prompt:
            return prop
        return {"passed": False, "reason": "no"}

    session, store, trace_dir = _make_i(
        monkeypatch, tmp_path, [b0], {0: d0}, chat_fail
    )
    session.run()
    assert session.phase == PHASE_STOPPED_AT_E

    def chat_ok(prompt: str):
        if "layer recognition" in prompt:
            return prop
        return {"passed": True, "reason": "ok"}

    session.chat = chat_ok
    session.resume(RESUME_CONTINUE)

    user_metas = []
    for path in trace_dir.glob("orchestrate_decision_*.json"):
        meta = json.loads(path.read_text(encoding="utf-8"))["meta"]
        if meta.get("trigger_source") == "user":
            user_metas.append(meta)
    assert user_metas, "expected at least one user-triggered decision"
    for meta in user_metas:
        assert meta["trigger_source"] == "user"
        params = meta["decision_params"]
        assert "rationale_text" in params
        assert isinstance(params["rationale_text"], str)
        assert params["rationale_text"].strip() != ""
        assert params.get("mechanical") is not True


def test_decision_trace_orchestrator_trigger_mechanical(tmp_path, monkeypatch):
    """I 齐备进 B / 达上限转 E → trigger_source=orchestrator + mechanical。"""
    b0, d0 = _block(0), _draft(0)
    prop = _good_proposal(b0, d0)

    def chat_ok(prompt: str):
        if "layer recognition" in prompt:
            return prop
        return {"passed": True, "reason": "ok"}

    session, store, trace_dir = _make_i(
        monkeypatch, tmp_path, [b0], {0: d0}, chat_ok, run_id="run-mech-ok"
    )
    out = session.run()
    assert out["phase"] == PHASE_READY_FOR_B

    enter_b = []
    for path in trace_dir.glob("orchestrate_decision_*.json"):
        meta = json.loads(path.read_text(encoding="utf-8"))["meta"]
        if meta.get("decision_kind") == "enter_b":
            enter_b.append(meta)
    assert len(enter_b) == 1
    assert enter_b[0]["trigger_source"] == "orchestrator"
    assert enter_b[0]["decision_params"] == {"mechanical": True}

    # 达上限自主转 E
    def chat_fail(prompt: str):
        if "layer recognition" in prompt:
            return prop
        return {"passed": False, "reason": "no"}

    session2, _, trace_dir2 = _make_i(
        monkeypatch, tmp_path, [b0], {0: d0}, chat_fail, run_id="run-mech-e"
    )
    session2.run()
    hand_offs = []
    for path in trace_dir2.glob("orchestrate_decision_*.json"):
        meta = json.loads(path.read_text(encoding="utf-8"))["meta"]
        if meta.get("decision_kind") == "hand_off_e":
            hand_offs.append(meta)
    assert hand_offs
    for meta in hand_offs:
        assert meta["trigger_source"] == "orchestrator"
        assert meta["decision_params"] == {"mechanical": True}


def test_decision_trace_missing_trigger_source_audit(tmp_path, monkeypatch):
    """缺 trigger_source → cross_table_audit_structure_invalid。"""
    from pipeline.block_cross_table.orchestrate_decision_trace import (
        OrchestrateDecisionTracer,
    )

    _patch_env(monkeypatch, tmp_path)
    tracer = OrchestrateDecisionTracer("run-audit", lambda: "T")
    with pytest.raises(CrossTableError) as ei:
        tracer.write(
            "enter_b",
            "",  # 缺/空
            {"mechanical": True},
        )
    assert ei.value.code == "cross_table_audit_structure_invalid"
    with pytest.raises(CrossTableError) as ei2:
        tracer.write(
            "enter_b",
            "not_a_valid_source",
            {"mechanical": True},
        )
    assert ei2.value.code == "cross_table_audit_structure_invalid"
