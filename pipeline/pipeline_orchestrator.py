# pipeline/pipeline_orchestrator.py
"""
四层管线编排：单文件从"磁盘原始内容"到"统一 JSON"的完整流程。
Stage1 解析 → Stage2 判别（带缓存）→ Stage3 分块+AI抽取 → Stage4 归一化
表格文件（CSV/XLSX）走表格支线：画像 → 缓存 → 直接产出统一 JSON（tables 字段）。
文本文件走完整支线，AI 抽取后由复核层打回重跑（轮数由 review.rounds 参数控制）。
"""
import config  # 可调参数统一从 config.json 的 params 段读取（严禁写死）

from pipeline.stage1_parser import file_parser
from pipeline.stage2_classifier import doc_classifier
from pipeline.stage3_processor import text_splitter, ai_caller, table_engine
from pipeline.stage4_normalizer import normalizer
from infra import cache_db


def run_pipeline(file_path, api_key: str, progress=None) -> dict:
    """
    处理单个文件。
    progress: 可选回调 fn(msg)，把阶段进度报给上层 UI。
    返回 {
        "ok": bool,
        "message": str,               # 给用户看的简述（失败时是原因）
        "unified": dict|None,         # 统一 JSON
        "chunk_results": [...],       # 块级明细（含每块原文 content，供落库/报告）
        "table": dict|None,           # 表格支线产出的原表
        "profiles": {...},            # 表格列画像
        "doc_type": str,
    }
    """

    def report(msg):
        if progress:
            progress(msg)

    cache_db.init_cache_tables()  # 幂等：确保 doc_type_cache / table_profile 等缓存表存在

    # ---- Stage1 解析 ----
    parsed = file_parser.parse_file(file_path)
    if parsed["kind"] == "unsupported":
        return {"ok": False, "message": f"跳过 {parsed['filename']}：{parsed['note']}",
                "unified": None, "chunk_results": [], "table": None,
                "profiles": {}, "doc_type": ""}

    # ---- 表格支线 ----
    if parsed["kind"] == "table":
        return _run_table_branch(parsed, report)

    # ---- 文本支线 ----
    text = parsed["text"]
    if len(text.strip()) < 30:
        return {"ok": False, "message": f"跳过 {parsed['filename']}：有效内容太少（不足 30 字）",
                "unified": None, "chunk_results": [], "table": None,
                "profiles": {}, "doc_type": ""}

    # ---- Stage2 判别（带缓存：内容不变直接复用上次结果） ----
    cached = cache_db.get_doc_type(parsed["hash"])
    if cached:
        doc_type, scores = cached["doc_type"], cached["scores"]
        report(f"🧭 {parsed['filename']} 命中判别缓存：{doc_type}")
    else:
        doc_type, scores = doc_classifier.classify(text)
        cache_db.set_doc_type(parsed["hash"], doc_type, scores)
        report(f"🧭 {parsed['filename']} 判别为「{doc_type}」文档")

    # ---- Stage3a 分块 ----
    chunks = text_splitter.split(text, doc_type)
    report(f"✂️ {parsed['filename']} 切成 {len(chunks)} 块，开始 AI 结构化抽取…")

    # ---- Stage3b AI 抽取（块级，失败自动重试降级在 ai_caller 内部） ----
    chunk_results = []
    for i, c in enumerate(chunks, 1):
        report(f"   ({i}/{len(chunks)}) 抽取 {c['chunk_id']} · {c['section_path']}")
        r = ai_caller.call_ai_json(c, doc_type, api_key)
        chunk_results.append({**r, **{k: c[k] for k in ("start_offset", "end_offset")}})
        chunk_results[-1]["content"] = c["content"]  # 原文带回，供落库与报告

    # ---- 复核闭环（规划→执行→检查→读报错→自我修正→再验证） ----
    # 每轮先"检查"找出非 ok 块，把具体报错读出来上报，再只对问题块"自我修正"，
    # 修正后立即"再验证"；3 轮仍有问题则带伤前进（降级/失败块在结果中明确标注）。
    from agent_core.reviewer import review  # 延迟导入避免包初始化顺序问题
    rounds = int(config.get_param("review.rounds"))  # 自我修正轮数，参数可调
    for round_no in range(1, rounds + 1):
        bad = [r for r in chunk_results if r["status"] != "ok"]  # 检查
        if not bad:
            if round_no > 1:
                report("✅ 再验证通过：所有块状态 ok")  # 修正生效的证据
            break
        # 读报错：把问题块的具体原因带上（截断展示前 3 个）
        reasons = "; ".join(
            f"{r['chunk_id']}:{(r.get('error') or r['status'])[:50]}"
            for r in bad[:3])
        report(f"🔍 检查发现 {len(bad)} 个问题块（{reasons}）→ 第 {round_no}/{rounds} 轮自我修正")
        for r in bad:  # 只重跑问题块，已成功的块不浪费调用
            origin = next(c for c in chunks if c["chunk_id"] == r["chunk_id"])
            retry = ai_caller.call_ai_json(origin, doc_type, api_key)
            retry["content"] = origin["content"]
            retry["start_offset"] = origin["start_offset"]
            retry["end_offset"] = origin["end_offset"]
            chunk_results[chunk_results.index(r)] = retry
    still_bad = [r for r in chunk_results if r["status"] != "ok"]
    if still_bad:
        report(f"⚠️ {rounds} 轮修正后仍有 {len(still_bad)} 个问题块，结果中已标注原因，绝不静默")

    # ---- Stage4 归一化 ----
    unified = normalizer.normalize(
        chunk_results, doc_type, parsed["filename"], parsed["hash"],
        extra_meta={"classify_scores": scores, "path": parsed["path"]},
    )
    degraded_with_summary = any(r["status"] == "degraded" and r.get("summary")
                                for r in chunk_results)
    if degraded_with_summary:
        unified["_has_degraded_summary"] = True  # 告诉复核层：有摘要兜底不算空结果

    # ---- 终审：对统一 JSON 做结构/完整性校验，问题随结果带回 ----
    ok, issues = review(unified)
    if not ok:
        unified["_review_issues"] = issues
        report("⚠️ 终审未完全通过：" + "；".join(issues))

    n_fail = unified["document_info"]["failed_chunks"]
    n_deg = unified["document_info"]["degraded_chunks"]
    status_line = "全部块抽取成功" if n_fail == n_deg == 0 else \
        f"{n_fail} 块失败、{n_deg} 块降级（结果中已标注，不静默）"
    report(f"🧩 {parsed['filename']} 整理完成：{status_line}")

    return {"ok": True, "message": status_line, "unified": unified,
            "chunk_results": chunk_results, "table": None,
            "profiles": {}, "doc_type": doc_type}


def _run_table_branch(parsed: dict, report) -> dict:
    """CSV/XLSX 支线：画像缓存 + 统一 JSON（tables 字段承载原表）"""
    table = parsed["table"]
    table["name"] = parsed["filename"]  # 表名用文件名，联查/落库时好认
    profile = table_engine.profile_table(table)
    cached = cache_db.get_table_profile(parsed["path"], parsed["mtime"])
    if not cached:  # 画像落缓存，同版本文件不重复计算
        cache_db.set_table_profile(parsed["path"], parsed["mtime"], profile)

    unified = normalizer.normalize(
        [], "technical", parsed["filename"], parsed["hash"],  # 表格无文本可判别，类型记 technical
        extra_meta={"path": parsed["path"], "columns": table["columns"],
                    "row_count": len(table["rows"]), "profile": profile},
    )
    unified["structured_content"]["tables"].append(
        {"name": table["name"], "columns": table["columns"], "rows": table["rows"]})
    unified["database_ready"] = True
    report(f"📊 {parsed['filename']}：{len(table['rows'])} 行 × {len(table['columns'])} 列")

    return {"ok": True, "message": f"{len(table['rows'])} 行 × {len(table['columns'])} 列",
            "unified": unified, "chunk_results": [], "table": table,
            "profiles": profile, "doc_type": "table"}
