# -*- coding: utf-8 -*-
"""E 段轨迹：ConflictSessionTraceMeta → 既有 write_trace。"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.join_hypothesis import write_trace
from pipeline.block_cross_table.stage_plan_trace import require_run_id

KIND_FOUR_PART = "conflict_four_part_ask"
KIND_USER_REPLY = "conflict_user_reply"
_ALLOWED = frozenset({KIND_FOUR_PART, KIND_USER_REPLY})


class ConflictSessionTracer:
    def __init__(
        self,
        run_id: Any,
        conflict_session_seq: int,
        clock: Callable[[], str],
        *,
        block_ref: Optional[str] = None,
        stage_ref: Optional[str] = None,
    ) -> None:
        self.run_id = require_run_id(run_id)
        if (
            isinstance(conflict_session_seq, bool)
            or not isinstance(conflict_session_seq, int)
            or conflict_session_seq < 1
        ):
            raise CrossTableError(
                "cross_table_audit_structure_invalid",
                "conflict_session_seq invalid",
            )
        self.conflict_session_seq = int(conflict_session_seq)
        self.clock = clock
        self.block_ref = block_ref
        self.stage_ref = stage_ref
        self._kind_seq: dict[str, int] = {}
        self.paths: list[str] = []

    def write(self, call_kind: str, body: Optional[Mapping[str, Any]] = None) -> str:
        if call_kind not in _ALLOWED:
            raise CrossTableError(
                "cross_table_audit_structure_invalid",
                "call_kind not in conflict session kinds",
                {"call_kind": call_kind},
            )
        seq = self._kind_seq.get(call_kind, 0) + 1
        self._kind_seq[call_kind] = seq
        meta: dict[str, Any] = {
            "run_id": self.run_id,
            "conflict_session_seq": self.conflict_session_seq,
            "call_kind": call_kind,
            "kind_seq": seq,
        }
        if self.block_ref is not None:
            meta["block_ref"] = self.block_ref
        if self.stage_ref is not None:
            meta["stage_ref"] = self.stage_ref
        payload: dict[str, Any] = {"meta": meta}
        if body:
            payload.update(dict(body))
        path = write_trace(str(call_kind), payload, self.clock)
        self.paths.append(path)
        return path
