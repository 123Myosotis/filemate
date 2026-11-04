# -*- coding: utf-8 -*-
"""One-shot: copy v1.8.0 → v1.9.0 and merge query-engine contract (no fixture numerics)."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "contracts" / "versions" / "v1.8.0.json"
DST = ROOT / "contracts" / "versions" / "v1.9.0.json"

QUERY_TRACE_KINDS = [
    "query_plan",
    "query_exec_step",
    "query_review",
    "query_user_reply",
    "query_budget_verdict",
]

# Load op catalog fixture (no coeffs).
OPS = json.loads(
    (ROOT / "tests" / "fixtures" / "query_engine" / "op_catalog_fixture.json").read_text(
        encoding="utf-8"
    )
)["operators"]


def main() -> None:
    data = json.loads(SRC.read_text(encoding="utf-8"))
    data = deepcopy(data)
    data["contract_version"] = "v1.9.0"
    data["published_at"] = "2026-10-21T00:00:00Z"
    data["compat_window_until_version"] = "v1.8.0"
    data["min_acceptable_version"] = "v1.1.0"

    ct = data["cross_table"]
    enums = ct["enums"]

    # --- enums (B1 + post-pass patches); no memory_*/embedding ---
    enums["structure_status_queryable"] = ["layers_recognized"]
    enums["domain_cap_status"] = ["within_cap", "over_cap", "unavailable"]
    enums["domain_cap_needs_user_statuses"] = ["over_cap", "unavailable"]
    enums["PortraitGateSubreason"] = [
        "structure_status",
        "time_axis",
        "term_domain",
        "join_key",
        "extension_missing",
        "unknown",
    ]
    enums["QueryFailLocus"] = [
        "term_expand",
        "predicate",
        "time_window",
        "interval_build",
        "join_key",
        "derive",
        "existence",
        "group_latest",
        "projection",
        "portrait_gate",
        "budget",
        "other",
    ]
    enums["query_time_basis"] = ["wall_clock_now", "data_max_time"]
    enums["query_rollback_anchor"] = ["query_planner_start"]
    enums["query_delivery_mode"] = ["direct_result", "structure_via_b_s"]
    enums["query_conflict_kind"] = [
        "query_term_unmapped",
        "query_predicate_contradiction",
        "query_time_axis_missing",
        "query_join_key_absent",
        "query_utterance_ambiguous",
        "query_step_input_empty",
        "query_op_uncertain",
    ]
    enums["column_name_source"] = [
        "from_portrait",
        "from_ai_runtime",
        "from_user_confirmed",
    ]
    enums["cardinality_class"] = ["low", "high"]
    enums["complexity_bound_class"] = [
        "near_linear_rows",
        "near_linear_cells_or_rows",
        "declared_join",
        "stamp_window_declared",
        "nlogn_declared",
        "group_times_size",
        "near_linear_declared",
    ]
    enums["QueryBudgetExhaustedVerdict"] = [
        "continue_ask",
        "stop_workbook",
        "switch_path",
    ]
    enums["QueryReviewVerdict"] = [
        "review_passed",
        "review_failed",
        "needs_user",
    ]
    enums["query_condition_node_kind"] = [
        "AND",
        "OR",
        "NOT",
        "ATOM",
        "EXISTS",
        "ANY",
    ]
    enums["query_trace_kind"] = list(QUERY_TRACE_KINDS)

    # Extend routing_trace_kind (do not remove old values).
    rtk = list(enums["routing_trace_kind"])
    for k in QUERY_TRACE_KINDS:
        if k not in rtk:
            rtk.append(k)
    enums["routing_trace_kind"] = rtk

    # --- structural slots (numerics null / empty; budget card later) ---
    ct["time_axis_candidate_required_fields"] = [
        "value_domain",
        "granularity",
        "hierarchy",
        "source_locator",
    ]
    ct["complexity_bound_eval"] = {
        "near_linear_rows": {"form": "linear", "scale_key": "nrows"},
        "near_linear_cells_or_rows": {
            "form": "max_pair",
            "scale_keys": ["ncells", "nrows"],
        },
        "declared_join": {"form": "sum_pair", "scale_keys": ["n_left", "n_right"]},
        "stamp_window_declared": {"form": "linear", "scale_key": "nrows"},
        "nlogn_declared": {"form": "nlogn", "scale_key": "nrows"},
        "group_times_size": {"form": "linear", "scale_key": "group_size_sum"},
        "near_linear_declared": {"form": "linear", "scale_key": "nrows"},
    }
    # Numeric slots: structure only, values null (do not copy fixture numbers).
    ct["complexity_bound_coeffs"] = {cls: None for cls in enums["complexity_bound_class"]}
    ct["query_exec_budget"] = {
        "unit": None,
        "max_cost_per_step": None,
        "max_cost_per_plan": None,
    }
    ct["query_ask_budget"] = None
    ct["query_replan_budget"] = None
    ct["term_expand_domain_enum_cap"] = None

    ct["query_operator_catalog"] = deepcopy(OPS)
    ct["query_operator_catalog_notes"] = {
        "complexity_bound_required": True,
        "coeff_lives_in_complexity_bound_coeffs_slot": True,
        "result_projection_is_not_an_executable_op": True,
        "sort_truncate_forbids_as_group_latest": True,
        "GroupTopK_ArgExtremum_names": "schematic; retained per B1 review",
    }

    # Schema abstracts (required fields conceptual; JSON-schema-lite).
    ct["ResultProjection"] = {
        "note": "Plan-level schema on QueryPlanAst.result_projection; NOT an executable catalog op; QP must not select as steps[].op",
        "required": ["columns"],
        "properties": {
            "columns": {
                "type": "array",
                "items": {"$ref": "ResultProjectionColumn"},
            }
        },
    }
    ct["ResultProjectionColumn"] = {
        "required": [
            "column_id",
            "display_name",
            "column_name_source",
            "source_step_id",
            "provenance",
        ]
    }
    ct["QueryPlanAst"] = {
        "required": [
            "steps",
            "edges",
            "result_projection",
            "query_time_basis",
            "demand_points",
            "rollback_anchors",
        ]
    }
    ct["QueryPlanStep"] = {
        "required": [
            "step_id",
            "op",
            "args",
            "inputs",
            "output",
            "provenance",
        ]
    }
    ct["QueryConditionTree"] = {
        "required": ["node_kind"],
        "node_kind_enum": "query_condition_node_kind",
    }
    ct["QueryProvenanceRef"] = {
        "required_any_of": ["utterance_span", "portrait_path"]
    }
    ct["ConfirmedSemanticBinding"] = {
        "required": [
            "binding_id",
            "utterance_span_fingerprint",
            "binding_payload",
            "confirmed_at",
        ]
    }
    ct["BindingPayload"] = {
        "required": ["predicate_root"],
        "note": "predicate tree root; optional join/window interpretation + node provenance",
    }
    ct["portrait_query_extensions"] = {
        "required": [
            "queryable_columns",
            "column_value_profiles",
            "time_axis_candidates",
            "join_key_candidates",
        ]
    }
    ct["QueryableColumn"] = {
        "required": ["column_id", "type_class"]
    }
    ct["ColumnValueProfile"] = {
        "required": [
            "column_id",
            "cardinality_class",
            "domain_cap_status",
        ]
    }
    ct["TimeAxisCandidate"] = {
        "required": [
            "value_domain",
            "granularity",
            "hierarchy",
            "source_locator",
        ]
    }
    ct["JoinKeyCandidate"] = {
        "required": ["left_column_ref", "right_column_ref"]
    }
    ct["QueryExecStepTrace"] = {
        "required": ["step_id", "status"]
    }
    ct["PortraitGateNeedUser"] = {
        "required": ["fail_locus", "subreason"],
        "fail_locus_fixed": "portrait_gate",
    }
    ct["query_engine_merge_note"] = (
        "Merged from QUERY_ENGINE_CONTRACT_DRAFT_B1 (+ structure_status_queryable, "
        "domain_cap_needs_user_statuses). Numeric slots null pending QUERY_ENGINE_BUDGET_CARD. "
        "No memory_*/embedding keys."
    )

    DST.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("wrote", DST)


if __name__ == "__main__":
    main()
