# -*- coding: utf-8 -*-
"""E 段：冲突通道真四段。本地不做语义判断；纠正文原样交模型；不执行回滚重做。

收包校验 / 四段组装与追问 / 用户四类回复 / 交接包。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from pipeline.block_cross_table.conflict_e_trace import (
    KIND_FOUR_PART,
    KIND_USER_REPLY,
    ConflictSessionTracer,
)
from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.stage_plan import (
    ROLLBACK_A_PRIME,
    ROLLBACK_LAYOUT,
    ROLLBACK_OWNERSHIP,
    ROLLBACK_STAGE,
)
from pipeline.block_cross_table.stage_plan_trace import require_run_id

ENTRY_KINDS = frozenset(
    {"from_i_recognize", "from_s_stage", "from_b_via_i_ownership"}
)
TRIGGER_SOURCES = frozenset({"I", "S"})

MISSING_FIELD_REQUIRED = (
    "missing_field",
    "review_raw",
    "stage_id",
    "locator_ids",
)
AI_EXHAUSTED_REQUIRED = (
    "invalid_reason",
    "stage_id",
    "locator_ids",
    "attempts",
)
REVIEW_FAILED_ASK_REQUIRED = (
    "stage_id",
    "locator_ids",
    "review_raw",
    "trigger",
)
OWNERSHIP_REQUIRED = (
    "rollback_point",
    "input_block_ids",
    "conflicts",
)
# 识别链（信封 body）
I_REVIEW_FAILED_HINTS = ("rejection_reason", "status", "reason")
I_CONFLICT_HINTS = ("reason_codes", "status")

FIGURE4_POINTS = frozenset(
    {
        ROLLBACK_A_PRIME,
        ROLLBACK_STAGE,
        ROLLBACK_LAYOUT,
        ROLLBACK_OWNERSHIP,
    }
)
EXECUTOR_BY_POINT = {
    ROLLBACK_A_PRIME: "I",
    ROLLBACK_STAGE: "S",
    ROLLBACK_LAYOUT: "I",
    ROLLBACK_OWNERSHIP: "I",
}

DECISION_CONTINUE = "continue"
DECISION_ROLLBACK = "rollback"
DECISION_CORRECT = "correct_and_reask"
DECISION_STOP = "stop_workbook"
_USER_REPLY_KINDS = frozenset(
    {DECISION_CONTINUE, DECISION_ROLLBACK, DECISION_CORRECT, DECISION_STOP}
)

_FOUR_CN = ("推测", "依据", "可能错在哪", "下一步")
_FOUR_EN = {
    "speculation": "推测",
    "evidence": "依据",
    "what_might_be_wrong": "可能错在哪",
    "next_step": "下一步",
}


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def require_conflict_ask_max(conflict_ask_max: Any) -> int:
    if conflict_ask_max is None:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "conflict_ask_max_rounds missing",
        )
    if (
        isinstance(conflict_ask_max, bool)
        or not isinstance(conflict_ask_max, int)
        or conflict_ask_max < 1
    ):
        raise CrossTableError(
            "cross_table_contract_invalid",
            "conflict_ask_max_rounds invalid",
        )
    return int(conflict_ask_max)


def validate_e_package(package: Mapping[str, Any]) -> str:
    """校验 S/I 载荷 body（按 kind）。缺要素 → 契约无效类。"""
    if not isinstance(package, Mapping):
        raise CrossTableError(
            "cross_table_contract_invalid",
            "e package not object",
        )
    kind = package.get("kind")
    if kind == "missing_field":
        required = MISSING_FIELD_REQUIRED
    elif kind == "stage_ai_structure_exhausted":
        required = AI_EXHAUSTED_REQUIRED
    elif kind == "review_failed_ask_user":
        required = REVIEW_FAILED_ASK_REQUIRED
    elif kind == "cross_block_ownership":
        required = OWNERSHIP_REQUIRED
    elif kind == "review_failed":
        # 识别链：至少有否决原因类字段之一
        if not any(k in package for k in ("rejection_reason", "status")):
            raise CrossTableError(
                "cross_table_contract_invalid",
                "review_failed body incomplete",
            )
        return str(kind)
    elif kind == "conflict":
        codes = package.get("reason_codes")
        if not isinstance(codes, list) or not codes:
            raise CrossTableError(
                "cross_table_contract_invalid",
                "conflict reason_codes missing",
            )
        return str(kind)
    else:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "e package kind unknown",
            {"kind": kind},
        )
    missing = [k for k in required if k not in package]
    if missing:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "e package missing elements",
            {"missing": missing, "kind": kind},
        )
    if kind == "cross_block_ownership":
        ids = package.get("input_block_ids")
        if not isinstance(ids, list) or not ids:
            raise CrossTableError(
                "cross_table_contract_invalid",
                "input_block_ids required non-empty for ownership",
            )
        if package.get("rollback_point") != ROLLBACK_OWNERSHIP:
            raise CrossTableError(
                "cross_table_contract_invalid",
                "ownership rollback_point mismatch",
            )
        if package.get("adjudication") not in (None,):
            # must be null / missing treated as null only if key present as null
            if "adjudication" in package and package.get("adjudication") is not None:
                raise CrossTableError(
                    "cross_table_contract_invalid",
                    "adjudication must be null",
                )
    if kind == "stage_ai_structure_exhausted":
        attempts = package.get("attempts")
        if (
            isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or attempts < 1
        ):
            raise CrossTableError(
                "cross_table_contract_invalid",
                "attempts invalid",
            )
    return str(kind)


def validate_e_envelope(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """校验统一信封；返回规范化视图。不重复校验 S 门闸。"""
    if not isinstance(envelope, Mapping):
        raise CrossTableError(
            "cross_table_contract_invalid",
            "e envelope not object",
        )
    run_id = require_run_id(envelope.get("run_id"))
    entry_kind = envelope.get("entry_kind")
    if entry_kind not in ENTRY_KINDS:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "entry_kind invalid",
            {"entry_kind": entry_kind},
        )
    seq = envelope.get("conflict_session_seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "conflict_session_seq invalid",
        )
    trigger = envelope.get("trigger_source")
    if trigger not in TRIGGER_SOURCES:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "trigger_source invalid",
        )
    body = envelope.get("body")
    if not isinstance(body, Mapping):
        # 兼容：载荷平铺在信封上（S 旧路径）
        body = {k: v for k, v in envelope.items() if k not in (
            "run_id",
            "entry_kind",
            "conflict_session_seq",
            "trigger_source",
            "body",
        )}
    kind = body.get("kind") or envelope.get("kind")
    if kind is None:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "payload kind missing",
        )
    body = dict(body)
    body["kind"] = kind
    validate_e_package(body)
    # ④ 无论 B/S：已在 validate_e_package
    return {
        "run_id": run_id,
        "entry_kind": entry_kind,
        "conflict_session_seq": int(seq),
        "trigger_source": trigger,
        "kind": str(kind),
        "body": body,
    }


def _normalize_four_part(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return None
    out: dict[str, Any] = {}
    for cn in _FOUR_CN:
        if cn in raw:
            out[cn] = raw[cn]
    for en, cn in _FOUR_EN.items():
        if cn not in out and en in raw:
            out[cn] = raw[en]
    if len(out) < 4:
        return None
    return out


def _extract_rollback_point(next_step: Any) -> Optional[str]:
    if isinstance(next_step, str) and next_step in FIGURE4_POINTS:
        return next_step
    if isinstance(next_step, Mapping):
        rp = next_step.get("rollback_point") or next_step.get("回滚点")
        if isinstance(rp, str) and rp in FIGURE4_POINTS:
            return rp
    return None


def _four_part_complete(fp: Mapping[str, Any]) -> bool:
    for k in _FOUR_CN:
        if k not in fp or fp[k] in (None, ""):
            return False
    return _extract_rollback_point(fp.get("下一步")) is not None


def _seed_four_part_from_body(kind: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """机械拼装种子（非语义判断）：字段原样搬运，缺段留空供模型补。"""
    speculation = ""
    evidence = ""
    if kind == "missing_field":
        speculation = (
            "复核结论声称通过但缺少必要字段，无法确认是否需问用户"
            if isinstance(body.get("review_raw"), Mapping)
            and body.get("review_raw", {}).get("passed") is True
            else "阶段结构缺字段"
        )
        evidence = json.dumps(
            {
                "missing_field": body.get("missing_field"),
                "review_raw": body.get("review_raw"),
                "stage_id": body.get("stage_id"),
                "locator_ids": body.get("locator_ids"),
            },
            ensure_ascii=False,
            default=str,
        )
    elif kind == "stage_ai_structure_exhausted":
        speculation = "阶段 AI 结构无效重试用尽"
        evidence = json.dumps(
            {
                "invalid_reason": body.get("invalid_reason"),
                "stage_id": body.get("stage_id"),
                "locator_ids": body.get("locator_ids"),
                "attempts": body.get("attempts"),
            },
            ensure_ascii=False,
            default=str,
        )
    elif kind == "review_failed_ask_user":
        speculation = "阶段复核未通过且门闸允许问用户"
        evidence = json.dumps(body.get("review_raw"), ensure_ascii=False, default=str)
    elif kind == "cross_block_ownership":
        speculation = "跨块同维层归属不一致（E 不裁定对错）"
        evidence = json.dumps(
            {
                "input_block_ids": body.get("input_block_ids"),
                "conflicts": body.get("conflicts"),
            },
            ensure_ascii=False,
            default=str,
        )
    elif kind == "review_failed":
        speculation = str(body.get("rejection_reason") or body.get("status") or "")
        evidence = json.dumps(
            {
                "review_locator_ids": body.get("review_locator_ids"),
                "ask_rounds_used": body.get("ask_rounds_used"),
                "entry_retry_exhausted": body.get("entry_retry_exhausted", False),
            },
            ensure_ascii=False,
            default=str,
        )
    elif kind == "conflict":
        speculation = "冲突载荷"
        evidence = json.dumps(
            {"reason_codes": body.get("reason_codes")},
            ensure_ascii=False,
            default=str,
        )
    return {
        "推测": speculation,
        "依据": evidence,
        "可能错在哪": "",
        "下一步": "",
    }


def _build_ask_prompt(seed: Mapping[str, Any], body: Mapping[str, Any], envelope: Mapping[str, Any]) -> str:
    payload = {
        "entry_kind": envelope.get("entry_kind"),
        "kind": body.get("kind"),
        "body": body,
        "seed_four_part": seed,
        "require": (
            "只输出一个 JSON 对象，四个键都必须是非空字符串："
            "推测、依据、可能错在哪、下一步。"
            "其中「下一步」的值必须恰好等于以下四字面量之一（整段字符串、不要加前缀/后缀/竖线列表）："
            "a_prime_recognition / stage_start / upstream_layout / cross_block_ownership。"
            "「可能错在哪」写一句中文风险说明，禁止空串。"
            "不要裁定跨块层归属对错；不要输出 markdown。"
        ),
    }
    return "E conflict four-part ask. JSON only. " + json.dumps(
        payload, ensure_ascii=False, default=str
    )


def _handoff_continue(env: Mapping[str, Any], four_part: Mapping[str, Any]) -> dict[str, Any]:
    trigger = env["trigger_source"]
    resume = "resume_stopped_block" if trigger == "I" else "stage_restart_like_rollback_2"
    return {
        "decision": DECISION_CONTINUE,
        "trigger_source": trigger,
        "conflict_session_seq": env["conflict_session_seq"],
        "resume_hint": resume,
        "four_part": dict(four_part),
        "source": "user",
    }


def _handoff_rollback(
    env: Mapping[str, Any],
    four_part: Mapping[str, Any],
    rollback_point: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    scope: dict[str, Any] = {}
    if body.get("stage_id") is not None:
        scope["stage_id"] = body.get("stage_id")
    if rollback_point == ROLLBACK_OWNERSHIP:
        ids = body.get("input_block_ids")
        if not isinstance(ids, list) or not ids:
            raise CrossTableError(
                "cross_table_contract_invalid",
                "rollback 4 requires input_block_ids in scope",
            )
        scope["input_block_ids"] = list(ids)
    return {
        "decision": DECISION_ROLLBACK,
        "rollback_point": rollback_point,
        "executor_hint": EXECUTOR_BY_POINT[rollback_point],
        "scope": scope,
        "trigger_source": env["trigger_source"],
        "conflict_session_seq": env["conflict_session_seq"],
        "four_part": dict(four_part),
        "source": "user",
    }


def _handoff_stop(env: Mapping[str, Any], *, source: str, four_part: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "decision": DECISION_STOP,
        "trigger_source": env["trigger_source"],
        "conflict_session_seq": env["conflict_session_seq"],
        "source": source,
    }
    if four_part is not None:
        out["four_part"] = dict(four_part)
    return out


def run_conflict_e(
    *,
    envelope: Mapping[str, Any],
    chat: Callable[[str], Any],
    user_callback: Callable[[Mapping[str, Any]], Any],
    conflict_ask_max: Any,
    clock: Optional[Callable[[], str]] = None,
) -> dict[str, Any]:
    """E 主入口。返回交接包 + trace_paths。"""
    env = validate_e_envelope(envelope)
    ask_max = require_conflict_ask_max(conflict_ask_max)
    _clock = clock or _default_clock
    body = env["body"]
    kind = env["kind"]

    stage_ref = body.get("stage_id")
    if stage_ref is not None:
        stage_ref = str(stage_ref)
    block_ref = None
    if isinstance(body.get("input_block_ids"), list) and body["input_block_ids"]:
        block_ref = str(body["input_block_ids"][0])

    tracer = ConflictSessionTracer(
        env["run_id"],
        env["conflict_session_seq"],
        _clock,
        block_ref=block_ref,
        stage_ref=stage_ref,
    )
    seed = _seed_four_part_from_body(kind, body)
    four_part = dict(seed)
    asks_used = 0
    last_raw: Any = None

    while asks_used < ask_max:
        asks_used += 1
        raw = chat(_build_ask_prompt(four_part, body, env))
        last_raw = raw
        tracer.write(KIND_FOUR_PART, {"attempt": asks_used, "raw": raw})
        normalized = _normalize_four_part(raw)
        if normalized is None:
            continue
        # 合并：模型可覆盖空段；不本地改写已有非空推测/依据语义——仅填补
        for k in _FOUR_CN:
            val = normalized.get(k)
            if val not in (None, ""):
                four_part[k] = val
        if _four_part_complete(four_part):
            break
    else:
        # 达上限仍不合法
        return {
            **_handoff_stop(env, source="ask_exhausted", four_part=four_part),
            "trace_paths": list(tracer.paths),
            "asks_used": asks_used,
            "ok": False,
            "status": "ask_exhausted",
        }

    if not _four_part_complete(four_part):
        return {
            **_handoff_stop(env, source="ask_exhausted", four_part=four_part),
            "trace_paths": list(tracer.paths),
            "asks_used": asks_used,
            "ok": False,
            "status": "ask_exhausted",
        }

    # 用户回复循环（纠正可多轮，计入同一 ask 预算剩余）
    remaining = ask_max - asks_used
    while True:
        reply_raw = user_callback(dict(four_part))
        tracer.write(KIND_USER_REPLY, {"reply": reply_raw})
        if not isinstance(reply_raw, Mapping):
            raise CrossTableError(
                "cross_table_input_invalid",
                "user reply not object",
            )
        reply_kind = reply_raw.get("reply_kind") or reply_raw.get("decision")
        if reply_kind not in _USER_REPLY_KINDS:
            raise CrossTableError(
                "cross_table_input_invalid",
                "user reply_kind invalid",
                {"reply_kind": reply_kind},
            )

        if reply_kind == DECISION_CONTINUE:
            out = _handoff_continue(env, four_part)
            out["trace_paths"] = list(tracer.paths)
            out["ok"] = True
            out["status"] = "continue"
            return out

        if reply_kind == DECISION_STOP:
            out = _handoff_stop(env, source="user", four_part=four_part)
            out["trace_paths"] = list(tracer.paths)
            out["ok"] = True
            out["status"] = "stop_workbook"
            return out

        if reply_kind == DECISION_ROLLBACK:
            rp = reply_raw.get("rollback_point") or _extract_rollback_point(
                four_part.get("下一步")
            )
            if rp not in FIGURE4_POINTS:
                raise CrossTableError(
                    "cross_table_proposal_invalid",
                    "rollback_point not named",
                )
            out = _handoff_rollback(env, four_part, str(rp), body)
            out["trace_paths"] = list(tracer.paths)
            out["ok"] = True
            out["status"] = "rollback"
            return out

        # correct_and_reask：原文进模型，E 不解析；模型不完整则原地重试模型（不回头再问用户）
        if remaining < 1:
            out = _handoff_stop(env, source="ask_exhausted", four_part=four_part)
            out["trace_paths"] = list(tracer.paths)
            out["ok"] = False
            out["status"] = "ask_exhausted"
            return out
        correction = reply_raw.get("text") or reply_raw.get("correction") or ""
        revised = False
        while remaining >= 1:
            remaining -= 1
            asks_used += 1
            prompt = (
                "E conflict revise four-part with user correction (verbatim). JSON only. "
                + json.dumps(
                    {
                        "prior_four_part": four_part,
                        "user_correction_verbatim": correction,
                        "require": (
                            "输出四键非空：推测、依据、可能错在哪、下一步。"
                            "必须吸收 user_correction_verbatim 的含义，"
                            "更新「推测」或「下一步」或「可能错在哪」，禁止原样照抄 prior_four_part。"
                            "「下一步」必须恰好为下列之一："
                            + "/".join(sorted(FIGURE4_POINTS))
                        ),
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
            raw = chat(prompt)
            tracer.write(
                KIND_FOUR_PART,
                {"attempt": asks_used, "phase": "correct", "raw": raw},
            )
            normalized = _normalize_four_part(raw)
            if normalized is not None and _four_part_complete(normalized):
                four_part = normalized
                revised = True
                break
        if not revised:
            out = _handoff_stop(env, source="ask_exhausted", four_part=four_part)
            out["trace_paths"] = list(tracer.paths)
            out["ok"] = False
            out["status"] = "ask_exhausted"
            return out


class ConflictE:
    """供 S 调用的适配器：body 包 → 信封 → run_conflict_e。

    可选 auto_user_decision：测试用，跳过交互直接返回决定（仍先跑四段）。
    """

    def __init__(
        self,
        *,
        run_id: Any,
        chat: Callable[[str], Any],
        user_callback: Optional[Callable[[Mapping[str, Any]], Any]] = None,
        conflict_ask_max: Any = 3,
        clock: Optional[Callable[[], str]] = None,
        session_seq_start: int = 1,
        auto_user_decision: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.run_id = require_run_id(run_id)
        self.chat = chat
        self._user_callback = user_callback
        self.conflict_ask_max = conflict_ask_max
        self.clock = clock or _default_clock
        self._next_seq = session_seq_start
        self.auto_user_decision = (
            dict(auto_user_decision) if auto_user_decision else None
        )
        self.received: list[dict[str, Any]] = []
        self.last_result: Optional[dict[str, Any]] = None

    def _alloc_seq(self) -> int:
        seq = self._next_seq
        self._next_seq += 1
        return seq

    def handle(self, package: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(package)
        validate_e_package(body)
        self.received.append(body)
        envelope = {
            "run_id": self.run_id,
            "entry_kind": "from_s_stage",
            "conflict_session_seq": self._alloc_seq(),
            "trigger_source": "S",
            "kind": body.get("kind"),
            "body": body,
        }

        def user_cb(four_part: Mapping[str, Any]) -> Any:
            if self.auto_user_decision is not None:
                return dict(self.auto_user_decision)
            if self._user_callback is None:
                return {"reply_kind": DECISION_STOP}
            return self._user_callback(four_part)

        result = run_conflict_e(
            envelope=envelope,
            chat=self.chat,
            user_callback=user_cb,
            conflict_ask_max=self.conflict_ask_max,
            clock=self.clock,
        )
        self.last_result = result
        # 对 S 兼容：露出 decision/source
        return {
            "decision": result.get("decision"),
            "source": result.get("source"),
            "echo_kind": body.get("kind"),
            "handoff": result,
        }
