# -*- coding: utf-8 -*-
"""全链整合：A'→A→I→B→S→E 脚本化路径 + 六类刁难 + trace 检索。

真实工作簿路径用构造表（注明非用户原表）+ 验收目录既有 xlsx 做一次冒烟。
模型侧用脚本化 chat：本地不做语义判断。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

import config
from pipeline.block_cross_table.conflict_e import ConflictE, run_conflict_e
from pipeline.block_cross_table.join_hypothesis import reset_trace_seq, write_trace
from pipeline.block_cross_table.portrait_store_fs import FilePortraitStore
from pipeline.block_cross_table.portrait_write import write_block_portrait
from pipeline.block_cross_table.stage_exec import run_stage_exec
from pipeline.block_cross_table.stage_plan import ROLLBACK_OWNERSHIP, ROLLBACK_STAGE


ROOT = Path(__file__).resolve().parents[2]
ACCEPT = ROOT / "acceptance" / "cross_table"
TRACE_ROOT = ROOT / ".pytest_trace_tmp"


def _patch_trace(monkeypatch, tmp_path: Path, name: str = "fc"):
    reset_trace_seq()
    trace_dir = TRACE_ROOT / tmp_path.name / name
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


def _four(rp=ROLLBACK_STAGE):
    return {
        "推测": "model_speculation",
        "依据": "model_evidence",
        "可能错在哪": "model_alt",
        "下一步": {"rollback_point": rp},
    }


def _ok_layer():
    return {
        "rect_to_layer": {"r0": "L0"},
        "layer_order": ["L0"],
        "layers": [{"layer_id": "L0"}],
        "subtable_boundaries": [{"subtable_id": 0}],
        "nesting_facts": [],
        "group_label_declaration": [],
        "dimension_membership": [
            {"dimension_field_id": "d0", "subtable_id": 0, "layer_ids": ["L0"]}
        ],
        "header_completeness": "complete",
    }


def _stage():
    return {
        "stage_id": "s0",
        "parent_table_ids": ["t0"],
        "block_ids": [0],
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


def _plan():
    return {
        "stages": [_stage()],
        "plan_generation": 1,
        "review_result": {"needs_user_ask": True, "passed": True},
    }


def _search_traces(trace_dir: Path, **want):
    hits = []
    for p in sorted(trace_dir.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        meta = data.get("meta") or {}
        ok = True
        for k, v in want.items():
            if meta.get(k) != v and data.get(k) != v:
                ok = False
                break
        if ok:
            hits.append(p)
    return hits


def test_full_chain_smoke_portrait_to_e(tmp_path, monkeypatch):
    """真实表冒烟：验收目录 xlsx 存在 + A 写画像 → S → E。"""
    trace_dir = _patch_trace(monkeypatch, tmp_path, "smoke")
    real = ACCEPT / "inputs" / "交易流水.xlsx"
    assert real.is_file()
    wb = load_workbook(real, read_only=True)
    assert wb.active.max_row >= 1

    portrait = TRACE_ROOT / tmp_path.name / "portrait"
    store = FilePortraitStore(portrait, clear=True)
    write_block_portrait(
        {"status": "ok", "block_id": 0, "layer_result": _ok_layer()},
        store,
    )

    e = ConflictE(
        run_id="full-smoke",
        chat=lambda p: _four(),
        conflict_ask_max=2,
        clock=lambda: "FSM",
        auto_user_decision={"reply_kind": "continue"},
    )

    phase = {"ai": True}

    def chat(prompt):
        if "review" in prompt:
            return {"passed": False, "reason": "need confirm", "needs_user_ask": True}
        return {"ok": True, "artifact": {"note": "from_real_sheet"}, "decision_kind": "enter_s"}

    out = run_stage_exec(
        run_id="full-smoke",
        plan=_plan(),
        chat=chat,
        e_handler=e,
        plan_regenerate_max=2,
        clock=lambda: "FSM",
    )
    assert out["status"] == "hand_off_e"
    assert e.received[0]["kind"] == "review_failed_ask_user"
    # trace：run_id 可检索 E
    e_hits = list(trace_dir.glob("conflict_*.json"))
    assert e_hits, "expected conflict traces"


def _hard_case_via_e(tmp_path, monkeypatch, case_id: str, body: dict, user_reply: dict):
    """六类刁难共用：包进 E → 四段 → 用户纠正/继续；断言本地未做语义合并。"""
    trace_dir = _patch_trace(monkeypatch, tmp_path, case_id)
    seen_prompts = []

    def chat(prompt):
        seen_prompts.append(prompt)
        # 纠正轮：原文必须出现在 prompt（E 不解析）
        return _four()

    out = run_conflict_e(
        envelope={
            "run_id": "hard-%s" % case_id,
            "entry_kind": "from_s_stage",
            "conflict_session_seq": 1,
            "trigger_source": "S",
            "body": body,
        },
        chat=chat,
        user_callback=lambda fp: user_reply,
        conflict_ask_max=3,
        clock=lambda: "H%s" % case_id[:4],
    )
    # 全程本地未做语义判断：prompt 只搬运，不出现「默认合并」类本地裁定
    joined = "\n".join(seen_prompts)
    assert "默认合并" not in joined
    assert "本地裁定对错" not in joined
    assert out.get("four_part") or out.get("decision")
    return out, trace_dir, seen_prompts


def test_hard_1_semantic_similar(tmp_path, monkeypatch):
    """刁难1：表面相似字段标题 → 须确认（走 E），非默认绑定。"""
    _patch_trace(monkeypatch, tmp_path, "h1")
    body = {
        "kind": "review_failed_ask_user",
        "stage_id": "s0",
        "locator_ids": ["col:客户名称", "col:客户名"],
        "review_raw": {
            "passed": False,
            "reason": "titles look similar but semantics unclear",
            "needs_user_ask": True,
        },
        "trigger": "review_failed_ask_user",
        "case_tag": "semantic_similar",
    }
    state = {"n": 0}
    prompts = []

    def chat(prompt):
        prompts.append(prompt)
        return _four()

    def user(fp):
        state["n"] += 1
        if state["n"] == 1:
            return {
                "reply_kind": "correct_and_reask",
                "text": "左侧是开户名，右侧是交易对手简称，勿合并",
            }
        return {"reply_kind": "continue"}

    out = run_conflict_e(
        envelope={
            "run_id": "hard-h1",
            "entry_kind": "from_s_stage",
            "conflict_session_seq": 1,
            "trigger_source": "S",
            "body": body,
        },
        chat=chat,
        user_callback=user,
        conflict_ask_max=4,
        clock=lambda: "H1",
    )
    assert out["decision"] == "continue"
    assert state["n"] == 2
    assert any("勿合并" in p for p in prompts)
    assert all("默认合并" not in p for p in prompts)


def test_hard_2_same_name_different_sense(tmp_path, monkeypatch):
    body = {
        "kind": "missing_field",
        "missing_field": "needs_user_ask",
        "review_raw": {"passed": True, "note": "账户：科目 vs 账户：银行账号"},
        "stage_id": "s0",
        "locator_ids": ["dim:账户"],
        "case_tag": "same_name_diff_sense",
    }
    out, _, _ = _hard_case_via_e(
        tmp_path,
        monkeypatch,
        "h2",
        body,
        {"reply_kind": "continue"},
    )
    assert out["decision"] == "continue"
    assert out["status"] == "continue"


def test_hard_3_utterance_ambiguity(tmp_path, monkeypatch):
    body = {
        "kind": "review_failed_ask_user",
        "stage_id": "s0",
        "locator_ids": ["utt:把明细和流水对上"],
        "review_raw": {
            "passed": False,
            "reason": "subject/object and grain unclear",
            "needs_user_ask": True,
        },
        "trigger": "review_failed_ask_user",
        "case_tag": "utterance_ambiguity",
    }
    out, _, _ = _hard_case_via_e(
        tmp_path,
        monkeypatch,
        "h3",
        body,
        {"reply_kind": "rollback", "rollback_point": ROLLBACK_STAGE},
    )
    assert out["decision"] == "rollback"


def test_hard_4_nonstandard_structure_constructed_workbook(tmp_path, monkeypatch):
    """构造工作簿（合并/隐藏行）— 注明非用户原表。"""
    _patch_trace(monkeypatch, tmp_path, "h4")
    path = tmp_path / "constructed_nonstandard.xlsx"
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "科目"
    ws["B1"] = "金额"
    ws.merge_cells("A2:A3")
    ws["A2"] = "合并单元格示意"
    ws.row_dimensions[4].hidden = True
    ws["A4"] = "隐藏行"
    wb.save(path)
    # 报告字段
    note = {
        "workbook": str(path),
        "note": "构造的，不是用户原表",
        "features": ["merged_cells", "hidden_row"],
    }
    body = {
        "kind": "stage_ai_structure_exhausted",
        "invalid_reason": "nonstandard_layout_needs_confirm",
        "stage_id": "s0",
        "locator_ids": ["sheet:constructed"],
        "attempts": 2,
        "constructed_workbook_meta": note,
    }
    out, _, _ = _hard_case_via_e(
        tmp_path,
        monkeypatch,
        "h4b",
        body,
        {"reply_kind": "stop_workbook"},
    )
    assert note["note"] == "构造的，不是用户原表"
    assert out["decision"] == "stop_workbook"


def test_hard_5_cross_block_ownership_four(tmp_path, monkeypatch):
    body = {
        "kind": "cross_block_ownership",
        "rollback_point": ROLLBACK_OWNERSHIP,
        "input_block_ids": ["0", "1"],
        "conflicts": [
            {
                "dimension_field_id": "币种",
                "blocks": [
                    {"block_id": "0", "layer_ids": ["L0"]},
                    {"block_id": "1", "layer_ids": ["L9"]},
                ],
            }
        ],
        "adjudication": None,
        "case_tag": "cross_block_ownership",
    }
    out, trace_dir, _ = _hard_case_via_e(
        tmp_path,
        monkeypatch,
        "h5",
        body,
        {"reply_kind": "rollback", "rollback_point": ROLLBACK_OWNERSHIP},
    )
    assert out["decision"] == "rollback"
    assert out["rollback_point"] == ROLLBACK_OWNERSHIP
    assert out["scope"]["input_block_ids"] == ["0", "1"]


def test_hard_6_multidim_same_name(tmp_path, monkeypatch):
    body = {
        "kind": "review_failed_ask_user",
        "stage_id": "s0",
        "locator_ids": ["sub:0:dim:产品", "sub:1:dim:产品"],
        "review_raw": {
            "passed": False,
            "reason": "same dimension name across subtables",
            "needs_user_ask": True,
        },
        "trigger": "review_failed_ask_user",
        "case_tag": "multidim_same_name",
    }
    out, _, _ = _hard_case_via_e(
        tmp_path,
        monkeypatch,
        "h6",
        body,
        {"reply_kind": "continue"},
    )
    assert out["decision"] == "continue"


def test_trace_retrievable_by_run_and_stage(tmp_path, monkeypatch):
    """按 run_id / stage_id 检索 S+E 落点。"""
    trace_dir = _patch_trace(monkeypatch, tmp_path, "retr")
    run_id = "retr-run-1"
    # 手工写一条 S 风格 + 跑 E
    write_trace(
        "stage_exec_model",
        {
            "meta": {
                "run_id": run_id,
                "stage_id": "s0",
                "call_kind": "stage_exec_model",
                "kind_seq": 1,
            }
        },
        lambda: "R1",
    )
    e = ConflictE(
        run_id=run_id,
        chat=lambda p: _four(),
        conflict_ask_max=2,
        clock=lambda: "R2",
        auto_user_decision={"reply_kind": "stop_workbook"},
        session_seq_start=1,
    )
    e.handle(
        {
            "kind": "missing_field",
            "missing_field": "needs_user_ask",
            "review_raw": {},
            "stage_id": "s0",
            "locator_ids": [],
        }
    )
    # 检索命令等价实现
    by_run = []
    by_stage = []
    for p in trace_dir.glob("*.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        meta = data.get("meta") or {}
        if meta.get("run_id") == run_id:
            by_run.append(p.name)
        if meta.get("run_id") == run_id and meta.get("stage_id") == "s0":
            by_stage.append(p.name)
    assert any("stage_exec" in n for n in by_run)
    assert any("conflict" in n for n in by_run)
    assert by_stage  # S 落点含 stage_id
    # E 可经 stage_ref
    e_stage = []
    for p in trace_dir.glob("conflict_*.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        meta = data.get("meta") or {}
        if meta.get("run_id") == run_id and meta.get("stage_ref") == "s0":
            e_stage.append(p.name)
    assert e_stage
