# -*- coding: utf-8 -*-
"""
Gate-passed plan: triage, optional execute, summary or confirmation.
execution_status is success | summary_failed | complex.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Optional

from .evidence_path import evidence_body, evidence_relpath, write_evidence_file
from .report_render import render_report
from .triage import triage_after_execute, triage_plan
from .triage_confirm import ACTIONS_BEFORE, ACTIONS_LATE, build_complex_confirmation


def run_triaged(tables: dict, plan: dict, notes: list, cv,
                ai_confirm: Optional[Callable] = None,
                ai_summary: Optional[Callable] = None,
                execute_mechanical: Optional[Callable] = None) -> dict:
    pre = triage_plan(plan)
    if pre["triage"] == "complex":
        if str(plan.get("status") or "") == "ready":
            plan["status"] = "await_user"
        conf = build_complex_confirmation(
            plan, pre, ai_complete=ai_confirm, action_set=ACTIONS_BEFORE, lock_status="pending",
        )
        return {
            "name": "合成表",
            "columns": [],
            "rows": [],
            "plan_notes": notes,
            "contract_rejected": False,
            "contract_status": "await_user",
            "execution_status": "complex",
            "triage": pre,
            "complex_confirmation": conf,
            "traces": [],
            "trace_ok": False,
            "_mechanical": False,
        }

    if execute_mechanical is None:
        from .executor import execute_mechanical as execute_mechanical
    out = execute_mechanical(tables, plan, notes, cv=cv)
    _absorb_mismatch_notes(plan, out)
    post = triage_after_execute(plan, out)
    if post["triage"] == "complex":
        conf = build_complex_confirmation(
            plan, post, ai_complete=ai_confirm, action_set=ACTIONS_LATE,
            lock_status="rolled_back", execution_result=out,
        )
        return {
            "name": "合成表",
            "columns": [],
            "rows": [],
            "withheld_rows": out.get("rows") or [],
            "plan_notes": notes,
            "contract_rejected": True,
            "contract_status": "rollback",
            "execution_status": "complex",
            "triage_runtime_escalation": True,
            "triage": post,
            "complex_confirmation": conf,
            "traces": [],
            "trace_ok": False,
            "_mechanical": False,
        }

    summary = _summary_or_none(plan, out, ai_summary)
    if summary is None:
        out["execution_status"] = "summary_failed"
        out["execution_summary"] = None
        out["delivery_success"] = False
        return out
    body = evidence_body(plan, summary)
    dest_root = os.environ.get("FILEMATE_REPORT_ROOT") or os.environ.get(
        "FILEMATE_ACCEPTANCE_DIR"
    )
    try:
        wrote, err = write_evidence_file(
            plan, body, str(summary.get("evidence_path") or ""), dest_root=dest_root,
        )
    except Exception:
        wrote, err = False, "evidence_write_failed exception"
    if not wrote:
        notes = plan.get("runtime_notes")
        if not isinstance(notes, list):
            notes = []
            plan["runtime_notes"] = notes
        token = err or "evidence_write_failed"
        if token not in notes:
            notes.append(token)
        out["execution_status"] = "summary_failed"
        out["execution_summary"] = None
        out["delivery_success"] = False
        out["evidence"] = body
        return out
    out["execution_status"] = "success"
    out["execution_summary"] = summary
    out["delivery_success"] = True
    out["evidence"] = body
    rendered = render_report(summary, body, dest_root=dest_root)
    out["report_status"] = rendered["report_status"]
    out["report_html_path"] = rendered["report_html_path"]
    out["report_text_path"] = rendered["report_text_path"]
    return out


def _absorb_mismatch_notes(plan: dict, out: dict) -> None:
    bucket = plan.get("runtime_notes")
    if not isinstance(bucket, list):
        bucket = []
        plan["runtime_notes"] = bucket
    for line in list(out.get("plan_notes") or []):
        if isinstance(line, str) and line.startswith("shared_map_mismatch"):
            if line not in bucket:
                bucket.append(line)


def _ops_present(plan: dict) -> list:
    ops = []
    for step in plan.get("ops") or []:
        if isinstance(step, dict) and step.get("op"):
            ops.append(str(step["op"]))
    return sorted(set(ops))


def _summary_or_none(plan: dict, out: dict, ai_summary: Optional[Callable]) -> Optional[dict]:
    rows = out.get("rows") or []
    cols = out.get("columns") or []
    sample_ref = {"sample_coords": ["row:0"] if rows else ["row:none"]}
    if not rows:
        sample_ref = {"sample_manifest_id": "empty-output"}
    base = {
        "schema_version": "d3-summary-1",
        "contract_version": plan.get("contract_version"),
        "contract_hash": plan.get("contract_hash"),
        "triage": "simple",
        "fired_signals": [],
        "primary_table": str(plan.get("primary_table") or "").strip(),
        "ops_present": _ops_present(plan),
        "row_count_out": len(rows),
        "column_count_out": len(cols),
        "sample_ref": sample_ref,
        "evidence_path": evidence_relpath(plan),
    }
    if not base["primary_table"]:
        return None
    for _attempt in range(2):
        text = ""
        if ai_summary is None:
            break
        try:
            text = ai_summary(base)
        except Exception:
            text = ""
        if isinstance(text, str) and text.strip():
            base["human_text"] = text
            if _summary_ok(base):
                return base
    return None


def _summary_ok(summary: dict) -> bool:
    required = (
        "schema_version", "contract_version", "contract_hash", "triage",
        "fired_signals", "primary_table", "ops_present", "row_count_out",
        "column_count_out", "sample_ref", "evidence_path", "human_text",
    )
    for k in required:
        if k not in summary:
            return False
    if summary.get("schema_version") != "d3-summary-1":
        return False
    if summary.get("triage") != "simple" or summary.get("fired_signals") != []:
        return False
    if not isinstance(summary.get("human_text"), str) or not summary["human_text"].strip():
        return False
    ref = summary.get("sample_ref") or {}
    mid = ref.get("sample_manifest_id")
    coords = ref.get("sample_coords")
    if not ((isinstance(mid, str) and mid.strip()) or (isinstance(coords, list) and coords)):
        return False
    if not str(summary.get("evidence_path") or "").startswith("evidence/"):
        return False
    return True


def summary_json(summary: dict) -> str:
    return json.dumps(summary, ensure_ascii=False, sort_keys=True)
