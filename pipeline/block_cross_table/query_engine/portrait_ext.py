# -*- coding: utf-8 -*-
"""B3: portrait_query_extensions + F gate ①/② (contract-driven enums; not CURRENT)."""
from __future__ import annotations

from typing import Any, Collection, Iterable, Mapping

FAIL_LOCUS_PORTRAIT_GATE = "portrait_gate"
FAIL_LOCUS_TERM_EXPAND = "term_expand"
SUBREASON_STRUCTURE_STATUS = "structure_status"
SUBREASON_EXTENSION_MISSING = "extension_missing"
SUBREASON_TIME_AXIS = "time_axis"
SUBREASON_TERM_DOMAIN = "term_domain"
SUBREASON_UNKNOWN = "unknown"

_EXT_KEYS = (
    "queryable_columns",
    "column_value_profiles",
    "time_axis_candidates",
    "join_key_candidates",
)


class PortraitGateError(ValueError):
    """Portrait F-gate rejection with fail_locus + subreason."""

    def __init__(
        self,
        message: str,
        *,
        fail_locus: str = FAIL_LOCUS_PORTRAIT_GATE,
        subreason: str = SUBREASON_UNKNOWN,
        missing_detail: str | None = None,
    ):
        super().__init__(message)
        self.fail_locus = fail_locus
        self.subreason = subreason
        self.missing_detail = missing_detail


class NeedsUserVerdict(dict):
    """QP preflight envelope: status=needs_user + fail_locus + subreason + detail."""

    @classmethod
    def make(
        cls,
        *,
        fail_locus: str,
        subreason: str,
        missing_detail: str,
    ) -> "NeedsUserVerdict":
        return cls(
            {
                "status": "needs_user",
                "fail_locus": fail_locus,
                "subreason": subreason,
                "missing_detail": missing_detail,
            }
        )


def read_structure_status_queryable(
    contract_slice: Mapping[str, Any],
) -> frozenset[str]:
    """
    Read enums.structure_status_queryable from contract slice.
    Must not hardcode success tokens in implementation.
    """
    block = contract_slice.get("cross_table") or contract_slice
    enums = block.get("enums") or {}
    members = enums.get("structure_status_queryable")
    if not isinstance(members, list) or not members:
        raise PortraitGateError(
            "structure_status_queryable enum missing in contract",
            fail_locus=FAIL_LOCUS_PORTRAIT_GATE,
            subreason=SUBREASON_STRUCTURE_STATUS,
        )
    return frozenset(str(x) for x in members)


def read_time_axis_required_fields(
    contract_slice: Mapping[str, Any],
) -> tuple[str, ...]:
    block = contract_slice.get("cross_table") or contract_slice
    fields = block.get("time_axis_candidate_required_fields")
    if not isinstance(fields, list) or not fields:
        raise PortraitGateError(
            "time_axis_candidate_required_fields missing in contract",
            fail_locus=FAIL_LOCUS_PORTRAIT_GATE,
            subreason=SUBREASON_TIME_AXIS,
        )
    return tuple(str(x) for x in fields)


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def extract_portrait_query_extensions(portrait: dict[str, Any]) -> dict[str, Any]:
    """Read portrait_query_extensions; missing block → empty lists."""
    ext = portrait.get("portrait_query_extensions")
    if not isinstance(ext, dict):
        return {k: [] for k in _EXT_KEYS}
    return {k: _as_list(ext.get(k)) for k in _EXT_KEYS}


def gate_f1_structure_status(
    portrait: dict[str, Any],
    *,
    queryable_statuses: Collection[str],
) -> None:
    """F gate ① structure half: status must be in contract queryable set."""
    if not queryable_statuses:
        raise PortraitGateError(
            "queryable_statuses empty (contract required)",
            fail_locus=FAIL_LOCUS_PORTRAIT_GATE,
            subreason=SUBREASON_STRUCTURE_STATUS,
        )
    status = portrait.get("structure_status")
    if status is None:
        raise PortraitGateError(
            "structure_status missing",
            fail_locus=FAIL_LOCUS_PORTRAIT_GATE,
            subreason=SUBREASON_STRUCTURE_STATUS,
            missing_detail="structure_status",
        )
    token = str(status)
    if token not in set(str(x) for x in queryable_statuses):
        raise PortraitGateError(
            "structure_status not queryable: %s" % status,
            fail_locus=FAIL_LOCUS_PORTRAIT_GATE,
            subreason=SUBREASON_STRUCTURE_STATUS,
            missing_detail="structure_status=%s" % status,
        )


def gate_f1_required_extensions(
    portrait: dict[str, Any],
    *,
    require_queryable: bool = True,
) -> dict[str, Any]:
    """F gate ① extension half: missing required ext → portrait_gate + subreason."""
    raw = portrait.get("portrait_query_extensions")
    if not isinstance(raw, dict):
        raise PortraitGateError(
            "portrait_query_extensions missing",
            fail_locus=FAIL_LOCUS_PORTRAIT_GATE,
            subreason=SUBREASON_EXTENSION_MISSING,
            missing_detail="portrait_query_extensions",
        )
    for key in _EXT_KEYS:
        if key not in raw:
            raise PortraitGateError(
                "%s key missing" % key,
                fail_locus=FAIL_LOCUS_PORTRAIT_GATE,
                subreason=SUBREASON_EXTENSION_MISSING,
                missing_detail=key,
            )
    ext = extract_portrait_query_extensions(portrait)
    if require_queryable and not ext["queryable_columns"]:
        raise PortraitGateError(
            "queryable_columns empty",
            fail_locus=FAIL_LOCUS_PORTRAIT_GATE,
            subreason=SUBREASON_UNKNOWN,
            missing_detail="queryable_columns",
        )
    return ext


def gate_portrait_f1(
    portrait: dict[str, Any],
    *,
    queryable_statuses: Collection[str],
    require_queryable: bool = True,
) -> dict[str, Any]:
    """Default path ①: structure + required extensions."""
    gate_f1_structure_status(portrait, queryable_statuses=queryable_statuses)
    return gate_f1_required_extensions(portrait, require_queryable=require_queryable)


def _confirmed_column_ids(confirmed_bindings: Iterable[Mapping[str, Any]]) -> set[str]:
    out: set[str] = set()
    for item in confirmed_bindings:
        if not isinstance(item, Mapping):
            continue
        payload = item.get("binding_payload")
        if not isinstance(payload, Mapping):
            continue
        for cid in _as_list(payload.get("confirmed_column_ids")):
            out.add(str(cid))
    return out


def gate_portrait_f2_authorized(
    portrait: dict[str, Any],
    *,
    authorized: bool,
    confirmed_bindings: list[dict[str, Any]] | None = None,
    referenced_critical_column_ids: list[str] | None = None,
) -> dict[str, Any]:
    """
    Path ②: explicit authorization + ConfirmedSemanticBinding required.
    Any referenced critical column not covered by a confirmed binding → reject before QE.
    """
    if not authorized:
        raise PortraitGateError(
            "path2 requires explicit authorization",
            fail_locus=FAIL_LOCUS_PORTRAIT_GATE,
            subreason=SUBREASON_UNKNOWN,
            missing_detail="authorization",
        )
    bindings = confirmed_bindings or []
    if not bindings:
        raise PortraitGateError(
            "path2 requires at least one ConfirmedSemanticBinding",
            fail_locus=FAIL_LOCUS_PORTRAIT_GATE,
            subreason=SUBREASON_UNKNOWN,
            missing_detail="ConfirmedSemanticBinding",
        )
    for item in bindings:
        if not isinstance(item, dict):
            raise PortraitGateError(
                "binding must be object",
                fail_locus=FAIL_LOCUS_PORTRAIT_GATE,
                subreason=SUBREASON_UNKNOWN,
            )
        if not item.get("binding_id") or not item.get("binding_payload"):
            raise PortraitGateError(
                "binding missing binding_id or binding_payload",
                fail_locus=FAIL_LOCUS_PORTRAIT_GATE,
                subreason=SUBREASON_UNKNOWN,
            )
    if referenced_critical_column_ids:
        confirmed = _confirmed_column_ids(bindings)
        for cid in referenced_critical_column_ids:
            if str(cid) not in confirmed:
                raise PortraitGateError(
                    "critical column not in ConfirmedSemanticBinding: %s" % cid,
                    fail_locus=FAIL_LOCUS_PORTRAIT_GATE,
                    subreason=SUBREASON_UNKNOWN,
                    missing_detail="unconfirmed_critical_column=%s" % cid,
                )
    return extract_portrait_query_extensions(portrait)


def gate_before_qe_critical_columns(
    *,
    referenced_critical_column_ids: list[str],
    confirmed_bindings: list[dict[str, Any]],
) -> None:
    """Minimal pre-QE check: unconfirmed critical column → portrait_gate reject."""
    confirmed = _confirmed_column_ids(confirmed_bindings)
    for cid in referenced_critical_column_ids:
        if str(cid) not in confirmed:
            raise PortraitGateError(
                "critical column not confirmed before QE: %s" % cid,
                fail_locus=FAIL_LOCUS_PORTRAIT_GATE,
                subreason=SUBREASON_UNKNOWN,
                missing_detail="unconfirmed_critical_column=%s" % cid,
            )


def read_domain_cap_needs_user_statuses(
    contract_slice: Mapping[str, Any],
) -> frozenset[str]:
    block = contract_slice.get("cross_table") or contract_slice
    enums = block.get("enums") or {}
    members = enums.get("domain_cap_needs_user_statuses")
    if not isinstance(members, list) or not members:
        raise PortraitGateError(
            "domain_cap_needs_user_statuses missing in contract",
            fail_locus=FAIL_LOCUS_PORTRAIT_GATE,
            subreason=SUBREASON_TERM_DOMAIN,
        )
    return frozenset(str(x) for x in members)


def qp_preflight_domain_cap(
    portrait: dict[str, Any],
    *,
    needs_user_statuses: Collection[str],
) -> NeedsUserVerdict | None:
    """
    If any column_value_profiles.domain_cap_status ∈ contract needs_user set → needs_user.
    Trigger set from domain_cap_needs_user_statuses (not hardcoded).
    Fail locus = QueryFailLocus.term_expand (设计卡【补 B】术语展开值域).
    """
    triggers = set(str(x) for x in needs_user_statuses)
    ext = extract_portrait_query_extensions(portrait)
    for profile in ext["column_value_profiles"]:
        if not isinstance(profile, dict):
            continue
        status = str(profile.get("domain_cap_status"))
        if status in triggers:
            cid = profile.get("column_id")
            return NeedsUserVerdict.make(
                fail_locus=FAIL_LOCUS_TERM_EXPAND,
                subreason=SUBREASON_TERM_DOMAIN,
                missing_detail="domain_cap_status=%s column_id=%s" % (status, cid),
            )
    return None


def qp_preflight_time_axis(
    portrait: dict[str, Any],
    *,
    required_fields: Collection[str],
) -> NeedsUserVerdict | None:
    """
    If any time_axis_candidates entry misses a required field → needs_user.
    required_fields come from contract (not hardcoded).
    """
    ext = extract_portrait_query_extensions(portrait)
    candidates = ext["time_axis_candidates"]
    if not candidates:
        return NeedsUserVerdict.make(
            fail_locus=FAIL_LOCUS_PORTRAIT_GATE,
            subreason=SUBREASON_TIME_AXIS,
            missing_detail="time_axis_candidates empty",
        )
    for i, cand in enumerate(candidates):
        if not isinstance(cand, dict):
            return NeedsUserVerdict.make(
                fail_locus=FAIL_LOCUS_PORTRAIT_GATE,
                subreason=SUBREASON_TIME_AXIS,
                missing_detail="time_axis_candidates[%s] not object" % i,
            )
        missing = [f for f in required_fields if f not in cand or cand.get(f) in (None, "")]
        if missing:
            return NeedsUserVerdict.make(
                fail_locus=FAIL_LOCUS_PORTRAIT_GATE,
                subreason=SUBREASON_TIME_AXIS,
                missing_detail="time_axis_candidates[%s] missing=%s" % (i, ",".join(missing)),
            )
    return None


# Aliases
gate_f2_required_extensions = gate_f1_required_extensions
gate_portrait_f = gate_portrait_f1
