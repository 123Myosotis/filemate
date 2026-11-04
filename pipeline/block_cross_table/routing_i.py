# -*- coding: utf-8 -*-
"""I 节编排：图 1/3/4 识别段块循环；不改 A'/A/trace 实现，只调用。

生产口径：run_id 必传（缺 → cross_table_audit_structure_invalid）。
复核不通过与冲突分函数处理，不得共用。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.layer_recognize import run_layer_recognize_block
from pipeline.block_cross_table.layer_recognize_limits import (
    read_layer_recognize_entry_retry_max,
)
from pipeline.block_cross_table.orchestrate_decision_trace import OrchestrateDecisionTracer
from pipeline.block_cross_table.portrait_store_fs import FilePortraitStore
from pipeline.block_cross_table.portrait_write import (
    FAIL_FIELD_MISSING,
    FAIL_MAPPING_UNDEFINED,
    FAIL_WRITE_REJECTED,
    PortraitWriteRetryGate,
    write_block_portrait,
)

PHASE_READY_FOR_B = "ready_for_b"
PHASE_STOPPED_AT_E = "stopped_at_e"
PHASE_AWAITING_USER = "awaiting_user_confirm_portrait"

RESUME_CONTINUE = "continue"
RESUME_ROLLBACK_A_PRIME = "rollback_to_a_prime"
RESUME_ROLLBACK_LAYOUT = "rollback_to_layout"
RESUME_RERUN_OWNERSHIP = "rerun_cross_block_ownership"  # 图 4 之 ④


def require_run_id(run_id: Any) -> str:
    """后续-15 生产口径：I 侧强制；缺则 audit，不许跳过。"""
    if run_id is None or (isinstance(run_id, str) and run_id.strip() == ""):
        raise CrossTableError(
            "cross_table_audit_structure_invalid",
            "run_id missing",
        )
    return str(run_id)


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


class RoutingI:
    """跨表识别段编排者（图 1 I）。"""

    def __init__(
        self,
        *,
        run_id: Any,
        blocks: Sequence[Mapping[str, Any]],
        drafts_by_block: Mapping[Any, Any],
        tables_by_id: Mapping[str, Any],
        user_utterance: str,
        review_input_capacity: int,
        portrait_store: FilePortraitStore,
        chat: Callable[[str], Any],
        clock: Optional[Callable[[], str]] = None,
        dimensions_by_block: Optional[Mapping[Any, Any]] = None,
        contract_data: Optional[Mapping[str, Any]] = None,
        portrait_user_confirm: Optional[Callable[[Mapping[str, Any]], bool]] = None,
    ) -> None:
        self.run_id = require_run_id(run_id)
        self.blocks = [dict(b) for b in blocks]
        self.drafts_by_block = dict(drafts_by_block or {})
        self.tables_by_id = dict(tables_by_id or {})
        self.user_utterance = user_utterance
        self.review_input_capacity = review_input_capacity
        self.store = portrait_store
        self.chat = chat
        self.clock = clock or _default_clock
        self.dimensions_by_block = dict(dimensions_by_block or {})
        self.contract_data = contract_data
        self.portrait_user_confirm = portrait_user_confirm
        self.entry_retry_max = read_layer_recognize_entry_retry_max(contract_data)
        # block_id str → 已因 review_failed 再调次数
        self._entry_retries: dict[str, int] = {}
        self._entry_seq: dict[str, int] = {}
        self.tracer = OrchestrateDecisionTracer(
            self.run_id, self.clock, contract_data=contract_data
        )
        self.stopped_block_id: Any = None
        self.e_payload: Optional[dict[str, Any]] = None
        self.completed_block_ids: list[Any] = []
        self.phase: str = "idle"
        self._cursor: int = 0

    def _bid(self, block: Mapping[str, Any]) -> Any:
        return block.get("block_id")

    def _bid_key(self, block_id: Any) -> str:
        return str(block_id)

    def _draft_for(self, block: Mapping[str, Any]) -> Any:
        bid = self._bid(block)
        if bid in self.drafts_by_block:
            return self.drafts_by_block[bid]
        if str(bid) in self.drafts_by_block:
            return self.drafts_by_block[str(bid)]
        return None

    def _dims_for(self, block: Mapping[str, Any]) -> Any:
        bid = self._bid(block)
        if bid in self.dimensions_by_block:
            return self.dimensions_by_block[bid]
        return self.dimensions_by_block.get(str(bid)) or {}

    def _next_entry_seq(self, block_id: Any) -> int:
        key = self._bid_key(block_id)
        cur = self._entry_seq.get(key, 0) + 1
        self._entry_seq[key] = cur
        return cur

    def _call_a_prime(self, block: Mapping[str, Any], entry_seq: int) -> dict[str, Any]:
        return run_layer_recognize_block(
            dict(block),
            self._draft_for(block),
            self.tables_by_id,
            self._dims_for(block),
            self.user_utterance,
            self.review_input_capacity,
            chat=self.chat,
            run_id=self.run_id,
            entry_seq=entry_seq,
            clock=self.clock,
        )

    def _write_a(
        self,
        status: str,
        block_id: Any,
        layer_result: Any = None,
    ) -> dict[str, Any]:
        package: dict[str, Any] = {"status": status, "block_id": block_id}
        if status == "ok":
            package["layer_result"] = layer_result
        result = write_block_portrait(
            package, self.store, contract_data=self.contract_data
        )
        if result.get("ok"):
            return result
        code = result.get("failure_code")
        if code in (FAIL_FIELD_MISSING, FAIL_MAPPING_UNDEFINED):
            return self._hand_off_e_from_portrait_fail(block_id, result, code)
        if code == FAIL_WRITE_REJECTED:
            return self._retry_portrait_write(package, block_id, result)
        raise CrossTableError(
            "cross_table_contract_invalid",
            "unknown portrait failure_code",
            {"failure_code": code},
        )

    def _hand_off_e_from_portrait_fail(
        self,
        block_id: Any,
        write_result: Mapping[str, Any],
        code: str,
    ) -> dict[str, Any]:
        self.tracer.write(
            "hand_off_e",
            "orchestrator",
            {"mechanical": True},
            block_id=block_id,
            trigger_status="portrait_write_failed",
        )
        self.tracer.write(
            "stop_workbook",
            "orchestrator",
            {"mechanical": True},
            block_id=block_id,
            trigger_status=code,
        )
        self.stopped_block_id = block_id
        self.e_payload = {
            "kind": "portrait_write_failed",
            "failure_code": code,
            "rewrite_forbidden": True,
            "write_result": dict(write_result),
            "block_id": block_id,
        }
        self.phase = PHASE_STOPPED_AT_E
        return {"ok": False, "handed_off_e": True, "e_payload": self.e_payload}

    def _retry_portrait_write(
        self,
        package: Mapping[str, Any],
        block_id: Any,
        first_fail: Mapping[str, Any],
    ) -> dict[str, Any]:
        gate = PortraitWriteRetryGate(self.contract_data)
        self.tracer.write(
            "retry_portrait_write",
            "orchestrator",
            {"mechanical": True},
            block_id=block_id,
            trigger_status=FAIL_WRITE_REJECTED,
        )
        prior = FAIL_WRITE_REJECTED
        last = dict(first_fail)
        while gate.can_accept_user_confirm(prior):
            confirmed = True
            if self.portrait_user_confirm is not None:
                confirmed = bool(self.portrait_user_confirm(last))
            if not confirmed:
                break
            last = gate.user_confirm_rewrite(
                package, self.store, prior_failure_code=prior
            )
            if last.get("ok"):
                return last
            prior = str(last.get("failure_code") or FAIL_WRITE_REJECTED)
            if prior != FAIL_WRITE_REJECTED:
                return self._hand_off_e_from_portrait_fail(block_id, last, prior)
            if last.get("retry_exhausted"):
                break
        self.tracer.write(
            "hand_off_e",
            "orchestrator",
            {"mechanical": True},
            block_id=block_id,
            trigger_status=FAIL_WRITE_REJECTED,
        )
        self.tracer.write(
            "stop_workbook",
            "orchestrator",
            {"mechanical": True},
            block_id=block_id,
            trigger_status="portrait_write_retry_exhausted",
        )
        self.stopped_block_id = block_id
        self.e_payload = {
            "kind": "portrait_write_failed",
            "failure_code": FAIL_WRITE_REJECTED,
            "write_result": last,
            "block_id": block_id,
            "retry_exhausted": True,
        }
        self.phase = PHASE_STOPPED_AT_E
        return {"ok": False, "handed_off_e": True, "e_payload": self.e_payload}

    def _handle_ok(self, block: Mapping[str, Any], a_prime: Mapping[str, Any]) -> str:
        """成功 → A 写。返回 continue|stopped。"""
        block_id = self._bid(block)
        write_out = self._write_a("ok", block_id, a_prime.get("layer_result"))
        if write_out.get("handed_off_e"):
            return "stopped"
        self.tracer.write(
            "continue_next_block",
            "orchestrator",
            {"mechanical": True},
            block_id=block_id,
            trigger_status="ok",
        )
        self.completed_block_ids.append(block_id)
        return "continue"

    def _handle_no_structure(
        self, block: Mapping[str, Any], a_prime: Mapping[str, Any]
    ) -> str:
        block_id = self._bid(block)
        write_out = self._write_a("no_structure", block_id)
        if write_out.get("handed_off_e"):
            return "stopped"
        self.tracer.write(
            "continue_next_block",
            "orchestrator",
            {"mechanical": True},
            block_id=block_id,
            trigger_status="no_structure",
        )
        self.completed_block_ids.append(block_id)
        return "continue"

    def _handle_review_failed(
        self, block: Mapping[str, Any], a_prime: Mapping[str, Any]
    ) -> str:
        """复核不通过专用路径：入口重试或转 E。不得与冲突共用。"""
        block_id = self._bid(block)
        key = self._bid_key(block_id)
        used = self._entry_retries.get(key, 0)
        if used >= self.entry_retry_max:
            payload = dict(a_prime)
            payload["entry_retry_exhausted"] = True
            payload["entry_retries_used"] = used
            self.tracer.write(
                "hand_off_e",
                "orchestrator",
                {"mechanical": True},
                block_id=block_id,
                entry_seq=self._entry_seq.get(key),
                trigger_status="review_failed",
            )
            self.tracer.write(
                "stop_workbook",
                "orchestrator",
                {"mechanical": True},
                block_id=block_id,
                trigger_status="entry_retry_exhausted",
            )
            self.stopped_block_id = block_id
            self.e_payload = {
                "kind": "review_failed_exhausted",
                "a_prime": payload,
                "block_id": block_id,
                "appended_reason": "entry_retry_exhausted",
            }
            self.phase = PHASE_STOPPED_AT_E
            return "stopped"

        self._entry_retries[key] = used + 1
        self.tracer.write(
            "retry_a_prime",
            "orchestrator",
            {"mechanical": True},
            block_id=block_id,
            entry_seq=self._entry_seq.get(key),
            trigger_status="review_failed",
        )
        return "retry"

    def _handle_conflict(
        self, block: Mapping[str, Any], a_prime: Mapping[str, Any]
    ) -> str:
        """冲突专用路径：直转 E；不追加入口重试用尽。"""
        block_id = self._bid(block)
        key = self._bid_key(block_id)
        self.tracer.write(
            "hand_off_e",
            "orchestrator",
            {"mechanical": True},
            block_id=block_id,
            entry_seq=self._entry_seq.get(key),
            trigger_status="conflict",
        )
        self.tracer.write(
            "stop_workbook",
            "orchestrator",
            {"mechanical": True},
            block_id=block_id,
            trigger_status="conflict",
        )
        self.stopped_block_id = block_id
        self.e_payload = {
            "kind": "conflict",
            "a_prime": dict(a_prime),
            "block_id": block_id,
            # 钉死：不附 entry_retry_exhausted
            "entry_retry_exhausted": False,
        }
        self.phase = PHASE_STOPPED_AT_E
        return "stopped"

    def _process_block(self, block: Mapping[str, Any]) -> str:
        """处理单块直至成功写入、需重试、或整表停。返回 continue|stopped。"""
        while True:
            entry_seq = self._next_entry_seq(self._bid(block))
            a_prime = self._call_a_prime(block, entry_seq)
            status = a_prime.get("status")
            if status == "ok":
                return self._handle_ok(block, a_prime)
            if status == "no_structure":
                return self._handle_no_structure(block, a_prime)
            if status == "review_failed":
                action = self._handle_review_failed(block, a_prime)
                if action == "retry":
                    continue
                return "stopped"
            if status == "conflict":
                return self._handle_conflict(block, a_prime)
            raise CrossTableError(
                "cross_table_input_invalid",
                "unknown a_prime status",
                {"status": status},
            )

    def run_block_loop(self, *, start_index: int = 0) -> dict[str, Any]:
        """图 3：顺序块循环。"""
        self.phase = "running"
        self._cursor = start_index
        while self._cursor < len(self.blocks):
            block = self.blocks[self._cursor]
            outcome = self._process_block(block)
            if outcome == "stopped":
                return self.snapshot()
            self._cursor += 1
        self.tracer.write(
            "enter_b",
            "orchestrator",
            {"mechanical": True},
            trigger_status="all_blocks_ready",
        )
        self.phase = PHASE_READY_FOR_B
        return self.snapshot()

    def run(self) -> dict[str, Any]:
        self.completed_block_ids = []
        self.stopped_block_id = None
        self.e_payload = None
        return self.run_block_loop(start_index=0)

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "run_id": self.run_id,
            "stopped_block_id": self.stopped_block_id,
            "completed_block_ids": list(self.completed_block_ids),
            "e_payload": self.e_payload,
            "cursor": self._cursor,
            "entry_retries": dict(self._entry_retries),
            "portrait_written": self.store.written_block_ids(),
            "decision_trace_paths": list(self.tracer.paths),
        }

    def resume(self, action: str, *, new_blocks: Optional[Sequence[Mapping[str, Any]]] = None) -> dict[str, Any]:
        """整表停后恢复（图 3 恢复表 + 图 4 ④）。"""
        if action == RESUME_CONTINUE:
            return self._resume_continue()
        if action == RESUME_ROLLBACK_A_PRIME:
            return self._resume_rollback_a_prime()
        if action == RESUME_ROLLBACK_LAYOUT:
            if new_blocks is None:
                raise CrossTableError(
                    "cross_table_input_invalid",
                    "rollback_to_layout requires new_blocks",
                )
            return self._resume_rollback_layout(new_blocks)
        if action == RESUME_RERUN_OWNERSHIP:
            return self._resume_rerun_ownership()
        raise CrossTableError(
            "cross_table_input_invalid",
            "unknown resume action",
            {"action": action},
        )

    def _resume_continue(self) -> dict[str, Any]:
        """从停住块恢复；前序画像保留、不重跑。"""
        if self.phase != PHASE_STOPPED_AT_E:
            raise CrossTableError(
                "cross_table_input_invalid",
                "continue only from stopped_at_e",
            )
        self.tracer.write(
            "retry_a_prime",
            "user",
            {"rationale_text": "user confirmed continue from stopped block"},
            block_id=self.stopped_block_id,
            trigger_status="resume_continue",
        )
        # 停块上的入口重试计数：用户继续时允许再开一轮（同块预算未必然用尽）
        # 若因耗尽停：用户继续仍从该块重试，计数保留（已达上限则下一失败仍进 E）
        self.e_payload = None
        self.phase = "running"
        start = self._cursor
        # 确保停块不在 completed 里
        bid = self.stopped_block_id
        self.completed_block_ids = [
            x for x in self.completed_block_ids if str(x) != str(bid)
        ]
        self.stopped_block_id = None
        return self.run_block_loop(start_index=start)

    def _reset_all_entry_counters(self) -> None:
        self._entry_retries.clear()
        self._entry_seq.clear()

    def _resume_rollback_a_prime(self) -> dict[str, Any]:
        """回滚到 A'：丢弃各块画像；全块重跑；计数归零。"""
        self.tracer.write(
            "rerun_all_a_prime",
            "user",
            {"rationale_text": "user rollback to a_prime recognition"},
            trigger_status="rollback_to_a_prime",
        )
        self.store.discard_all_slices()
        self._reset_all_entry_counters()
        self.completed_block_ids = []
        self.stopped_block_id = None
        self.e_payload = None
        self._cursor = 0
        return self.run_block_loop(start_index=0)

    def _resume_rollback_layout(
        self, new_blocks: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """回滚到上游布局：新块列表全量重跑；计数归零；画像丢弃。"""
        self.tracer.write(
            "rerun_all_a_prime",
            "user",
            {"rationale_text": "user rollback to upstream layout"},
            trigger_status="rollback_to_layout",
        )
        self.store.discard_all_slices()
        self._reset_all_entry_counters()
        self.blocks = [dict(b) for b in new_blocks]
        self.completed_block_ids = []
        self.stopped_block_id = None
        self.e_payload = None
        self._cursor = 0
        return self.run_block_loop(start_index=0)

    def _resume_rerun_ownership(self) -> dict[str, Any]:
        """图 4 之④：跨块归属不一致 → 全表重跑 A'→A；计数归零。"""
        self.tracer.write(
            "rerun_all_a_prime",
            "orchestrator",
            {"mechanical": True},
            trigger_status="cross_block_ownership",
        )
        self.store.discard_all_slices()
        self._reset_all_entry_counters()
        self.completed_block_ids = []
        self.stopped_block_id = None
        self.e_payload = None
        self._cursor = 0
        return self.run_block_loop(start_index=0)
