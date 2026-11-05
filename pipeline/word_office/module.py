# -*- coding: utf-8 -*-
"""Word 编辑模块：L0 最小实现（结构检查 + 确定性替换）+ 重架构接口占位。

重架构（九对象/七状态机/决策表/Overlay/preflight/分片/事务层/五角色/审计）
在本文件里全部以**占位函数**存在，后续按需替换实现（不改调用点）。
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from docx import Document

import config
from .state import is_clean, predicates, scan

# ---------------------------------------------------------------
# 重架构占位（后续接；现在不改行为）
# ---------------------------------------------------------------


_TIER_HINTS = ("校对", "错别字", "润色", "改写", "摘要", "总结", "标记", "术语", "建议")


def _decide_tier(text: str, state: dict, preds: dict) -> str:
    """决策表占位：有 AI 类词 → B；否则 A（确定性）。"""
    if any(h in str(text or "") for h in _TIER_HINTS):
        return "B"
    return "A"


def _finish(result: dict, stages: list = None, tokens: dict = None,
            warnings: list = None) -> dict:
    """统一返回结构：status / stages / fail_locus / tokens / warnings。"""
    result.setdefault("stages", stages or [])
    result.setdefault("fail_locus", "")
    result.setdefault("tokens", tokens or {})
    result.setdefault("warnings", warnings or [])
    return result


def _apply_overlay(plan: dict) -> dict:
    """ConfirmationOverlay 占位：现在不改 requires_confirmation。"""
    return plan


def _preflight(plan: dict) -> dict:
    """capability_preflight 占位：现在放行（后续加能力/代价/分片）。"""
    return {"ok": True, "reason": ""}


def _maybe_split(state: dict) -> list:
    """分片占位：现在不分片。"""
    return [state]


def _authorize(request: dict) -> bool:
    """五角色/双人确认占位：单用户，直接放行。"""
    return True


def _audit(event: dict) -> None:
    """审计占位：现在不落盘（后续接双通道审计）。"""
    return None


# ---------------------------------------------------------------
# L0 工具（确定性，不调模型）
# ---------------------------------------------------------------


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def _iter_paragraphs(doc):
    for p in doc.paragraphs:
        yield p
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    yield p


def _replace(doc, find: str, repl: str) -> tuple[int, int]:
    """先 run 级（保格式）；跨 run 的再段落级合并（会丢该段内部行内格式）。

    返回 `(run_level_count, paragraph_level_count)`，让调用方如实报告。
    """
    n_run = 0
    n_para = 0
    for p in _iter_paragraphs(doc):
        hit = False
        for r in p.runs:
            if find in r.text:
                r.text = r.text.replace(find, repl)
                n_run += 1
                hit = True
        if hit:
            continue
        ts = p._p.findall(".//" + _W + "t")
        if not ts:
            continue
        whole = "".join(t.text or "" for t in ts)
        if find not in whole:
            continue
        new = whole.replace(find, repl)
        ts[0].text = new
        ts[0].set(_XML_SPACE, "preserve")
        for t in ts[1:]:
            t.text = ""
        n_para += whole.count(find)
    return n_run, n_para


def _inspect_msg(state: dict, preds: dict) -> str:
    styles = "，".join(f"{k}×{v}" for k, v in sorted(
        state["styles"].items(), key=lambda kv: -kv[1])[:5]) or "（无样式信息）"
    tables = "；".join(
        f"{t['rows']}×{t['cols']}（合并 {t['merged']} 处）" for t in state["tables"]
    ) or "（无表格）"
    dirty = [k for k in (
        "no_revision_in_target", "no_content_control", "no_field_in_range",
        "no_bookmark_in_range", "no_nested_table")
        if preds.get(k, {}).get("value") != "true"]
    return (
        "📄 结构检查：段落 %d 个；样式 %s；表格 %s；图片 %d 张。\n"
        "结构风险：%s"
        % (state["paragraphs"], styles, tables, state["inline_shapes"],
           "无" if not dirty else "、".join(dirty))
    )


_REPLACE_PATTERNS = (
    re.compile(r"(?:把|将)\s*(.+?)\s*(?:替换成|替换为|改成|改为|换成|换为)\s*(.+?)(?:[。，,；;]|$)"),
    re.compile(r"(.+?)\s*(?:替换成|替换为|改成|改为|换成|换为)\s*(.+?)(?:[。，,；;]|$)"),
)


def _plan_ops(text: str) -> list:
    for pat in _REPLACE_PATTERNS:
        m = pat.search(text)
        if m:
            find = m.group(1).strip().strip("「」\"' ")
            repl = m.group(2).strip().strip("「」\"' ")
            if find and find != repl:
                return [{"op": "find_replace", "find": find, "replace": repl}]
    return [{"op": "inspect"}]


# ---------------------------------------------------------------
# 唯一对外入口
# ---------------------------------------------------------------


class WordModule:
    @staticmethod
    def handle(request: dict) -> dict:
        stages: list = []
        if not _authorize(request):
            return _finish(_fail("unauthorized", "当前无权执行 Word 操作。"))

        text = str(request.get("user_input") or "")
        selected = [str(p) for p in (request.get("selected_files") or []) if str(p).strip()]
        docxs = [p for p in selected if p.lower().endswith(".docx")]
        if not docxs:
            return _finish(_fail("no_docx", "请先在文件管理器里选中一个 .docx 文件，我才能处理。"))
        target = Path(docxs[0])
        if not target.is_file():
            return _finish(_fail("not_found", "选中的文件不存在：%s" % target.name))

        state = scan(target)
        stages.append({"name": "scan", "status": "ok", "detail": "%d 段" % state["paragraphs"]})
        preds = predicates(state)
        tier = _decide_tier(text, state, preds)
        if not is_clean(preds) and tier == "A":
            dirty = [k for k in (
                "no_revision_in_target", "no_content_control", "no_field_in_range",
                "no_bookmark_in_range", "no_nested_table")
                if preds.get(k, {}).get("value") != "true"]
            return _finish(_fail(
                "needs_confirmation",
                "这份文档有结构风险（%s），A 级不静默执行。" % "、".join(dirty)), stages)

        plan = {"tier": tier,
                "operations": [{"op": "l1"}] if tier == "B" else _plan_ops(text)}
        plan = _apply_overlay(plan)

        from .gate import pre_check
        pre = pre_check(plan)
        if not pre["ok"]:
            return _finish(
                _fail("pre_check", "预检未通过：%s" % "、".join(pre["errors"])),
                stages, warnings=pre["warnings"])
        warnings = list(pre["warnings"])

        pre2 = _preflight(plan)  # 旧占位，保留（现在实际由 pre_check 承担）
        if not pre2.get("ok"):
            return _finish(_fail("preflight", str(pre2.get("reason") or "")),
                           stages, warnings=warnings)

        if tier == "B":
            result = _exec_l1(target, state, preds, text)
        else:
            result = _execute(target, state, preds, plan)
        _audit({"path": str(target), "tier": tier, "ops": len(plan["operations"]),
                "status": result.get("status")})
        try:
            from .evidence import write_evidence
            write_evidence({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "user_input": text,
                "tier": tier,
                "file": {"path": str(target),
                         "structure_sha256": state.get("structure_sha256", "")},
                "ops": len(plan["operations"]),
                "before": {"paragraphs": state["paragraphs"],
                           "tables": state["tables"]},
                "status": result.get("status"),
                "reason": result.get("reason"),
            })
        except Exception:  # noqa: BLE001
            pass
        return _finish(result, stages, warnings=warnings)


def _portrait(target: Path, state: dict, limit: int = 30) -> str:
    """文档画像：段落数 + 标题样式 + 前 N 段正文（轻量，不传全文）。"""
    doc = Document(str(target))
    paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()][:limit]
    heads = [k for k in state["styles"] if "Heading" in k or k == "Title"]
    return "段落数 %d；标题样式：%s\n正文前 %d 段：\n%s" % (
        state["paragraphs"], "、".join(heads) or "无", len(paras),
        "\n".join("- " + p[:200] for p in paras))


def _exec_l1(target: Path, state: dict, preds: dict, user_input: str) -> dict:
    """L1：AI 只出 {summary, ops:[{find,replace}]}；本地白名单 + 备份 + 结构校验 + 内容对账。"""
    api_key = config.get_api_key()
    if not api_key:
        return _fail("no_key", "L1 需要 API Key，请先在设置里配置。")
    from .ai import call_l1

    data = call_l1(user_input, _portrait(target, state), api_key)
    if not isinstance(data, dict):
        return _fail("ai_failed", "AI 没有返回可用结果。")

    summary = str(data.get("summary") or "").strip()
    ops = []
    for o in (data.get("ops") or []):
        if not isinstance(o, dict):
            continue
        find = str(o.get("find") or "").strip()
        repl = str(o.get("replace") or "").strip()
        if find and find != repl:
            ops.append({"find": find, "replace": repl,
                        "reason": str(o.get("reason") or "")})
    if not ops:
        return {"status": "done", "reason": "suggest",
                "message": "💡 " + (summary or "AI 没有给出可执行的修改。"),
                "plan_ref": None, "audit_ref": None, "user_actions": []}

    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    doc = Document(str(target))
    n_run_total = 0
    n_para_total = 0
    for op in ops:
        nr, np = _replace(doc, op["find"], op["replace"])
        n_run_total += nr
        n_para_total += np
    if n_run_total == 0 and n_para_total == 0:
        return _fail("not_found", "AI 给的修改在文档里一处都没找到，未写入。")
    doc.save(str(target))

    after = scan(target)
    if (after["paragraphs"] != state["paragraphs"]
            or after["styles"] != state["styles"]
            or after["tables"] != state["tables"]):
        shutil.copy2(backup, target)
        return _fail("verify_failed", "修改改变了文档结构，已恢复原文件。")
    for op in ops:
        if _count_all(target, op["find"]) != 0:
            shutil.copy2(backup, target)
            return _fail("verify_failed", "有修改未生效或残留，已恢复原文件。")

    note = "（其中 %d 处跨 run，已合并该段内部行内格式）" % n_para_total if n_para_total else ""
    tail = ("\nAI 说明：" + summary) if summary else ""
    return {"status": "done", "reason": "l1_replace",
            "message": "✅ 已按 AI 建议修改 %d 处%s（写前备份：%s）。%s"
                       % (n_run_total + n_para_total, note, backup.name, tail),
            "plan_ref": None, "audit_ref": None, "user_actions": []}


def _fail(reason: str, message: str) -> dict:
    return {"status": "failed", "reason": reason, "message": message,
            "plan_ref": None, "audit_ref": None, "user_actions": []}


def _count_all(path: Path, needle: str) -> int:
    """按「段落内所有 w:t 拼接」计数，覆盖跨 run 的情况。"""
    if not needle:
        return 0
    doc = Document(str(path))
    n = 0
    for p in _iter_paragraphs(doc):
        ts = p._p.findall(".//" + _W + "t")
        n += "".join(t.text or "" for t in ts).count(needle)
    return n


def _execute(target: Path, state: dict, preds: dict, plan: dict) -> dict:
    op = (plan["operations"] or [{"op": "inspect"}])[0]
    if op["op"] == "inspect":
        return {"status": "done", "reason": "inspect",
                "message": _inspect_msg(state, preds),
                "plan_ref": None, "audit_ref": None, "user_actions": []}

    find, repl = op.get("find", ""), op.get("replace", "")
    before = _count_all(target, find)
    if before == 0:
        return _fail("not_found", "文档里没找到「%s」。" % find)

    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    doc = Document(str(target))
    n_run, n_para = _replace(doc, find, repl)
    doc.save(str(target))

    after = scan(target)
    if (after["paragraphs"] != state["paragraphs"]
            or after["tables"] != state["tables"]):
        shutil.copy2(backup, target)
        return _fail("verify_failed", "替换改变了文档结构，已恢复原文件。")
    if _count_all(target, find) != 0:
        shutil.copy2(backup, target)
        return _fail("verify_failed", "替换后仍有残留，已恢复原文件。")
    note = "（其中 %d 处跨 run，已合并该段内部行内格式）" % n_para if n_para else ""
    return {"status": "done", "reason": "find_replace",
            "message": "✅ 已替换 %d 处「%s」→「%s」%s（写前备份：%s）。"
                       % (n_run + n_para, find, repl, note, backup.name),
            "plan_ref": None, "audit_ref": None, "user_actions": []}
