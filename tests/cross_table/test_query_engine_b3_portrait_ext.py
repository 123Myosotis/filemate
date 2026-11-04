# -*- coding: utf-8 -*-
"""B3: portrait_query_extensions + F gate ①/② + QP preflight (contract enums)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline.block_cross_table.query_engine import (
    FAIL_LOCUS_PORTRAIT_GATE,
    FAIL_LOCUS_TERM_EXPAND,
    PortraitGateError,
    SUBREASON_EXTENSION_MISSING,
    SUBREASON_STRUCTURE_STATUS,
    SUBREASON_TERM_DOMAIN,
    SUBREASON_TIME_AXIS,
    SUBREASON_UNKNOWN,
    extract_portrait_query_extensions,
    gate_before_qe_critical_columns,
    gate_portrait_f1,
    gate_portrait_f2_authorized,
    qp_preflight_domain_cap,
    qp_preflight_time_axis,
    read_domain_cap_needs_user_statuses,
    read_structure_status_queryable,
    read_time_axis_required_fields,
)
from tests.cross_table.qe_test_helpers import current_slice

_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "query_engine"


def _load(name: str) -> dict:
    return json.loads((_FIX / name).read_text(encoding="utf-8"))


def _slice():
    return current_slice()


def _queryable():
    return read_structure_status_queryable(_slice())


def test_extract_extensions_ok():
    portrait = _load("portrait_ok.json")
    ext = extract_portrait_query_extensions(portrait)
    assert len(ext["queryable_columns"]) == 2
    assert len(ext["column_value_profiles"]) == 2
    assert "time_axis_candidates" in ext
    assert "join_key_candidates" in ext


def test_f1_pass_on_ok_portrait():
    portrait = _load("portrait_ok.json")
    ext = gate_portrait_f1(portrait, queryable_statuses=_queryable())
    assert ext["queryable_columns"]


def test_f1_fail_structure_status_subreason():
    portrait = _load("portrait_bad_structure.json")
    with pytest.raises(PortraitGateError) as ei:
        gate_portrait_f1(portrait, queryable_statuses=_queryable())
    assert ei.value.fail_locus == FAIL_LOCUS_PORTRAIT_GATE
    assert ei.value.subreason == SUBREASON_STRUCTURE_STATUS


def test_f1_fail_missing_column_value_profiles_key():
    portrait = _load("portrait_missing_ext.json")
    with pytest.raises(PortraitGateError) as ei:
        gate_portrait_f1(portrait, queryable_statuses=_queryable())
    assert ei.value.fail_locus == FAIL_LOCUS_PORTRAIT_GATE
    assert ei.value.subreason == SUBREASON_EXTENSION_MISSING


def test_f1_fail_empty_queryable():
    portrait = _load("portrait_ok.json")
    portrait["portrait_query_extensions"]["queryable_columns"] = []
    with pytest.raises(PortraitGateError) as ei:
        gate_portrait_f1(portrait, queryable_statuses=_queryable())
    assert ei.value.fail_locus == FAIL_LOCUS_PORTRAIT_GATE
    assert ei.value.subreason == SUBREASON_UNKNOWN


def test_f2_rejects_without_authorization():
    portrait = _load("portrait_ok.json")
    with pytest.raises(PortraitGateError) as ei:
        gate_portrait_f2_authorized(
            portrait,
            authorized=False,
            confirmed_bindings=[
                {
                    "binding_id": "b1",
                    "binding_payload": {"confirmed_column_ids": ["c1"]},
                }
            ],
        )
    assert ei.value.fail_locus == FAIL_LOCUS_PORTRAIT_GATE


def test_f2_rejects_without_confirmed_bindings():
    portrait = _load("portrait_ok.json")
    with pytest.raises(PortraitGateError) as ei:
        gate_portrait_f2_authorized(portrait, authorized=True, confirmed_bindings=[])
    assert ei.value.fail_locus == FAIL_LOCUS_PORTRAIT_GATE


def test_f2_pass_with_auth_and_bindings():
    portrait = _load("portrait_ok.json")
    ext = gate_portrait_f2_authorized(
        portrait,
        authorized=True,
        confirmed_bindings=[
            {
                "binding_id": "b1",
                "utterance_span_fingerprint": "fp",
                "binding_payload": {
                    "predicate_root": {"node_kind": "ATOM"},
                    "confirmed_column_ids": ["c1"],
                },
                "confirmed_at": "fixture",
            }
        ],
        referenced_critical_column_ids=["c1"],
    )
    assert "queryable_columns" in ext


def test_f2_unconfirmed_critical_column_rejected_before_qe():
    """补测 3：关键列未写入 ConfirmedSemanticBinding → 进 QE 前拒，fail_locus=portrait_gate。"""
    with pytest.raises(PortraitGateError) as ei:
        gate_before_qe_critical_columns(
            referenced_critical_column_ids=["c_critical_unbound"],
            confirmed_bindings=[
                {
                    "binding_id": "b_other",
                    "binding_payload": {"confirmed_column_ids": ["c_other"]},
                }
            ],
        )
    assert ei.value.fail_locus == FAIL_LOCUS_PORTRAIT_GATE
    assert ei.value.subreason == SUBREASON_UNKNOWN
    assert "c_critical_unbound" in str(ei.value.missing_detail)


def test_qp_preflight_over_cap_needs_user():
    """补测 4：domain_cap_status∈契约 needs_user 集 → QP needs_user + 缺项说明。"""
    portrait = _load("portrait_ok.json")
    portrait = copy.deepcopy(portrait)
    portrait["portrait_query_extensions"]["column_value_profiles"][0][
        "domain_cap_status"
    ] = "over_cap"
    triggers = read_domain_cap_needs_user_statuses(_slice())
    assert "over_cap" in triggers and "unavailable" in triggers
    verdict = qp_preflight_domain_cap(portrait, needs_user_statuses=triggers)
    assert verdict is not None
    assert verdict["status"] == "needs_user"
    assert verdict["fail_locus"] == FAIL_LOCUS_TERM_EXPAND
    assert verdict["subreason"] == SUBREASON_TERM_DOMAIN
    assert "over_cap" in verdict["missing_detail"]


def test_qp_preflight_unavailable_needs_user():
    """阻塞补测：domain_cap_status=unavailable → QP needs_user + 缺项说明（【补 B】）。"""
    portrait = _load("portrait_ok.json")
    portrait = copy.deepcopy(portrait)
    portrait["portrait_query_extensions"]["column_value_profiles"][0][
        "domain_cap_status"
    ] = "unavailable"
    triggers = read_domain_cap_needs_user_statuses(_slice())
    assert "unavailable" in triggers
    verdict = qp_preflight_domain_cap(portrait, needs_user_statuses=triggers)
    assert verdict is not None
    assert verdict["status"] == "needs_user"
    assert verdict["fail_locus"] == FAIL_LOCUS_TERM_EXPAND
    assert verdict["subreason"] == SUBREASON_TERM_DOMAIN
    assert "unavailable" in verdict["missing_detail"]


def test_qp_preflight_time_axis_missing_fields_needs_user():
    """补测 5：time_axis_candidates 缺 value_domain/hierarchy → needs_user + 缺项说明。"""
    portrait = _load("portrait_ok.json")
    portrait = copy.deepcopy(portrait)
    portrait["portrait_query_extensions"]["time_axis_candidates"] = [
        {
            "granularity": "day",
            "source_locator": {"column_id": "c_time"},
        }
    ]
    required = read_time_axis_required_fields(_slice())
    verdict = qp_preflight_time_axis(portrait, required_fields=required)
    assert verdict is not None
    assert verdict["status"] == "needs_user"
    assert verdict["fail_locus"] == FAIL_LOCUS_PORTRAIT_GATE
    assert verdict["subreason"] == SUBREASON_TIME_AXIS
    assert "value_domain" in verdict["missing_detail"]
    assert "hierarchy" in verdict["missing_detail"]
