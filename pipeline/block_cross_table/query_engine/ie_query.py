# -*- coding: utf-8 -*-
"""B5: I/E extensions for query sub-chain (fixture AI; no CURRENT merge)."""
from __future__ import annotations

from typing import Any, Callable, Mapping

from .errors import QueryEngineError
from .qp import ROLLBACK_QUERY_PLANNER_START, validate_query_plan_ast
from .qe import execute_query_plan
from .qr import (
    ACTION_ROLLBACK_QP,
    ACTION_ROUTE_E,
    E_ENTRY_WU,
    VERDICT_FAILED,
    VERDICT_NEEDS_USER,
    VERDICT_PASSED,
    run_query_review,
)
from .trace_query import QueryTraceSink

# E entries for query chain (IMPL §7.2).
E_ENTRY_DING = "E_entry_ding"  # 丁：QP needs_user
# E_ENTRY_WU already defined in qr (戊)

# Five-part ask: four legacy segments + explicit fifth (NOT merged into prompt text).
FIVE_PART_FIELD_KEYS = (
    "speculation",
    "evidence",
    "what_might_be_wrong",
    "next_step",
    "please_confirm_or_correct",
)

# Contract fail_locus enum (mechanical; not business prose).
CONTRACT_FAIL_LOCI = frozenset(
    {
        "predicate",
        "join_key",
        "time_window",
        "existence",
        "derive",
        "projection",
        "group_latest",
        "budget",
        "portrait_gate",
        "term_expand",
        "other",
    }
)


def map_fail_locus(raw_locus: object) -> str:
    """
    Map exception fail_locus for runner audit.
    Legal enum → as-is; missing/illegal → other (never invent projection).
    """
    text = str(raw_locus or "").strip()
    if text in CONTRACT_FAIL_LOCI:
        return text
    return "other"

USER_REPLY_CONFIRM = "confirm"
USER_REPLY_CORRECT = "correct"
USER_REPLY_ABORT = "abort"
USER_REPLY_KINDS = frozenset(
    {USER_REPLY_CONFIRM, USER_REPLY_CORRECT, USER_REPLY_ABORT}
)

# Budget-exhausted AI enum (local only executes; never raises structural error on counter).
BUDGET_VERDICT_CONTINUE = "continue_ask"
BUDGET_VERDICT_STOP = "stop_workbook"
BUDGET_VERDICT_SWITCH = "switch_path"
BUDGET_VERDICTS = frozenset(
    {
        BUDGET_VERDICT_CONTINUE,
        BUDGET_VERDICT_STOP,
        BUDGET_VERDICT_SWITCH,
    }
)

# Stage rollback points ①–③ (+ ownership ④ as parallel, not in stage enum).
STAGE_ROLLBACK_POINTS = frozenset(
    {
        "a_prime_recognition",
        "stage_start",
        "upstream_layout",
    }
)
OWNERSHIP_ROLLBACK_POINT = "cross_block_ownership"


class QueryPlanSession:
    """Tracks current plan; correction voids old plan and requires full replan."""

    def __init__(self) -> None:
        self.current_plan: dict[str, Any] | None = None
        self.current_plan_id: str | None = None
        self.voided_plan_ids: list[str] = []
        self.replan_required: bool = False

    def adopt_plan(self, plan: Mapping[str, Any], plan_id: str) -> None:
        self.current_plan = dict(plan)
        self.current_plan_id = str(plan_id)
        self.replan_required = False

    def void_current(self, *, reason: str) -> dict[str, Any]:
        old = self.current_plan_id
        if old:
            self.voided_plan_ids.append(old)
        self.current_plan = None
        self.current_plan_id = None
        self.replan_required = True
        return {
            "voided_plan_id": old,
            "reason": reason,
            "replan_required": True,
            "rollback_anchor": ROLLBACK_QUERY_PLANNER_START,
        }


def portraits_ready_for_query(
    portraits: list[Mapping[str, Any]],
    *,
    queryable_statuses: frozenset[str],
) -> bool:
    if not portraits:
        return False
    for p in portraits:
        if str(p.get("structure_status")) not in queryable_statuses:
            return False
    return True


def route_before_qp(
    portraits: list[dict[str, Any]],
    *,
    active_index: int = 0,
    rows_per_slice: int | None = None,
) -> dict[str, Any]:
    """
    I-layer bridge before qp_call_ai: oversized/truncated → slice_constraints.
    Does not call StagePlanSchema; reuses capacity windowing via i_slice_bridge.
    """
    from pipeline.block_cross_table.query_engine.i_slice_bridge import (
        apply_slice_route_to_portraits,
        build_i_slice_route,
    )

    route = build_i_slice_route(
        portraits, active_index=active_index, rows_per_slice=rows_per_slice
    )
    portraits_out = apply_slice_route_to_portraits(portraits, route)
    return {
        **route,
        "portraits": portraits_out,
    }


def build_five_part_ask(
    *,
    speculation: str,
    evidence: str,
    what_might_be_wrong: str,
    next_step: str,
    please_confirm_or_correct: str,
) -> dict[str, Any]:
    """
    Five-part envelope. Fifth segment is a dedicated field;
    must not be concatenated into a single prompt blob for the callback.
    """
    return {
        "speculation": speculation,
        "evidence": evidence,
        "what_might_be_wrong": what_might_be_wrong,
        "next_step": next_step,
        "please_confirm_or_correct": please_confirm_or_correct,
        # Explicit separation marker for E callback builders.
        "fifth_segment_separate": True,
        "prompt_parts_for_callback": [
            speculation,
            evidence,
            what_might_be_wrong,
            next_step,
        ],
        # Fifth is NOT in prompt_parts_for_callback.
    }


def validate_five_part_ask(envelope: Mapping[str, Any]) -> None:
    for key in FIVE_PART_FIELD_KEYS:
        val = envelope.get(key)
        if key not in envelope or val is None or (isinstance(val, str) and not val.strip()):
            raise QueryEngineError(
                "five_part missing field: %s" % key,
                fail_locus="other",
                subreason="five_part_empty",
                detail={"empty_field": key, "audit": "five_part_empty"},
            )
    if not envelope.get("fifth_segment_separate"):
        raise QueryEngineError(
            "fifth segment must be separate field",
            fail_locus="other",
        )
    prompt_parts = envelope.get("prompt_parts_for_callback") or []
    fifth = envelope.get("please_confirm_or_correct")
    if fifth in prompt_parts:
        raise QueryEngineError(
            "fifth segment must not be merged into prompt_parts",
            fail_locus="other",
        )


def assert_fail_locus_contract(fail_locus: Any) -> str:
    """Reject non-enum locus. Never map natural-language prose to an enum locally."""
    if not isinstance(fail_locus, str) or not fail_locus.strip():
        raise QueryEngineError(
            "fail_locus missing or empty",
            fail_locus="other",
            subreason="fail_locus_invalid",
            detail={"audit": "fail_locus_invalid"},
        )
    locus = fail_locus.strip()
    if locus not in CONTRACT_FAIL_LOCI:
        raise QueryEngineError(
            "fail_locus not in contract enum",
            fail_locus="other",
            subreason="fail_locus_invalid",
            detail={"audit": "fail_locus_invalid", "got": locus[:80]},
        )
    return locus


def five_part_from_ai_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """
    Build five-part envelope strictly from AI plan fields.
    Does not invent speculation/evidence text locally.
    """
    parts = {}
    for key in FIVE_PART_FIELD_KEYS:
        raw = plan.get(key)
        parts[key] = "" if raw is None else str(raw)
    return build_five_part_ask(
        speculation=parts["speculation"],
        evidence=parts["evidence"],
        what_might_be_wrong=parts["what_might_be_wrong"],
        next_step=parts["next_step"],
        please_confirm_or_correct=parts["please_confirm_or_correct"],
    )


def validate_needs_user_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Mechanical gate for status=needs_user: enum locus + non-empty AI five-part."""
    if str(plan.get("status") or "") != "needs_user":
        raise QueryEngineError(
            "validate_needs_user_plan requires status=needs_user",
            fail_locus="other",
        )
    locus = assert_fail_locus_contract(plan.get("fail_locus"))
    five = five_part_from_ai_plan(plan)
    validate_five_part_ask(five)
    return {"fail_locus": locus, "five_part": five}


def route_e_entry(
    *,
    source: str,
    five_part: Mapping[str, Any],
) -> dict[str, Any]:
    """source: qp_needs_user | qe_ask_user | qr_needs_user → 丁/戊."""
    validate_five_part_ask(five_part)
    if source == "qp_needs_user":
        entry = E_ENTRY_DING
    elif source in ("qe_ask_user", "qr_needs_user"):
        entry = E_ENTRY_WU
    else:
        raise QueryEngineError("unknown E source: %s" % source, fail_locus="other")
    return {
        "status": "needs_user",
        "e_entry": entry,
        "five_part": dict(five_part),
        "action": ACTION_ROUTE_E,
    }


def handle_ask_history_user_reply(
    *,
    user_reply_kind: str,
    user_reply: str,
    prior_needs_user: Mapping[str, Any],
    ask_history: list[dict[str, Any]] | None = None,
    turn_id: str | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    """
    Mechanical ask_history turn. abort → E exit, no replan, no binding persist,
    qp_call_allowed=False. confirm/correct → append turn, qp_call_allowed=True.
    """
    kind = str(user_reply_kind or "").strip()
    if kind not in USER_REPLY_KINDS:
        raise QueryEngineError(
            "user_reply_kind invalid",
            fail_locus="other",
            detail={"got": kind},
        )
    # prior must already carry AI five-part + enum locus (no local invention).
    validated = validate_needs_user_plan(prior_needs_user)
    history = [dict(x) for x in (ask_history or [])]
    turn = {
        "turn_id": str(turn_id or ("t_%s" % (len(history) + 1))),
        "at": at,
        "prior_needs_user": {
            "fail_locus": validated["fail_locus"],
            "five_part": dict(validated["five_part"]),
            "status": "needs_user",
        },
        "user_reply": str(user_reply or ""),
        "user_reply_kind": kind,
    }
    history.append(turn)
    if kind == USER_REPLY_ABORT:
        e_route = route_e_entry(
            source="qp_needs_user",
            five_part=validated["five_part"],
        )
        return {
            "status": "aborted",
            "action": "e_exit_abort",
            "ask_history": history,
            "replan": False,
            "persist_binding": False,
            "qp_call_allowed": False,
            "e_route": e_route,
            "audit": {
                "event": "ask_abort",
                "persist_binding": False,
                "replan": False,
            },
        }
    return {
        "status": "continue",
        "action": "replan_via_qp_call_ai",
        "ask_history": history,
        "replan": True,
        "persist_binding": False,
        "qp_call_allowed": True,
        "audit": {"event": "ask_%s" % kind},
    }


def apply_budget_exhausted_verdict(
    ai_verdict: str,
    *,
    trace_sink: QueryTraceSink | None = None,
    switch_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Local only executes AI enum. Counter exhaustion must NOT raise structural exception.
    """
    v = str(ai_verdict)
    if v not in BUDGET_VERDICTS:
        # Still not a structural counter exception — report as needs_user / other locus.
        result = {
            "status": "needs_user",
            "budget_verdict": None,
            "invalid_verdict": v,
            "structural_exception": False,
        }
    else:
        result = {
            "status": "budget_verdict_applied",
            "budget_verdict": v,
            "structural_exception": False,
            "local_action": v,
        }
    if trace_sink is not None:
        trace_sink.write(
            "query_budget_verdict",
            {
                "budget_verdict": result.get("budget_verdict"),
                "switch_state": dict(switch_state or {}),
                "structural_exception": False,
            },
        )
    return result


def assert_rollback_anchors_coexist(
    *,
    stage_anchors: set[str] | frozenset[str],
    query_anchors: set[str] | frozenset[str],
) -> None:
    """
    query_planner_start coexists with stage ①–④; must not mix namespaces
    (query anchors must not appear inside stage set and vice versa).
    """
    stage = set(str(x) for x in stage_anchors)
    query = set(str(x) for x in query_anchors)
    if ROLLBACK_QUERY_PLANNER_START not in query:
        raise QueryEngineError(
            "query anchors missing query_planner_start",
            fail_locus="other",
        )
    overlap = stage & query
    if overlap:
        raise QueryEngineError(
            "rollback anchor namespaces mixed: %s" % sorted(overlap),
            fail_locus="other",
        )
    # Stage set must not contain query planner start.
    if ROLLBACK_QUERY_PLANNER_START in stage:
        raise QueryEngineError(
            "query_planner_start must not be in stage rollback set",
            fail_locus="other",
        )


def apply_user_correction_void_plan(
    session: QueryPlanSession,
    *,
    correction_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """User correction → void old plan; QP must redo entire plan."""
    void_info = session.void_current(reason="user_correction")
    return {
        **void_info,
        "correction_payload": dict(correction_payload),
        "old_plan_void": True,
        "qp_must_replan_full": True,
    }


def run_query_subchain_i(
    *,
    portraits: list[Mapping[str, Any]],
    queryable_statuses: frozenset[str],
    catalog: dict[str, Any],
    plan: Mapping[str, Any] | None,
    plan_id: str,
    session: QueryPlanSession,
    tables_by_id: Mapping[str, dict[str, Any]],
    coeff_slots: Mapping[str, float],
    bound_eval: Mapping[str, Any],
    query_exec_budget: Mapping[str, Any],
    reconciliation_rows: list[dict[str, Any]] | None = None,
    verdict_fixture: Mapping[str, Any] | None = None,
    trace_sink: QueryTraceSink | None = None,
    ask_count: int = 0,
    ask_budget: int | None = None,
    budget_ai_verdict_fixture: str | None = None,
    qe_ask_user_loci: frozenset[str] | None = None,
    five_part_builder: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    I schedules QP→QE→QR after portraits ready.
    Budget touch-top → AI enum (fixture); local does not raise on counter.
    """
    if not portraits_ready_for_query(portraits, queryable_statuses=queryable_statuses):
        return {
            "status": "blocked",
            "reason": "portraits_not_ready",
            "allow_enter_b": False,
        }

    if ask_budget is not None and ask_count >= ask_budget:
        # Do NOT raise structural exception — AI enum path.
        verdict = budget_ai_verdict_fixture or BUDGET_VERDICT_STOP
        applied = apply_budget_exhausted_verdict(verdict, trace_sink=trace_sink)
        return {**applied, "status": "budget_exhausted", "allow_enter_b": False}

    if session.replan_required and plan is None:
        return {
            "status": "needs_replan",
            "rollback_anchor": ROLLBACK_QUERY_PLANNER_START,
            "allow_enter_b": False,
        }

    if plan is None:
        raise QueryEngineError("plan required", fail_locus="other")

    # QP validate (fixture plan stands in for AI QP).
    qp_out = validate_query_plan_ast(plan, catalog)
    if qp_out.get("status") == "needs_user":
        five = (five_part_builder or _default_five)(
            source="qp_needs_user",
            detail="qp_needs_user",
        )
        routed = route_e_entry(source="qp_needs_user", five_part=five)
        if trace_sink is not None:
            trace_sink.write("query_plan", {"status": "needs_user"})
        return {**routed, "allow_enter_b": False}

    session.adopt_plan(qp_out["plan"], plan_id)
    if trace_sink is not None:
        trace_sink.write("query_plan", {"status": "plan_ready", "plan_id": plan_id})

    qe_out = execute_query_plan(
        session.current_plan or {},
        catalog,
        tables_by_id=tables_by_id,
        coeff_slots=coeff_slots,
        bound_eval=bound_eval,
        query_exec_budget=query_exec_budget,
        trace_sink=trace_sink,
    )
    if qe_out.get("status") == "failed":
        locus = str(qe_out.get("fail_locus") or "other")
        ask_loci = qe_ask_user_loci or frozenset({"join_key", "time_window", "existence"})
        if locus in ask_loci:
            five = (five_part_builder or _default_five)(
                source="qe_ask_user",
                detail="fail_locus=%s" % locus,
            )
            routed = route_e_entry(source="qe_ask_user", five_part=five)
            return {
                **routed,
                "qe": qe_out,
                "allow_enter_b": False,
            }
        # Mechanical fail not askable → report to I, no enter B.
        return {
            "status": "failed",
            "qe": qe_out,
            "report_to_i": True,
            "allow_enter_b": False,
        }

    qr_out = run_query_review(
        session.current_plan or {},
        qe_out,
        reconciliation_rows=reconciliation_rows,
        verdict_fixture=verdict_fixture or {"verdict": VERDICT_PASSED},
        trace_sink=trace_sink,
    )
    if qr_out.get("verdict") == VERDICT_NEEDS_USER:
        five = (five_part_builder or _default_five)(
            source="qr_needs_user",
            detail="qr_needs_user",
        )
        routed = route_e_entry(source="qr_needs_user", five_part=five)
        return {**routed, "qr": qr_out, "qe": qe_out, "allow_enter_b": False}

    if qr_out.get("verdict") == VERDICT_FAILED:
        void_info = session.void_current(reason="review_failed")
        return {
            "status": VERDICT_FAILED,
            "action": ACTION_ROLLBACK_QP,
            "rollback_anchor": ROLLBACK_QUERY_PLANNER_START,
            "void": void_info,
            "qr": qr_out,
            "qe": qe_out,
            "allow_enter_b": False,
        }

    return {
        "status": VERDICT_PASSED,
        "qe": qe_out,
        "qr": qr_out,
        "allow_enter_b": True,
        "plan_id": session.current_plan_id,
    }


def _default_five(*, source: str, detail: str) -> dict[str, Any]:
    return build_five_part_ask(
        speculation="query_subchain_%s" % source,
        evidence=detail,
        what_might_be_wrong="capability_or_binding_gap",
        next_step="await_user_confirm_or_correct",
        please_confirm_or_correct="please_confirm_or_correct",
    )
