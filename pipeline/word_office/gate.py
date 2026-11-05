# -*- coding: utf-8 -*-
"""Word Pre-check（轻门）：tier / ops 白名单 / find 非空 / 数量 / 预算。

原则：
- 白名单、预算**读契约**（不在代码里另存一份）。
- `errors` → 拒绝执行；`warnings` → **只提示**（不硬拒，除非 `hard_budget`）。
"""
from __future__ import annotations

import config
from ai.context_budget import estimate_tokens
from pipeline.synth_contract.registry import load_contract

TIERS = ("A", "B", "C", "D", "E")


def _word_office() -> dict:
    cv = load_contract()
    return dict(cv.data.get("word_office") or {})


def ops_whitelist() -> set:
    return set(_word_office().get("ops_whitelist") or [])


def budget_limits() -> dict:
    """契约默认 + config 覆盖（config 是用户可改的）。"""
    base = dict(_word_office().get("budget") or {})
    override = config.get_param("word_office.budget") or {}
    if isinstance(override, dict):
        base.update(override)
    return base


def estimate_plan_tokens(plan: dict) -> int:
    total = 0
    for op in (plan.get("operations") or []):
        text = str(op.get("find") or "") + str(op.get("replace") or "")
        if text:
            total += estimate_tokens(text)
    return total


def pre_check(plan: dict) -> dict:
    """返回 {ok, errors[], warnings[], estimated_tokens, limit}。"""
    errors: list = []
    warnings: list = []
    tier = str(plan.get("tier") or "")
    if tier not in TIERS:
        errors.append("bad_tier:%s" % tier)

    ops = list(plan.get("operations") or [])
    if not ops:
        errors.append("no_ops")
    if len(ops) > 20:
        errors.append("ops_too_many:%d" % len(ops))

    wl = ops_whitelist()
    for op in ops:
        name = str(op.get("op") or "")
        if name not in wl:
            errors.append("bad_op:%s" % name)
        if name == "find_replace":
            find = str(op.get("find") or "")
            repl = str(op.get("replace") or "")
            if not find or find == repl:
                errors.append("bad_find_replace")

    limits = budget_limits()
    limit = int(limits.get(tier, 0) or 0)
    est = estimate_plan_tokens(plan)
    if limit and est > limit:
        warnings.append("over_budget:%d>%d" % (est, limit))
        if config.get_param("word_office.hard_budget", False):
            errors.append("over_budget_hard")

    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "estimated_tokens": est, "limit": limit}
