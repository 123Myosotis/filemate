# -*- coding: utf-8 -*-
"""Word 模块 D/E Report（架构 §7）：严闸门，宁可不出也不乱出。

复用 `export/word_renderer`；A/B/C 与只读绝不生成。
报告失败不推翻已成功的写回。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path


# 与 module._READONLY_OPS 对齐（避免循环 import，此处自维护只读集合）
_READONLY_OPS = frozenset({
    "inspect", "list_comments", "list_controls", "list_fields",
})


def should_emit_report(*, tier: str, result: dict, plan: dict,
                       request: dict) -> bool:
    """是否生成交付报告。全部条件都要满足。"""
    req = request or {}
    res = result or {}
    if req.get("no_report") or req.get("dry_run") or res.get("dry_run"):
        return False
    if req.get("rollback"):
        return False
    if str(res.get("status") or "") != "done":
        return False
    if str(tier or "") not in ("D", "E"):
        return False
    ops = list((plan or {}).get("operations") or [])
    write_ops = [o for o in ops if str(o.get("op") or "") not in _READONLY_OPS]
    if not write_ops:
        return False
    # 必须有真实生效处数：committed 但 applied=0 也不出（防空跑刷报告）
    # B 的 l1 本就不会进本闸门；D/E 只认写成功
    applied = int(res.get("applied") or 0)
    if applied <= 0:
        return False
    if str((res.get("txn") or {}).get("phase") or "") != "committed":
        return False
    # E：必须显式 batch / emit_report，防止「大文档提示」被误当成 E 刷报告
    if str(tier) == "E" and not (req.get("batch") or req.get("emit_report")):
        return False
    return True


def build_report_blocks(*, target: Path, user_input: str, tier: str,
                        plan: dict, result: dict, stages: list,
                        warnings: list) -> list:
    """机械拼装块；不再调模型。"""
    ops = list((plan or {}).get("operations") or [])
    op_lines = []
    for o in ops[:20]:
        name = str(o.get("op") or "")
        if name in _READONLY_OPS:
            continue
        bits = [name]
        if o.get("find"):
            bits.append("find=%s" % str(o.get("find"))[:40])
        if o.get("replace"):
            bits.append("→ %s" % str(o.get("replace"))[:40])
        op_lines.append("· " + " ".join(bits))
    blocks = [
        ("h2", "任务"),
        ("p", "级别：%s（仅 D/E 出报告）" % tier),
        ("p", "指令：%s" % (str(user_input or "")[:500] or "（无）")),
        ("p", "目标：%s" % target.name),
        ("h2", "结果"),
        ("p", "状态：%s / %s" % (
            result.get("status"), result.get("reason") or "—")),
        ("p", "生效处数：%s" % (result.get("applied") if result.get("applied") is not None else "—")),
    ]
    if op_lines:
        blocks.append(("h2", "操作"))
        blocks.append(("p", "\n".join(op_lines)))
    else:
        blocks.append(("p", "（无写操作明细）"))
    warn = [str(w) for w in (warnings or []) if str(w).strip()]
    if warn:
        blocks.append(("h2", "警告"))
        blocks.append(("p", "\n".join(warn[:20])))
    st = []
    for s in (stages or [])[:20]:
        if isinstance(s, dict):
            st.append("%s: %s (%s)" % (
                s.get("name") or "?", s.get("status") or "?",
                s.get("detail") or ""))
    if st:
        blocks.append(("h2", "阶段"))
        blocks.append(("p", "\n".join(st)))
    bak = ((result.get("patch_set") or {}).get("backup")
           or (result.get("txn") or {}).get("archive") or "")
    if bak:
        blocks.append(("h2", "备份"))
        blocks.append(("p", str(bak)))
    blocks.append(("p", "生成时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    blocks.append(("p", "说明：本报告由本地规则生成，未额外调用模型。"))
    return blocks


def emit_report(*, target: Path, user_input: str, tier: str,
                plan: dict, result: dict, stages: list = None,
                warnings: list = None) -> str | None:
    """写报告到目标旁；失败返回 None（不抛）。"""
    try:
        from export.word_renderer import render_report
    except Exception:  # noqa: BLE001
        return None
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = Path(target).with_name(
            "%s_编辑报告_%s_%s.docx" % (Path(target).stem, tier, ts))
        blocks = build_report_blocks(
            target=Path(target), user_input=user_input, tier=tier,
            plan=plan or {}, result=result or {},
            stages=list(stages or []), warnings=list(warnings or []))
        title = "FileMate Word 编辑报告（%s）" % tier
        return render_report(out, title, blocks)
    except Exception:  # noqa: BLE001
        return None
