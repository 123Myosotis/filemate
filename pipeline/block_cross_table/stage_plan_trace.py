# -*- coding: utf-8 -*-
"""B 段轨迹：StagePlanTraceMeta → 既有 write_trace。"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.join_hypothesis import write_trace

KIND_GENERATE = "stage_plan_generate"
KIND_REVIEW = "stage_plan_review"
_ALLOWED_KINDS = frozenset({KIND_GENERATE, KIND_REVIEW})


def require_run_id(run_id: Any) -> str:
    if run_id is None or (isinstance(run_id, str) and run_id.strip() == ""):
        raise CrossTableError(
            "cross_table_audit_structure_invalid",
            "run_id missing",
        )
    return str(run_id)


class StagePlanTracer:
    def __init__(
        self,
        run_id: Any,
        clock: Callable[[], str],
        *,
        plan_generation: int = 1,
    ) -> None:
        self.run_id = require_run_id(run_id)
        self.clock = clock
        if not isinstance(plan_generation, int) or isinstance(plan_generation, bool) or plan_generation < 1:
            raise CrossTableError(
                "cross_table_audit_structure_invalid",
                "plan_generation invalid",
            )
        self.plan_generation = int(plan_generation)
        self._kind_seq: dict[str, int] = {}
        self.paths: list[str] = []

    def write(self, call_kind: str, body: Optional[Mapping[str, Any]] = None) -> str:
        if call_kind not in _ALLOWED_KINDS:
            raise CrossTableError(
                "cross_table_audit_structure_invalid",
                "call_kind not in stage plan kinds",
                {"call_kind": call_kind},
            )
        seq = self._kind_seq.get(call_kind, 0) + 1
        self._kind_seq[call_kind] = seq
        meta = {
            "run_id": self.run_id,
            "plan_generation": self.plan_generation,
            "call_kind": call_kind,
            "kind_seq": seq,
        }
        payload: dict[str, Any] = {"meta": meta}
        if body:
            payload.update(dict(body))
        path = write_trace(str(call_kind), payload, self.clock)
        self.paths.append(path)
        return path
