# -*- coding: utf-8 -*-
"""S 段轨迹：StageExecTraceMeta → 既有 write_trace。"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Sequence

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.join_hypothesis import write_trace
from pipeline.block_cross_table.stage_plan_trace import require_run_id

KIND_STAGE_EXEC = "stage_exec_model"
_ALLOWED_KINDS = frozenset({KIND_STAGE_EXEC})


class StageExecTracer:
    def __init__(
        self,
        run_id: Any,
        stage_id: str,
        clock: Callable[[], str],
    ) -> None:
        self.run_id = require_run_id(run_id)
        if not isinstance(stage_id, str) or stage_id == "":
            raise CrossTableError(
                "cross_table_audit_structure_invalid",
                "stage_id missing",
            )
        self.stage_id = stage_id
        self.clock = clock
        self._kind_seq: dict[str, int] = {}
        self.paths: list[str] = []

    def write(
        self,
        call_kind: str,
        body: Optional[Mapping[str, Any]] = None,
        *,
        input_block_ids: Optional[Sequence[Any]] = None,
    ) -> str:
        if call_kind not in _ALLOWED_KINDS:
            raise CrossTableError(
                "cross_table_audit_structure_invalid",
                "call_kind not in stage exec kinds",
                {"call_kind": call_kind},
            )
        seq = self._kind_seq.get(call_kind, 0) + 1
        self._kind_seq[call_kind] = seq
        meta: dict[str, Any] = {
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "call_kind": call_kind,
            "kind_seq": seq,
        }
        if input_block_ids is not None:
            meta["input_block_ids"] = [str(x) for x in input_block_ids]
        payload: dict[str, Any] = {"meta": meta}
        if body:
            payload.update(dict(body))
        path = write_trace(str(call_kind), payload, self.clock)
        self.paths.append(path)
        return path
