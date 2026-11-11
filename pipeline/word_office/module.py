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
from docx.oxml.ns import qn

import config
from .state import is_clean, predicates, scan

# ---------------------------------------------------------------
# 重架构占位（后续接；现在不改行为）
# ---------------------------------------------------------------


_TIER_HINTS = ("校对", "错别字", "润色", "改写", "摘要", "总结", "标记", "术语", "建议")


def _decide_tier(text: str, state: dict, preds: dict) -> str:
    """**决策表**（读契约 `word_office.decision_rules`）：按规则匹配 → tier。"""
    try:
        from pipeline.synth_contract.registry import load_contract
        rules = (load_contract().data.get("word_office") or {}).get("decision_rules") or []
    except Exception:  # noqa: BLE001
        rules = []
    if not rules:
        # 兜底：老行为
        return "B" if any(h in str(text or "") for h in _TIER_HINTS) else "A"
    for r in rules:
        when = r.get("when") or {}
        if when.get("default"):
            return str(r.get("tier") or "A")
        kws = when.get("any_keyword") or []
        if kws and any(k in str(text or "") for k in kws):
            return str(r.get("tier") or "A")
    return "A"


def _finish(result: dict, stages: list = None, tokens: dict = None,
            warnings: list = None) -> dict:
    """统一返回结构：status / stages / fail_locus / tokens / warnings。

    **七状态机**：`done` / `failed` / `partially_completed` /
    `plan_invalidated` / `failed_postcheck` / `compensation_failed` / `rollback_failed`。
    """
    result.setdefault("stages", stages or [])
    result.setdefault("fail_locus", "")
    result.setdefault("tokens", tokens or {})
    result.setdefault("warnings", warnings or [])
    if result.get("status") not in _STATUS:
        result["status"] = "failed"
    return result


def _apply_overlay(plan: dict) -> dict:
    """ConfirmationOverlay：危险 op → `modal`；写进 `plan["confirmation"]`。"""
    from .objects import ConfirmationOverlay

    ops = [str(o.get("op") or "") for o in (plan.get("operations") or [])]
    if "delete" in ops or "tracked_change" in ops:
        plan["confirmation"] = ConfirmationOverlay(
            level="modal", reason="dangerous_op",
            user_actions=[{"label": "确认执行", "action": "confirm"}]).__dict__
    else:
        plan["confirmation"] = ConfirmationOverlay(level="silent").__dict__
    return plan


def _preflight(plan: dict) -> dict:
    """Preflight：**能力执行器**校验（每个 op 必须在 `ToolRegistry` 里）。"""
    try:
        from .objects import ToolRegistry
        tools = ToolRegistry().tools
    except Exception:  # noqa: BLE001
        tools = {}
    for op in (plan.get("operations") or []):
        name = str(op.get("op") or "")
        if not name:
            return {"ok": False, "reason": "op_empty"}
        if tools and name not in tools and name not in ("find_replace", "l1"):
            return {"ok": False, "reason": "no_executor:%s" % name}
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


_INSERT_PATTERNS = (
    re.compile(r"在\s*(.+?)\s*(?:后面|之后|后)\s*插入\s*(.+?)(?:[。，,；;]|$)"),
    re.compile(r"把\s*(.+?)\s*(?:后面|之后|后)\s*插入\s*(.+?)(?:[。，,；;]|$)"),
)
_DELETE_PATTERNS = (
    re.compile(r"(?:删除|删掉|去掉)(?!\s*原标题)\s*(?:这一段|这一行|这一句)?\s*(.+?)(?:[。，,；;]|$)"),
)
_FORMAT_PATTERNS = (
    re.compile(r"(?:把|将)\s*(.+?)\s*(?:加粗|设为粗体|变粗)"),
    re.compile(r"(?:把|将)\s*(.+?)\s*(?:改斜体|设为斜体)"),
    re.compile(r"(?:把|将)\s*(.+?)\s*(?:设为|改成|改为)\s*(\d+)\s*(?:号|磅|pt)"),
    re.compile(r"(?:把|将)\s*(.+?)\s*(?:设为|改成|改为)\s*(红|蓝|绿|黑|黄)色?"),
    re.compile(r"(?:把|将)\s*(.+?)\s*(?:设为|改成|改为)\s*([\u4e00-\u9fff]{2,4})\s*字体"),
)
_MOVE_PATTERNS = (
    re.compile(r"(?:把|将)\s*(.+?)\s*(?:移到|移动到|挪到)\s*(.+?)\s*(后面|之后|后|前面|之前|前)"),
)
_TRACKED_PATTERNS = (
    re.compile(r"(?:修订|留痕|记录修订)[：:]\s*(?:把|将)\s*(.+?)\s*(?:替换成|替换为|改成|改为|换成)\s*(.+?)(?:[。，,；;]|$)"),
)
_OUTLINE_PATTERNS = (
    re.compile(r"(?:把|将)\s*(.+?)\s*设为\s*第?\s*([一二三四五六])\s*级标题"),
    re.compile(r"(?:把|将)\s*(.+?)\s*(?:设为|改成|改为)\s*(段落|正文)"),
)
_TABLE_TO_TEXT_PATTERNS = (
    re.compile(r"(?:把|将)\s*(.+?)\s*(?:表格|表)\s*(?:转成|转为|变成|改成)\s*(?:文字|正文|段落)"),
    re.compile(r"(?:把|将)\s*表格\s*(?:转成|转为|变成)\s*(?:文字|正文)"),
)
_TEXT_TO_TABLE_PATTERNS = (
    re.compile(r"(?:把|将)\s*(.+?)\s*(?:转成|转为|变成|改成)\s*表格"),
)
_TABLE_TO_OUTLINE_PATTERNS = (
    re.compile(r"(?:把|将)\s*(.+?)\s*(?:表格|表)\s*(?:转成|转为|变成|改成)\s*(?:大纲|标题)"),
)
_OUTLINE_TO_TABLE_PATTERNS = (
    re.compile(r"(?:把|将)\s*(.+?)\s*(?:大纲|标题)\s*(?:转成|转为|变成|改成)\s*(?:表格|表)"),
    re.compile(r"(?:把|将)\s*(?:大纲|标题)\s*(?:转成|转为|变成|改成)\s*(?:表格|表)"),
)


def _plan_ops(text: str) -> list:
    # **规则表优先**（契约 `word_office.intent_rules`）；没有 → 退回正则
    try:
        from pipeline.synth_contract.registry import load_contract
        rules = (load_contract().data.get("word_office") or {}).get("intent_rules") or []
    except Exception:  # noqa: BLE001
        rules = []
    if rules:
        for rule in sorted(rules, key=lambda r: -int(r.get("priority") or 0)):
            pat = str(rule.get("pattern") or "")
            if not pat:
                continue
            try:
                m = re.search(pat, text)
            except re.error:
                continue
            if not m:
                continue
            op = str(rule.get("op") or "")
            if op == "find_replace" and len(m.groups()) >= 2:
                find = m.group(1).strip().strip("「」\"' ")
                repl = m.group(2).strip().strip("「」\"' ")
                if find and find != repl:
                    return [{"op": "find_replace", "find": find, "replace": repl}]
            elif op == "insert" and len(m.groups()) >= 2:
                anchor = m.group(1).strip().strip("「」\"' ")
                content = m.group(2).strip().strip("「」\"' ")
                if anchor and content:
                    return [{"op": "insert", "anchor": anchor, "content": content}]
            elif op == "tracked_change" and len(m.groups()) >= 2:
                find = m.group(1).strip().strip("「」\"' ")
                repl = m.group(2).strip().strip("「」\"' ")
                if find and find != repl:
                    return [{"op": "tracked_change", "find": find, "replace": repl}]
            elif op == "delete" and len(m.groups()) >= 1:
                find = m.group(1).strip().strip("「」\"' ")
                if find:
                    return [{"op": "delete", "find": find}]
    for pat in _INSERT_PATTERNS:
        m = pat.search(text)
        if m:
            anchor = m.group(1).strip().strip("「」\"' ")
            content = m.group(2).strip().strip("「」\"' ")
            if anchor and content:
                return [{"op": "insert", "anchor": anchor, "content": content}]
    for pat in _TRACKED_PATTERNS:
        m = pat.search(text)
        if m:
            find = m.group(1).strip().strip("「」\"' ")
            repl = m.group(2).strip().strip("「」\"' ")
            if find and find != repl:
                return [{"op": "tracked_change", "find": find, "replace": repl}]
    # **先说"大纲→表"**（因为它和"X 转成表格"长得像）
    for pat in _OUTLINE_TO_TABLE_PATTERNS:
        m = pat.search(text)
        if m:
            find = (m.group(1) if m.groups() else "").strip().strip("「」\"' ")
            # **删除原标题 = 显式 opt-in**（默认保留，安全）
            remove = bool(re.search(r"(?:删除原标题|移除原标题|去掉原标题|搬走原标题|搬走)", text))
            return [{"op": "outline_to_table", "find": find, "remove_source": remove}]
    for pat in _TABLE_TO_OUTLINE_PATTERNS:
        m = pat.search(text)
        if m:
            find = (m.group(1) or "").strip().strip("「」\"' ")
            if find:
                return [{"op": "table_to_outline", "find": find}]
    for pat in _TEXT_TO_TABLE_PATTERNS:
        m = pat.search(text)
        if m:
            find = (m.group(1) or "").strip().strip("「」\"' ")
            if find:
                return [{"op": "text_to_table", "find": find}]
    for pat in _TABLE_TO_TEXT_PATTERNS:
        m = pat.search(text)
        if m:
            find = (m.group(1) if m.groups() else "").strip().strip("「」\"' ")
            return [{"op": "table_to_text", "find": find}]
    for pat in _OUTLINE_PATTERNS:
        m = pat.search(text)
        if m:
            find = m.group(1).strip().strip("「」\"' ")
            level = m.group(2).strip()
            if not find:
                continue
            if level in ("段落", "正文"):
                return [{"op": "outline", "find": find, "level": 0}]
            num = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}.get(level, 1)
            return [{"op": "outline", "find": find, "level": num}]
    for pat in _DELETE_PATTERNS:
        m = pat.search(text)
        if m:
            find = m.group(1).strip().strip("「」\"' ")
            if find:
                return [{"op": "delete", "find": find}]
    for pat in _FORMAT_PATTERNS:
        m = pat.search(text)
        if m:
            find = m.group(1).strip().strip("「」\"' ")
            if not find:
                continue
            if "粗" in text:
                return [{"op": "format", "find": find, "mode": "bold"}]
            if "斜" in text:
                return [{"op": "format", "find": find, "mode": "italic"}]
            size = re.search(r"(\d+)\s*(?:号|磅|pt)", text)
            if size:
                return [{"op": "format", "find": find, "mode": "size",
                         "size": int(size.group(1))}]
            color = re.search(r"(?:设为|改成|改为)\s*(红|蓝|绿|黑|黄)色?", text)
            if color:
                return [{"op": "format", "find": find, "mode": "color",
                         "color": color.group(1)}]
            font = re.search(r"(?:设为|改成|改为)\s*([\u4e00-\u9fff]{2,4})\s*字体", text)
            if font:
                return [{"op": "format", "find": find, "mode": "font",
                         "font": font.group(1)}]
    for pat in _MOVE_PATTERNS:
        m = pat.search(text)
        if m:
            src = m.group(1).strip().strip("「」\"' ")
            dst = m.group(2).strip().strip("「」\"' ")
            before = any(k in text for k in ("前面", "之前", "前"))
            if src and dst:
                return [{"op": "move", "src": src, "dst": dst, "before": before}]
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

        # **真 dry-run**：在临时副本上跑，原文件一个字节都不碰
        dry = bool(request.get("dry_run"))
        work = target
        tmp = None
        if dry:
            tmp = target.with_suffix(".dryrun" + target.suffix)
            shutil.copy2(target, tmp)
            work = tmp

        state = scan(work)
        stages.append({"name": "scan", "status": "ok", "detail": "%d 段" % state["paragraphs"]})
        # 分片路由：大文档 → 提示（**只提示，不强制分块**）
        try:
            from .chunk import route as _route
            rt = _route(target)
            if rt.get("should_split"):
                stages.append({"name": "route", "status": "warn",
                               "detail": "large_doc（%d 段）建议分块" % rt.get("paragraphs")})
        except Exception:  # noqa: BLE001
            pass
        # 术语自动挖掘（规则，不调 LLM）——扫完就挖，落库当"候选"
        try:
            from .memory import mine_and_save
            doc = Document(str(target))
            body = "\n".join(p.text for p in doc.paragraphs if p.text.strip())[:20000]
            mine_and_save(body)
        except Exception:  # noqa: BLE001
            pass
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

        # 置信度：**fallback 到 inspect 且用户没说"检查/结构" → 低置信**
        ops = plan.get("operations") or []
        low = False
        for o in ops:
            if o.get("op") == "inspect" and not any(
                    k in text for k in ("检查", "结构", "看看", "看看结构")):
                o["confidence"] = 0.3
                low = True
            else:
                o.setdefault("confidence", 0.9)
        if low and not request.get("confirmed"):
            return _finish({
                "status": "needs_confirmation",
                "reason": "low_confidence",
                "message": "我没太确定你要做什么（是「检查结构」还是别的？）。请确认后再执行。",
                "user_actions": [{"label": "确认按「结构检查」执行", "action": "confirm"}],
                "plan_ref": None, "audit_ref": None,
            }, stages)

        # 危险操作（delete）走确认（架构 §3：低置信/危险操作要弹窗）
        if any(o.get("op") == "delete" for o in plan["operations"]):
            if not request.get("confirmed"):
                return _finish({
                    "status": "needs_confirmation",
                    "reason": "dangerous_op",
                    "message": "删除是危险操作，请确认后再执行。",
                    "user_actions": [{"label": "确认删除", "action": "confirm_delete"}],
                    "plan_ref": None, "audit_ref": None,
                }, stages)

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

        # **独立备份**：复制到 storage/backups/word/（不依赖 .bak）
        bak2 = None
        try:
            from pathlib import Path as _P
            bkroot = _P(__file__).resolve().parents[2] / "storage" / "backups" / "word"
            bkroot.mkdir(parents=True, exist_ok=True)
            bak2 = bkroot / ("%s_%s" % (time.strftime("%Y%m%d-%H%M%S"), work.name))
            shutil.copy2(work, bak2)
            # **保留 N 份**（按 mtime 只留最近 20 份/每文件）
            groups: dict = {}
            for f in bkroot.iterdir():
                if not f.is_file():
                    continue
                key = f.name.split("_", 1)[-1]
                groups.setdefault(key, []).append(f)
            for key, files in groups.items():
                files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                for old in files[20:]:
                    try:
                        old.unlink()
                    except OSError:
                        pass
        except Exception:  # noqa: BLE001
            bak2 = None

        if tier == "B":
            related = []
            try:
                from .memory import get_file_memory, recall
                from .state import file_sha256
                self_mem = get_file_memory(file_sha256(target))
                if self_mem:
                    related.append(self_mem)
                related.extend(recall(text))
            except Exception:  # noqa: BLE001
                pass
            result = _exec_l1(work, state, preds, text, related=related)
            if related:
                result.setdefault("memory", related)
        else:
            result = _execute(work, state, preds, plan)
        # PatchSet：这次改了哪些 + 备份 + 生效数（**真实 applied**）
        try:
            from .objects import PatchSet
            ps = PatchSet(ops=list(plan.get("operations") or []),
                          backup_path=str(target) + ".bak",
                          applied=int(result.get("applied") or 0))
            result["patch_set"] = {"ops": ps.ops, "backup": ps.backup_path,
                                   "applied": ps.applied}
        except Exception:  # noqa: BLE001
            pass
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
        # 记忆（lazy，4.2）：执行后按"文件内容 hash"存一条（**不调 LLM**，用 state 的廉价摘要）
        try:
            from .memory import save_file_memory
            from .state import file_sha256
            save_file_memory(
                file_sha256(target), str(target),
                summary="段落 %d / 表格 %d" % (state["paragraphs"], len(state["tables"])),
                keywords="、".join(
                    sorted(state["styles"], key=lambda k: -state["styles"][k])[:5]),
            )
        except Exception:  # noqa: BLE001
            pass
        # 即时记忆（4.4）：记住"最近一次任务"，供多轮/追问
        try:
            from .session import remember
            remember({"file": str(target), "tier": tier,
                      "user_input": text, "status": result.get("status")})
        except Exception:  # noqa: BLE001
            pass
        # dry-run / 回滚：**独立处理**
        if request.get("rollback"):
            bak = target.with_suffix(target.suffix + ".bak")
            if bak.exists():
                shutil.copy2(bak, target)
            elif bak2 is not None and bak2.exists():
                shutil.copy2(bak2, target)
            else:
                return _finish(_fail("rollback_failed", "没有备份（.bak 与独立备份都没有），无法回滚。"),
                               stages, warnings=warnings)
            result["rollback"] = True
            result["applied"] = 0
        elif request.get("dry_run"):
            result["dry_run"] = True
            result["applied"] = 0
        # 真 dry-run：删临时副本
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        return _finish(result, stages, warnings=warnings)


def _memory_related_max() -> int:
    """记忆上下文条数上限：**契约**（不写死）；缺失回退 3。"""
    try:
        from pipeline.synth_contract.registry import load_contract
        n = (load_contract().data.get("word_office") or {}).get("memory_related_max", 3)
        return max(0, int(n or 0))
    except Exception:  # noqa: BLE001
        return 3


def _memory_blob(related) -> str:
    """拼接 related → "摘要（关键词）"；**条数**读契约，**长度**受 prompt 预算约束。"""
    if not related:
        return ""
    picked = list(related)[:_memory_related_max()]
    text = "；".join(
        "%s（%s）" % (r.get("summary") or r.get("hash", "")[:8],
                     r.get("keywords") or "")
        for r in picked)
    try:
        from ai.context_budget import effective_input_chars
        cap = int(effective_input_chars(reserved_output=600) * 0.2)  # 记忆段最多占 20%
        return text[:cap]
    except Exception:  # noqa: BLE001
        return text


def _portrait(target: Path, state: dict, limit: int = 30, related=None):
    """文档画像：**返回 `IntermediateArtifact`**（kind=portrait，payload.text）。"""
    from .objects import IntermediateArtifact

    doc = Document(str(target))
    paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()][:limit]
    heads = [k for k in state["styles"] if "Heading" in k or k == "Title"]
    base = "段落数 %d；标题样式：%s\n正文前 %d 段：\n%s" % (
        state["paragraphs"], "、".join(heads) or "无", len(paras),
        "\n".join("- " + p[:200] for p in paras))
    blob = _memory_blob(related)
    if blob:
        base += "\n历史相关文件：" + blob
    return IntermediateArtifact(kind="portrait", ref=str(target),
                                payload={"text": base})


def _exec_l1(target: Path, state: dict, preds: dict, user_input: str,
             related=None) -> dict:
    """L1：AI 只出 {summary, ops:[{find,replace}]}；本地白名单 + 备份 + 结构校验 + 内容对账。"""
    api_key = config.get_api_key()
    if not api_key:
        return _fail("no_key", "L1 需要 API Key，请先在设置里配置。")
    from .ai import call_l1

    data = call_l1(user_input, _portrait(target, state, related=related).payload["text"], api_key)
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


_STATUS = (
    "done", "failed", "partially_completed",
    "plan_invalidated", "failed_postcheck",
    "compensation_failed", "rollback_failed",
    "needs_confirmation",   # 危险操作/低置信，等用户确认
)

_REASON_STATUS = {
    "pre_check": "plan_invalidated",
    "plan_invalidated": "plan_invalidated",
    "verify_failed": "failed_postcheck",
    "failed_postcheck": "failed_postcheck",
    "compensation_failed": "compensation_failed",
    "rollback_failed": "rollback_failed",
}


def _fail(reason: str, message: str) -> dict:
    """失败：**按 reason 映射到七状态机**（默认 `failed`）。"""
    status = _REASON_STATUS.get(reason, "failed")
    return {"status": status, "reason": reason, "message": message,
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
    # ToolRegistry 分派（新增 op 只改 objects.ToolRegistry，不改这里）
    from .objects import ToolRegistry
    import sys as _sys
    name = ToolRegistry().tools.get(str(op["op"]))
    if name and name != "_execute":
        fn = getattr(_sys.modules[__name__], name, None)
        if fn is not None:
            return fn(target, op)

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
            "applied": n_run + n_para,
            "plan_ref": None, "audit_ref": None, "user_actions": []}


def _exec_insert(target: Path, op: dict) -> dict:
    """insert：找到含锚点的**段落**。

    - **锚点是标题**（Heading/Title）→ 段后**插新段**（段级）。
    - **否则** → 锚点文字**后**插**内联文字**（run 级，保留锚点格式）。
    """
    from docx.oxml.ns import qn

    anchor = str(op.get("anchor") or "").strip()
    content = str(op.get("content") or "").strip()
    if not anchor or not content:
        return _fail("bad_plan", "insert 缺少锚点或内容。")
    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    doc = Document(str(target))
    n_para = 0
    n_text = 0
    for p in _iter_paragraphs(doc):
        if anchor not in p.text:
            continue
        style = p.style.name if p.style is not None else ""
        if style.startswith("Heading") or style == "Title":
            new_p = doc.add_paragraph(content)
            try:
                new_p.style = p.style
            except Exception:  # noqa: BLE001
                pass
            p._p.addnext(new_p._p)
            n_para += 1
            continue
        for mid in _split_runs_at(p, anchor):
            rpr = mid.find(qn("w:rPr"))
            mid.addnext(_new_run(content, rpr))
            n_text += 1
    if n_para == 0 and n_text == 0:
        return _fail("not_found", "文档里没找到锚点「%s」。" % anchor)
    msg = ("✅ 在「%s」后插入 %d 段（写前备份：%s）。" % (anchor, n_para, backup.name)
           if n_para else
           "✅ 在「%s」后插入 %d 处文字（写前备份：%s）。" % (anchor, n_text, backup.name))
    doc.save(str(target))
    return {"status": "done", "reason": "insert", "message": msg,
            "applied": n_para + n_text,
            "plan_ref": None, "audit_ref": None, "user_actions": []}


def _new_run(text: str, rpr):
    """造一个 run（可带 `w:rPr` 副本，保留格式）。"""
    from copy import deepcopy
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    r = OxmlElement("w:r")
    if rpr is not None:
        r.append(deepcopy(rpr))
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    r.append(t)
    return r


def _split_runs_at(p, find: str):
    """拆出段落里**所有** `find` 的 run（保留 `w:rPr`）。

    - **同一 run 内**：拆 before/find/after。
    - **跨 run**：先合并全段（保留首 run 的 `w:rPr`），再拆（**跨 run 会丢段内格式**）。
    返回 **find 的 run 列表**。
    """
    from docx.oxml.ns import qn

    out = []
    done = set()   # 已拆过的 `w:r` 的 id（**不能比 Run 包装**——每次都是新对象）

    def _remaining():
        for r in p.runs:
            if id(r._r) in done:
                continue
            if find in r.text:
                return r
        return None

    guard = 0
    while guard < 50:
        guard += 1
        hit = _remaining()
        if hit is None:
            # 已有 find run（在 out 里）→ 不再合并
            if any(find in r.text for r in p.runs):
                break
            # 跨 run：合并全段再试
            full = p.text
            if find not in full or not p.runs:
                break
            first = p.runs[0]
            first.text = full
            for r in list(p.runs)[1:]:
                r._r.getparent().remove(r._r)
            continue
        idx = hit.text.index(find)
        before, after = hit.text[:idx], hit.text[idx + len(find):]
        rpr = hit._r.find(qn("w:rPr"))
        hit.text = before
        done.add(id(hit._r))
        mid = _new_run(find, rpr)
        tail = _new_run(after, rpr)
        hit._r.addnext(mid)
        mid.addnext(tail)
        done.add(id(mid))
        out.append(mid)
    return out


def _exec_delete(target: Path, op: dict) -> dict:
    """delete：只删**目标文本**（run 级）；整段删空才删段。"""
    find = str(op.get("find") or "").strip()
    if not find:
        return _fail("bad_plan", "delete 缺少目标文本。")
    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    doc = Document(str(target))
    n = 0
    for p in list(_iter_paragraphs(doc)):
        orig = p.text
        if find not in orig:
            continue
        n += orig.count(find)
        mids = _split_runs_at(p, find)
        for mid in mids:
            mid.getparent().remove(mid)
        if not p.text.strip():
            p._p.getparent().remove(p._p)
    if n == 0:
        return _fail("not_found", "文档里没找到「%s」。" % find)
    doc.save(str(target))
    return {"status": "done", "reason": "delete",
            "message": "✅ 删除 %d 处「%s」（只删文字，不删段；写前备份：%s）。"
                       % (n, find, backup.name),
            "applied": n,
            "plan_ref": None, "audit_ref": None, "user_actions": []}


def _exec_outline_to_table(target: Path, op: dict) -> dict:
    """**跨结构（大纲→表）**：把 `find` 之后的标题 → 2 列表格（标题 + 层级）。"""
    find = str(op.get("find") or "").strip()
    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    doc = Document(str(target))
    paras = list(doc.paragraphs)
    start = -1
    if find:
        for i, p in enumerate(paras):
            if find in p.text:
                start = i
                break
        if start < 0:
            return _fail("not_found", "文档里没找到「%s」。" % find)
    rows = []
    heading_els = []
    for p in paras[start + 1:]:
        name = p.style.name or ""
        if name.startswith("Heading"):
            try:
                level = int(name.split()[-1])
            except (ValueError, IndexError):
                level = 1
            rows.append((p.text.strip(), str(level)))
            heading_els.append(p._p)
        elif not p.text.strip():
            continue
    if not rows:
        return _fail("bad_plan", ("「%s」后面没有标题。" % find) if find else "文档里没有标题。")
    tbl = doc.add_table(rows=len(rows), cols=2)
    for i, (title, level) in enumerate(rows):
        tbl.cell(i, 0).text = title
        tbl.cell(i, 1).text = level  # 数字层级（可回表 round-trip）
    anchor_el = paras[start]._p if start >= 0 else paras[0]._p
    anchor_el.addnext(tbl._tbl)
    # **搬走 = 显式 opt-in**（`remove_source` 才删标题；默认保留 = 安全）
    remove = bool(op.get("remove_source"))
    if remove:
        for el in heading_els:
            el.getparent().remove(el)
    doc.save(str(target))
    action = "已删除原标题" if remove else "原标题已保留"
    return {"status": "done", "reason": "outline_to_table",
            "message": "✅ 已把 %d 个标题转成表格（第二列为层级；%s；写前备份：%s）。"
                       % (len(rows), action, backup.name),
            "applied": len(rows),
            "plan_ref": None, "audit_ref": None, "user_actions": []}


def _exec_table_to_outline(target: Path, op: dict) -> dict:
    """**跨结构（表→大纲）**：表格**首列**→`Heading level`，**其余列→正文段**；表格删除。"""
    find = str(op.get("find") or "").strip()
    level = int(op.get("level") or 1)
    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    doc = Document(str(target))
    picked = []
    for t in doc.tables:
        if not find or any(find in c.text for row in t.rows for c in row.cells):
            picked.append(t)
            if find:
                break
    if not picked:
        picked = list(doc.tables)
    if not picked:
        return _fail("not_found", "没找到表格。")
    n = 0
    for t in picked:
        for row in t.rows:
            cells = [c.text.strip() for c in row.cells]
            first = cells[0] if cells else ""
            if not first:
                continue
            p = doc.add_paragraph(first)
            try:
                p.style = doc.styles["Heading %d" % level]
            except Exception:  # noqa: BLE001
                pass
            t._tbl.addprevious(p._p)
            n += 1
            rest = cells[1:]
            if rest:
                bp = doc.add_paragraph("　".join(rest))
                t._tbl.addprevious(bp._p)
        t._tbl.getparent().remove(t._tbl)
    doc.save(str(target))
    return {"status": "done", "reason": "table_to_outline",
            "message": "✅ 已把 %d 个表格的首列转成 %d 个标题（其余列→正文；写前备份：%s）。"
                       % (len(picked), n, backup.name),
            "applied": n,
            "plan_ref": None, "audit_ref": None, "user_actions": []}


def _exec_text_to_table(target: Path, op: dict) -> dict:
    """**跨结构（叙述→表）**：把 `find` 之后连续的 `键:值` 段落转成 2 列表格。

    支持分隔符 `：` / `:` / `=`；**跳过空行**；遇到既非空又非键值对的段落才停。
    """
    find = str(op.get("find") or "").strip()
    if not find:
        return _fail("bad_plan", "text_to_table 缺少目标。")
    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    doc = Document(str(target))
    paras = list(doc.paragraphs)
    start = None
    for i, p in enumerate(paras):
        if find in p.text:
            start = i
            break
    if start is None:
        return _fail("not_found", "文档里没找到「%s」。" % find)
    rows = []
    body = paras[start]._p.getparent()
    anchor = paras[start]._p
    for p in paras[start + 1:]:
        t = p.text.strip()
        if not t:
            continue  # 空行跳过，不中断
        kv = None
        for sep in ("：", ":", "="):
            if sep in t:
                k, v = t.split(sep, 1)
                kv = (k.strip(), v.strip())
                break
        if kv is None:
            break  # 既非空又非键值对 → 列表结束
        rows.append(kv)
        body.remove(p._p)
    if not rows:
        return _fail("bad_plan", "「%s」后面没有可转成表的「键:值」段落。" % find)
    tbl = doc.add_table(rows=len(rows), cols=2)
    for i, (k, v) in enumerate(rows):
        tbl.cell(i, 0).text = k
        tbl.cell(i, 1).text = v
    anchor.addnext(tbl._tbl)
    doc.save(str(target))
    return {"status": "done", "reason": "text_to_table",
            "message": "✅ 已把 %d 段文字转成表格（写前备份：%s）。"
                       % (len(rows), backup.name),
            "applied": len(rows),
            "plan_ref": None, "audit_ref": None, "user_actions": []}


def _exec_table_to_text(target: Path, op: dict) -> dict:
    """**跨结构（表→叙述）**：把表格逐行转成段落文字（表格本身删除）。"""
    find = str(op.get("find") or "").strip()
    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    doc = Document(str(target))
    picked = []
    for t in doc.tables:
        if not find or any(find in c.text for row in t.rows for c in row.cells):
            picked.append(t)
            if find:
                break
    note = ""
    if not picked and find:
        # **表标题（caption）匹配**：向前找最多 5 段，隔一段也能定位
        for t in doc.tables:
            node = t._tbl.getprevious()
            for _ in range(5):
                if node is None or node.tag != qn("w:p"):
                    break
                txt = "".join(x.text or "" for x in node.iter(qn("w:t")))
                if find in txt:
                    picked.append(t)
                    break
                node = node.getprevious()
            if picked:
                break
        if picked:
            note = "（按表标题「%s」定位）" % find
    if not picked:
        picked = list(doc.tables)
        note = ("（没在格子里找到「%s」，已转全部表格）" % find) if find else ""
    if not picked:
        return _fail("not_found", "没找到符合条件的表格。")
    n = 0
    for t in picked:
        lines = []
        for row in t.rows:
            cells = [c.text.strip() for c in row.cells]
            cells = [c for i, c in enumerate(cells) if i == 0 or c != cells[i - 1]]
            line = "：".join([cells[0]] + cells[1:]) if len(cells) > 1 else (cells[0] if cells else "")
            if line:
                lines.append(line)
        # 段落插到表格原位置（`addprevious`，顺序不反），再删表
        for line in lines:
            p = doc.add_paragraph(line)
            t._tbl.addprevious(p._p)
            n += 1
        t._tbl.getparent().remove(t._tbl)
    doc.save(str(target))
    return {"status": "done", "reason": "table_to_text",
            "message": "✅ 已把 %d 个表格转成 %d 段文字%s（写前备份：%s）。"
                       % (len(picked), n, note, backup.name),
            "applied": n,
            "plan_ref": None, "audit_ref": None, "user_actions": []}


def _exec_outline(target: Path, op: dict) -> dict:
    """outline：**改标题层级**（`Heading N`）或**降为正文**（`level=0`）。"""
    find = str(op.get("find") or "").strip()
    level = int(op.get("level") or 0)
    if not find:
        return _fail("bad_plan", "outline 缺少目标文本。")
    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    doc = Document(str(target))
    n = 0
    for p in _iter_paragraphs(doc):
        if find not in p.text:
            continue
        try:
            p.style = doc.styles["Heading %d" % level] if level > 0 else doc.styles["Normal"]
            n += 1
        except Exception:  # noqa: BLE001
            continue
    if n == 0:
        return _fail("not_found", "文档里没找到「%s」。" % find)
    doc.save(str(target))
    label = ("%d 级标题" % level) if level > 0 else "正文"
    note = ""
    if level > 0 and re.search(r"\d+\s*[.、．]\s*\d+", find):
        note = "（**编号可能需手工核对**）"
    return {"status": "done", "reason": "outline",
            "message": "✅ 已把含「%s」的 %d 段设为%s%s（写前备份：%s）。"
                       % (find, n, label, note, backup.name),
            "applied": n,
            "plan_ref": None, "audit_ref": None, "user_actions": []}


def _exec_format(target: Path, op: dict) -> dict:
    """format：对**目标文字**设格式（**run 级**，支持跨 run、字号、字体）。"""
    from docx.shared import Pt
    from docx.text.run import Run

    find = str(op.get("find") or "").strip()
    mode = str(op.get("mode") or "bold")
    size = op.get("size")
    font = str(op.get("font") or "").strip()
    color = str(op.get("color") or "").strip()
    if not find:
        return _fail("bad_plan", "format 缺少目标文本。")
    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    doc = Document(str(target))
    n = 0
    for p in _iter_paragraphs(doc):
        if find not in p.text:
            continue
        for mid in _split_runs_at(p, find):
            run = Run(mid, p)
            if mode == "italic":
                run.italic = True
            elif mode == "font":
                if font:
                    run.font.name = font
            elif mode == "size":
                if size:
                    run.font.size = Pt(int(size))
            elif mode == "color":
                if color:
                    from docx.shared import RGBColor
                    rgb = {
                        "红": RGBColor(0xC0, 0x00, 0x00),
                        "蓝": RGBColor(0x00, 0x00, 0xC0),
                        "绿": RGBColor(0x00, 0x70, 0x30),
                        "黑": RGBColor(0x00, 0x00, 0x00),
                        "黄": RGBColor(0xC0, 0x80, 0x00),
                    }.get(color)
                    if rgb is not None:
                        run.font.color.rgb = rgb
            else:
                run.bold = True
            n += 1
    if n == 0:
        return _fail("not_found", "文档里没找到「%s」。" % find)
    doc.save(str(target))
    if mode == "italic":
        label = "斜体"
    elif mode == "font":
        label = "字体「%s」" % font
    elif mode == "size":
        label = "字号 %s" % size
    else:
        label = "粗体"
    return {"status": "done", "reason": "format",
            "message": "✅ 已把「%s」的 %d 个文本块设为%s（写前备份：%s）。"
                       % (find, n, label, backup.name),
            "applied": n,
            "plan_ref": None, "audit_ref": None, "user_actions": []}


def _exec_move(target: Path, op: dict) -> dict:
    """move：**run 级**——把含 `src` 的那段文字移到含 `dst` 的那段文字后（或前）。"""
    src = str(op.get("src") or "").strip()
    dst = str(op.get("dst") or "").strip()
    before = bool(op.get("before"))
    if not src or not dst:
        return _fail("bad_plan", "move 缺少源或目标。")
    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    doc = Document(str(target))
    src_el = dst_el = None
    for p in _iter_paragraphs(doc):
        if src_el is None and src in p.text:
            mids = _split_runs_at(p, src)
            if mids:
                src_el = mids[0]
        if dst_el is None and dst in p.text:
            mids = _split_runs_at(p, dst)
            if mids:
                dst_el = mids[0]
    if src_el is None:
        return _fail("not_found", "文档里没找到「%s」。" % src)
    if dst_el is None:
        return _fail("not_found", "文档里没找到目标「%s」。" % dst)
    if src_el is dst_el:
        return _fail("bad_plan", "源和目标同一处，无法移动。")
    # **带标点**：src 后紧跟的标点 run 一起移
    tail = None
    nxt = src_el.getnext()
    if nxt is not None and nxt.tag == qn("w:r"):
        txt = "".join(x.text or "" for x in nxt.findall(qn("w:t")))
        if txt and txt[0] in "，。；！？,.;!?":
            tail = nxt
    src_el.getparent().remove(src_el)
    if before:
        dst_el.addprevious(src_el)
    else:
        dst_el.addnext(src_el)
    if tail is not None:
        tail.getparent().remove(tail)
        src_el.addnext(tail)
    doc.save(str(target))
    return {"status": "done", "reason": "move",
            "message": "✅ 已把「%s」移到「%s」%s（run 级；写前备份：%s）。"
                       % (src, dst, "前面" if before else "后面", backup.name),
            "applied": 1,
            "plan_ref": None, "audit_ref": None, "user_actions": []}

def _exec_tracked_change(target: Path, op: dict) -> dict:
    """tracked_change：**run 级**修订——只把 `find` 那段包进 `w:del`/`w:ins`，前后文保留。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    find = str(op.get("find") or "").strip()
    repl = str(op.get("replace") or "").strip()
    if not find or find == repl:
        return _fail("bad_plan", "tracked_change 参数不完整。")
    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    doc = Document(str(target))
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _plain_run(text: str, pid: str):
        if not text:
            return None
        r = OxmlElement("w:r")
        t = OxmlElement("w:t")
        t.set(qn("xml:space"), "preserve")
        t.text = text
        r.append(t)
        return r

    n = 0
    for p in _iter_paragraphs(doc):
        orig = p.text
        if find not in orig:
            continue
        n += 1
        mids = _split_runs_at(p, find)
        for mid in mids:
            del_el = OxmlElement("w:del")
            del_el.set(qn("w:id"), str(9000 + n))
            del_el.set(qn("w:author"), "FileMate")
            del_el.set(qn("w:date"), ts)
            mid.addprevious(del_el)
            del_el.append(mid)
            # `w:del` 里的文字必须是 `w:delText`
            for t in mid.findall(qn("w:t")):
                t.tag = qn("w:delText")
            ins_el = OxmlElement("w:ins")
            ins_el.set(qn("w:id"), str(9500 + n))
            ins_el.set(qn("w:author"), "FileMate")
            ins_el.set(qn("w:date"), ts)
            ins_run = OxmlElement("w:r")
            ins_text = OxmlElement("w:t")
            ins_text.set(qn("xml:space"), "preserve")
            ins_text.text = repl
            ins_run.append(ins_text)
            ins_el.append(ins_run)
            del_el.addnext(ins_el)
            n += 1
    if n == 0:
        return _fail("not_found", "文档里没找到「%s」。" % find)
    doc.save(str(target))
    return {"status": "done", "reason": "tracked_change",
            "message": "✅ 已以**修订**方式改 %d 段（原文保留在删除痕迹里；写前备份：%s）。"
                       % (n, backup.name),
            "applied": n,
            "plan_ref": None, "audit_ref": None, "user_actions": []}
