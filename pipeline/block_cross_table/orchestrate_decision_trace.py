# -*- coding: utf-8 -*-
"""I 编排决策 trace：走既有 write_trace；种类 orchestrate_decision。"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.join_hypothesis import write_trace

CALL_KIND = "orchestrate_decision"


def _cross_table(contract_data: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
    if contract_data is not None:
        block = contract_data.get("cross_table")
    else:
        from pipeline.synth_contract.registry import load_contract

        block = (load_contract().data or {}).get("cross_table")
    if not isinstance(block, Mapping):
        raise CrossTableError(
            "cross_table_contract_invalid",
            "cross_table block missing",
        )
    return block


def read_decision_kinds(contract_data: Optional[Mapping[str, Any]] = None) -> list[str]:
    enums = (_cross_table(contract_data).get("enums") or {})
    kinds = enums.get("orchestrate_decision_kind")
    if not isinstance(kinds, list) or not kinds:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "orchestrate_decision_kind missing",
        )
    return [str(x) for x in kinds]


def read_trigger_sources(contract_data: Optional[Mapping[str, Any]] = None) -> list[str]:
    enums = (_cross_table(contract_data).get("enums") or {})
    sources = enums.get("orchestrate_trigger_source")
    if not isinstance(sources, list) or not sources:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "orchestrate_trigger_source missing",
        )
    return [str(x) for x in sources]


def validate_decision_params(params: Any) -> dict[str, Any]:
    """机械 vs 有理由互斥；违反 → audit。"""
    if not isinstance(params, dict):
        raise CrossTableError(
            "cross_table_audit_structure_invalid",
            "decision_params not object",
        )
    mechanical = params.get("mechanical") is True
    rationale = params.get("rationale_text")
    has_rationale = isinstance(rationale, str) and rationale.strip() != ""
    if mechanical and has_rationale:
        raise CrossTableError(
            "cross_table_audit_structure_invalid",
            "mechanical decision with non-empty rationale_text",
        )
    if mechanical and not has_rationale:
        return {"mechanical": True}
    if has_rationale and not mechanical:
        return {"rationale_text": str(rationale)}
    if has_rationale and mechanical:
        raise CrossTableError(
            "cross_table_audit_structure_invalid",
            "rationale_text and mechanical both set",
        )
    raise CrossTableError(
        "cross_table_audit_structure_invalid",
        "reasoned decision requires rationale_text",
    )


class OrchestrateDecisionTracer:
    def __init__(
        self,
        run_id: str,
        clock: Callable[[], str],
        contract_data: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if run_id is None or str(run_id).strip() == "":
            raise CrossTableError(
                "cross_table_audit_structure_invalid",
                "run_id missing",
            )
        self.run_id = str(run_id)
        self.clock = clock
        self.contract_data = contract_data
        self._seq = 0
        self.paths: list[str] = []

    def write(
        self,
        decision_kind: str,
        trigger_source: str,
        decision_params: Mapping[str, Any],
        *,
        block_id: Any = None,
        entry_seq: Optional[int] = None,
        trigger_status: Optional[str] = None,
    ) -> str:
        kinds = read_decision_kinds(self.contract_data)
        if decision_kind not in kinds:
            raise CrossTableError(
                "cross_table_audit_structure_invalid",
                "decision_kind not in enum",
                {"decision_kind": decision_kind},
            )
        sources = read_trigger_sources(self.contract_data)
        if trigger_source not in sources:
            raise CrossTableError(
                "cross_table_audit_structure_invalid",
                "trigger_source missing or invalid",
                {"trigger_source": trigger_source},
            )
        params = validate_decision_params(dict(decision_params))
        self._seq += 1
        meta = {
            "run_id": self.run_id,
            "orchestrate_seq": self._seq,
            "decision_kind": decision_kind,
            "trigger_source": trigger_source,
            "decision_params": params,
            "block_id": None if block_id is None else str(block_id),
            "entry_seq": entry_seq,
            "trigger_status": trigger_status,
        }
        for key in (
            "run_id",
            "orchestrate_seq",
            "decision_kind",
            "trigger_source",
            "decision_params",
        ):
            if key not in meta or meta[key] is None:
                raise CrossTableError(
                    "cross_table_audit_structure_invalid",
                    "orchestrate decision meta incomplete",
                    {"field": key},
                )
        path = write_trace(CALL_KIND, {"meta": meta}, self.clock)
        self.paths.append(path)
        return path
