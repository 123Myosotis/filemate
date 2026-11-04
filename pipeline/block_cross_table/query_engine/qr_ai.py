# -*- coding: utf-8 -*-
"""B6 QR: live AI verdict + local mechanical standards (does not alter existing prompts)."""
from __future__ import annotations

import json
from typing import Any, Mapping

from ai.client import chat_json

from .errors import QueryEngineError
from .qr import (
    VERDICT_FAILED,
    VERDICT_NEEDS_USER,
    VERDICT_PASSED,
    run_query_review,
)

QR_SYSTEM = """You are a query reviewer. Output ONE JSON object only with key verdict in {review_passed, review_failed, needs_user} and optional reason. Judge structural completeness of plan vs exec only; no business-domain rules."""


def qr_call_ai(
    plan: Mapping[str, Any],
    exec_result: Mapping[str, Any],
    *,
    api_key: str,
    reconciliation_rows: list[dict[str, Any]] | None = None,
    trace_sink: Any = None,
) -> dict[str, Any]:
    prompt = json.dumps(
        {
            "plan_step_count": len(plan.get("steps") or []),
            "exec_status": exec_result.get("status"),
            "steps_executed": exec_result.get("steps_executed"),
            "fail_locus": exec_result.get("fail_locus"),
            "demand_point_count": len(plan.get("demand_points") or []),
        },
        ensure_ascii=False,
    )
    ai_calls = 1
    # Dedicated run_id so QR ok_trace is separable; raw_redacted now persisted by chat_json.
    raw = chat_json(
        prompt,
        api_key,
        system=QR_SYSTEM,
        trace_run_id="qr_review",
    )
    if not isinstance(raw, dict) or "verdict" not in raw:
        # Local-only path: if exec ok, propose passed; else failed.
        if exec_result.get("status") == "ok":
            verdict_fixture = {"verdict": VERDICT_PASSED, "reason": "fallback_local_exec_ok"}
        else:
            verdict_fixture = {"verdict": VERDICT_FAILED, "reason": "fallback_local_exec_fail"}
        ai_calls = 1  # still counted attempt
        last_err = str(getattr(chat_json, "last_error", "") or "invalid_verdict")
    else:
        v = str(raw.get("verdict"))
        if v not in (VERDICT_PASSED, VERDICT_FAILED, VERDICT_NEEDS_USER):
            raise QueryEngineError("qr_ai invalid verdict", fail_locus="other")
        verdict_fixture = dict(raw)
        last_err = ""

    out = run_query_review(
        plan,
        exec_result,
        reconciliation_rows=reconciliation_rows,
        verdict_fixture=verdict_fixture,
        trace_sink=trace_sink,
        force_local_checks=True,
    )
    out["ai_calls"] = ai_calls
    if last_err:
        out["ai_fallback"] = last_err
    return out
