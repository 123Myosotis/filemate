# agent_core/reviewer.py
"""
复核门禁：有 API 时由模型根据证据裁定 pass / retry / degrade；
本地 validator 只收集可核验事实，不单独决定打回。

无 API 时退回本地证据门禁（文件是否生成等物理事实）。
"""
from pipeline.stage2_classifier.doc_classifier import DOC_TYPES
from agent_core import validator


def review(unified: dict) -> tuple:
    """复核统一 JSON。返回 (ok, issues)。"""
    issues = []
    if not isinstance(unified, dict) or "document_info" not in unified:
        return False, ["文档结构不完整，缺少基本信息"]

    info = unified["document_info"]
    for field in ("type", "filename", "total_chunks", "content_hash"):
        if not info.get(field) and info.get(field) != 0:
            issues.append(f"文档信息缺少必填项：{field}")

    if info.get("type") not in DOC_TYPES:
        issues.append(f"文档类型「{info.get('type')}」不在支持范围内，应为 {','.join(DOC_TYPES)}")

    sc = unified.get("structured_content") or {}
    chapters = sc.get("chapters", [])
    chunk_count = sum(len(ch.get("chunk_ids", [])) for ch in chapters)
    if chunk_count != info.get("total_chunks", -1):
        issues.append(f"段落计数不一致：实际 {chunk_count} 段，记录 {info.get('total_chunks')} 段")
    if info.get("total_chunks", 0) > 0 and info.get("failed_chunks", 0) == info["total_chunks"]:
        issues.append("所有段落都提取失败，结果不可用，需要重新处理")

    if chunk_count > 0 and not (sc.get("global_entities") or sc.get("facts") or sc.get("tables")):
        has_summary = unified.get("_has_degraded_summary", False)
        if not has_summary:
            issues.append("未能从文档中提取到任何实体、事实或表格")

    return (len(issues) == 0), issues


def review_execution(user_input: str, summary: dict, api_key: str = "",
                     intent: dict = None, ctx: dict = None) -> dict:
    """
    统一复核入口。
    - 本地 validator：只产出 evidence / candidate issues（事实清单）
    - 有 api_key：由模型根据用户指令 + 证据裁定 action；硬门禁跟模型返回值走
    - 指令冲突：不据此回滚，留给用户确认
    """
    import config
    intent = intent or (ctx or {}).get("intent") or {}
    if ctx is not None:
        evidence_gate = validator.validate_execution(user_input, intent, ctx)
    else:
        evidence_gate = _summary_fallback(summary)

    conflicts = ((ctx or {}).get("data") or {}).get("instruction_conflicts") or []
    if conflicts:
        # 冲突留给用户确认，不因冲突打回
        out = dict(evidence_gate)
        out["action"] = "pass"
        out["passed"] = True
        out.setdefault("assist_notes", []).append(
            "指令自身有冲突，已留给用户确认是否保留结果，不据此回滚重跑")
        out["issues"] = [
            i for i in (out.get("issues") or [])
            if i.get("code") != "ai_assist"
        ]
        return out

    if api_key and ctx is not None and config.get_param("review.ai_gate", True):
        decided = _ai_decide_gate(
            user_input, summary, evidence_gate, api_key, ctx)
        if decided:
            return decided

    # 无 API / 模型未返回：仅保留物理交付类 high（缺文件、空表），其余不打回
    return _physical_only_gate(evidence_gate)


def _physical_only_gate(gate: dict) -> dict:
    """无模型时：只拦「根本没有交付物」这类物理事实。"""
    physical = {
        "missing_excel", "missing_csv", "missing_word", "unsupported_ppt",
        "file_missing", "file_empty", "no_output", "empty_table",
    }
    issues = [
        i for i in (gate.get("issues") or [])
        if i.get("code") in physical and i.get("severity") == "high"
    ]
    high = bool(issues)
    return {
        "passed": not high,
        "failed_dimensions": list(dict.fromkeys(i["dimension"] for i in issues)),
        "issues": issues,
        "action": "retry" if high else "pass",
        "evidence": gate.get("evidence") or {},
        "assist_notes": list(gate.get("assist_notes") or []) + [
            "无模型裁定，仅按物理交付事实门禁"
        ],
    }


def _ai_decide_gate(user_input: str, summary: dict, evidence_gate: dict,
                    api_key: str, ctx: dict) -> dict:
    """模型看证据后裁定是否打回。本地候选问题只作参考，不直接决定。"""
    import json as _json
    from ai.client import chat_json
    from ai.context_budget import effective_input_chars

    table = (ctx.get("data") or {}).get("table_result") or {}
    cols = list(table.get("columns") or [])
    rows = list(table.get("rows") or [])
    sample = rows[:12]
    notes = list((ctx.get("data") or {}).get("filter_notes") or [])
    notes += list((ctx.get("data") or {}).get("degrade_notes") or [])
    candidate = evidence_gate.get("issues") or []
    evidence = {
        "report_paths": list((ctx.get("data") or {}).get("report_paths") or []),
        "table_columns": cols,
        "table_row_count": len(rows),
        "table_sample_rows": sample,
        "synth_notes_tail": notes[-40:],
        "local_candidate_issues": candidate,
        "execution_summary": summary,
    }
    try:
        blob = _json.dumps(evidence, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = str(evidence)
    budget = effective_input_chars(reserved_output=600)
    blob = blob[:budget]

    prompt = f"""你是表格交付质检官。根据用户指令与执行证据，裁定是否接受本次结果。
本地「候选问题」只是线索，你必须自己根据样例行、列名、笔记判断是否真有错。
不要因为摘要抽样不完整就判 high。指令里自相矛盾的要求不算交付失败。

action 取值：
- pass：结果可用，或问题可接受
- retry：必须回滚重跑（列绑错、该联的表没联上、汇总粒错误、整列空等）
- degrade：有瑕疵但可交付

只输出 JSON：
{{"action":"pass|retry|degrade","passed":true/false,
"issues":[{{"dimension":"数据准确性|需求符合度|关联正确性|格式完整性",
"description":"具体问题","severity":"high|medium|low","code":"short_code"}}],
"notes":"一句话理由"}}

【用户指令】
{(user_input or "")[:1200]}

【执行证据】
{blob}
"""
    data = chat_json(
        prompt, api_key,
        max_tokens=900,
        temperature=0.1,
        system="只输出合法 JSON。",
    )
    if not isinstance(data, dict):
        return None
    action = str(data.get("action") or "").strip().lower()
    if action not in ("pass", "retry", "degrade"):
        return None
    issues = []
    for iss in data.get("issues") or []:
        if not isinstance(iss, dict) or not iss.get("description"):
            continue
        sev = iss.get("severity") if iss.get("severity") in (
            "high", "medium", "low") else "medium"
        issues.append({
            "dimension": str(iss.get("dimension") or "需求符合度"),
            "description": str(iss["description"]),
            "severity": sev,
            "code": str(iss.get("code") or "ai_gate"),
        })
    passed = bool(data.get("passed")) if "passed" in data else (action == "pass")
    if action == "retry":
        passed = False
    elif action == "pass":
        passed = True
    import config
    if config.get_param("review.hard_gate") and action == "retry":
        passed = False
    return {
        "passed": passed,
        "failed_dimensions": list(dict.fromkeys(i["dimension"] for i in issues)),
        "issues": issues,
        "action": action,
        "evidence": evidence_gate.get("evidence") or {},
        "assist_notes": [str(data.get("notes") or "模型裁定复核")],
    }


def _summary_fallback(summary: dict) -> dict:
    issues = []
    if not summary.get("parsed_ok") and not summary.get("moved") \
            and not summary.get("table_result") and not summary.get("reports"):
        issues.append({"dimension": "需求符合度",
                       "description": "本次没有产生任何有效结果",
                       "severity": "high", "code": "no_output"})
    table = summary.get("table_result")
    if table and not table.get("rows"):
        issues.append({"dimension": "需求符合度",
                       "description": "表格筛选后没有匹配到任何数据",
                       "severity": "high", "code": "zero_rows"})
    high = any(i["severity"] == "high" for i in issues)
    return {
        "passed": not issues,
        "failed_dimensions": list(dict.fromkeys(i["dimension"] for i in issues)),
        "issues": issues,
        "action": "pass" if not issues else ("retry" if high else "degrade"),
    }
