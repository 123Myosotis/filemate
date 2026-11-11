# -*- coding: utf-8 -*-
"""契约 `intent_rules` 的守卫：**每条 pattern 必须能编译**，且**关键短语归属正确**。

防止"JSON 里反斜杠写错"和"规则互相抢"。
"""
from __future__ import annotations


def _rules():
    from pipeline.synth_contract.registry import load_contract
    return (load_contract().data.get("word_office") or {}).get("intent_rules") or []


def test_intent_rules_compile():
    import re
    rules = _rules()
    assert rules, "没有 intent_rules"
    for r in rules:
        rid = r.get("id")
        pat = str(r.get("pattern") or "")
        assert pat, "规则 %s 缺 pattern" % rid
        try:
            re.compile(pat)
        except re.error as e:  # noqa: PERF203
            raise AssertionError("规则 %s 的 pattern 编译失败: %s" % (rid, e))


def test_intent_rules_have_priority_unique():
    rules = _rules()
    ids = [r.get("id") for r in rules]
    assert len(ids) == len(set(ids)), "规则 id 重复"
    prios = [int(r.get("priority") or 0) for r in rules]
    assert len(prios) == len(set(prios)), "规则 priority 重复（会互相抢）"


def test_tracked_beats_replace():
    """`修订：把 A 改成 B` 必须属于 `tracked_change`（不是 replace）。"""
    from pipeline.word_office.module import _plan_ops
    ops = _plan_ops("修订：把 A 改成 B")
    assert ops[0]["op"] == "tracked_change"


def test_plain_replace():
    from pipeline.word_office.module import _plan_ops
    assert _plan_ops("把 A 改成 B")[0]["op"] == "find_replace"


def test_insert_delete():
    from pipeline.word_office.module import _plan_ops
    assert _plan_ops("在 A 后面插入 B")[0]["op"] == "insert"
    assert _plan_ops("删除 A")[0]["op"] == "delete"
