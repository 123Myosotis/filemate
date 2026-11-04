# agent_core/capabilities.py
"""
能力工具箱（Agent 的"手脚"）：把系统已有能力登记成统一接口，供任务编排层按需调用。

每个能力函数签名统一为 fn(ctx, task) -> {"ok": bool, "output": ..., "error": str}：
- ok=False 时 error 给出结构化原因，编排层据此跳过/重试/降级，绝不抛异常炸掉整条 DAG
- 能力之间通过 ctx["data"] 共享数据（scan→parsed→chunks→unified / table_result / report_paths）
- 失败不阻塞：聚合类能力（export/review/table_query）只消费 ctx.data 里已成功的产出

ctx 约定字段：
    user_input       用户原始指令
    work_dir         Path，工作目录
    api_key          智谱 API Key（可为空，能力内部自行降级）
    intent           8 维意图解析结果（operation_type/filter_conditions/sort_requirement/output_format...）
    moves            待执行的文件移动清单 [(src_path, dst_path)]（dispatcher 编译后放入）
    operations       撤回记录列表（file_organize 追加）
    report           进度回调 fn(msg)
    data             能力间共享数据字典
"""
import csv
import json
import re
import shutil
import time
from pathlib import Path

import config

from pipeline.stage1_parser import file_parser
from pipeline.stage2_classifier import doc_classifier
from pipeline.stage3_processor import text_splitter, ai_caller, table_engine, table_synth, table_normalizer, table_recover, semantic_match
from pipeline.stage4_normalizer import normalizer
from infra import cache_db
from storage import db_writer
from export import word_renderer

# 图片类扩展名（交给 vision 能力，与 fileops.type_words 的图片词表保持一致）
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}


def _report(ctx, msg):
    fn = ctx.get("report")
    if fn:
        fn(msg)


def _ok(output=None):
    return {"ok": True, "output": output, "error": "", "retryable": True}


def _fail(error, output=None, retryable=True):
    """结构化失败。retryable=False 表示不可重试错误（如数据不存在/参数无效），
    复核要求重跑时编排层会跳过此类任务，避免无效空转。"""
    return {"ok": False, "output": output, "error": str(error), "retryable": retryable}


# ------------------------------------------------------------------
# 1. 文件扫描能力
# ------------------------------------------------------------------

# 扫描时跳过的项目垃圾目录（避免 AI 被 .git/.venv 等无关文件淹没）
# 从配置注册表读取，便于集中管理


def _get_skip_dirs() -> set:
    import config as _cfg
    return set(_cfg.get_param("router.skip_dirs") or [])


def cap_scan(ctx, task):
    """扫描工作目录 → ctx.data["files"] = [相对路径...]；支持 task.input.exts 过滤。
    递归扫描子文件夹，用相对路径标识文件（与 dispatcher 传入规划层的 file_names 一致）。
    自动跳过 .git/.venv/node_modules 等项目垃圾目录，避免无关文件干扰。"""
    if ctx.get("data", {}).get("resume_from_tables") and ctx["data"].get("tables"):
        n = len(ctx["data"]["tables"])
        ctx["data"]["_skip_parse"] = True
        _report(ctx, f"📂 沿用检查点中的 {n} 张已解析表，跳过重新扫描")
        return _ok(list(ctx["data"].get("files") or []))
    work_dir = Path(ctx["work_dir"])
    skip = _get_skip_dirs()
    exts = None
    inp = task.get("input") or {}
    if isinstance(inp, dict) and inp.get("exts"):
        exts = {str(e).lower() if str(e).startswith(".") else "." + str(e).lower()
                for e in inp["exts"]}
    try:
        files = []
        for p in work_dir.rglob("*"):
            if not p.is_file() or p.name.startswith("."):
                continue
            # 跳过垃圾目录下的文件
            parts = p.relative_to(work_dir).parts
            if any(part in skip for part in parts):
                continue
            files.append(p)
        files.sort()
    except OSError as e:
        return _fail(f"目录扫描失败：{e}")
    if exts:
        files = [p for p in files if p.suffix.lower() in exts]
    ctx["data"]["files"] = [str(p.relative_to(work_dir)) for p in files]
    ctx["data"]["file_paths"] = {str(p.relative_to(work_dir)): p for p in files}
    _report(ctx, f"📂 扫描到 {len(files)} 个文件")
    return _ok([str(p.relative_to(work_dir)) for p in files])


# ------------------------------------------------------------------
# 2. 文件解析能力（txt/word/excel/ppt/pdf/csv；图片标记给 vision）
# ------------------------------------------------------------------

def cap_parse(ctx, task):
    """
    解析文件。task.input.file 指定单个文件（可并行多任务）；
    不指定则解析 scan 出的全部文件。表格类文件顺带完成画像+落库（不需要 AI）。
    parsed_store 以相对路径为键，与 file_paths 保持一致，避免子文件夹同名冲突。
    """
    if ctx.get("data", {}).get("_skip_parse") and ctx["data"].get("tables"):
        n = len(ctx["data"]["tables"])
        _report(ctx, f"📄 沿用检查点表格 {n} 张，跳过重新解析")
        return _ok({"parsed": "checkpoint", "tables": n})
    work_dir = Path(ctx["work_dir"])
    paths = _target_paths(ctx, task)
    if not paths:
        return _fail("没有可解析的文件（请先执行 scan 或检查目录内容）")
    parsed_store = ctx["data"].setdefault("parsed", {})
    results = []
    for p in paths:
        try:
            key = str(p.relative_to(work_dir))
        except ValueError:
            key = p.name
        if p.suffix.lower() in IMAGE_EXTS:
            parsed_store[key] = {"kind": "image", "filename": p.name,
                                 "path": str(p), "hash": "", "note": "图片文件"}
            results.append({"file": key, "kind": "image"})
            continue
        parsed = file_parser.parse_file(p)
        parsed_store[key] = parsed
        if parsed["kind"] == "table":  # 表格支线：画像 + 直接产出统一 JSON（无 AI 参与）
            extras = parsed.get("extra_tables") or []
            _finish_table(ctx, parsed)
            for extra in extras:
                # 多 sheet：每个工作表单独进 tables，键为 文件名::Sheet
                fake = dict(parsed)
                fake["filename"] = extra.get("name") or f"{parsed['filename']}"
                fake["table"] = extra
                _finish_table(ctx, fake)
        results.append({"file": key, "kind": parsed["kind"],
                        "note": parsed.get("note") or ""})
    bad = [r for r in results if r["kind"] == "unsupported"]
    out = {"parsed": results}
    if bad and len(bad) == len(results):
        return _fail("全部文件解析失败：" + "；".join(f"{r['file']}({r['note']})" for r in bad), out)
    _report(ctx, f"📄 解析完成 {len(results) - len(bad)}/{len(results)} 个文件")
    return _ok(out)


def _target_paths(ctx, task):
    """确定本任务要处理的具体文件路径：task.input.file 优先，否则 scan 结果全集。
    file_paths 的键是相对路径；若 AI 传了纯文件名，则按 basename 回退匹配。"""
    inp = task.get("input") or {}
    name = inp.get("file") if isinstance(inp, dict) else None
    file_paths = ctx["data"].get("file_paths") or {}
    if name:
        name = str(name)
        p = file_paths.get(name)
        if p:
            return [p]
        # 回退：按纯文件名匹配（AI 可能只写了文件名没写子目录）
        for key, path in file_paths.items():
            if Path(key).name == name or Path(key) == Path(name):
                return [path]
        return []
    return list(file_paths.values())


_PENDING_WASH_PATH = Path(__file__).resolve().parents[1] / "storage" / "pending_wash.json"


def write_pending_wash(file_path, table):
    """记下"等用户回答是否二次洗"（跨轮持久化）。"""
    try:
        _PENDING_WASH_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PENDING_WASH_PATH.write_text(
            json.dumps({"file": str(file_path), "table": str(table)}, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass


def read_pending_wash():
    try:
        return json.loads(_PENDING_WASH_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_pending_wash():
    try:
        _PENDING_WASH_PATH.unlink()
    except Exception:
        pass


def _ask_wash(ctx, tname, original, reason):
    """问用户表头是什么（本地静态五段式，不调 LLM）。"""
    ctx["data"].setdefault("table_needs_user", []).append(
        {
            "table": tname,
            "five_part": {
                "speculation": "这张表有数据，但表头认不出（可能是标题行/合并单元格/多级表头/根本没有表头）。",
                "evidence": "原表有 %d 行数据（%s）。"
                % (len(original.get("rows") or []), reason),
                "what_might_be_wrong": "几何清洗不会猜表头；硬猜可能改错列名，所以先按原表保留。",
                "next_step": "请告诉我表头是什么（用顿号分隔，如：设备、负责人、状态），或回复「让模型恢复」，或「不用」。",
                "please_confirm_or_correct": "表头是什么？",
            },
        }
    )


def _assign_columns(table, names):
    """把用户给的表头名按位置赋给列，所有行当数据。"""
    old_cols = list(table.get("columns") or [])
    rows = []
    for r in (table.get("rows") or []):
        if isinstance(r, dict):
            rows.append({n: r.get(c) for n, c in zip(names, old_cols)})
        else:
            rows.append({n: None for n in names})
    return {
        "name": table.get("name"),
        "columns": list(names),
        "rows": rows,
        "recognized": True,
        "shape": "user_header",
    }


def _finish_table(ctx, parsed):
    """表格支线收尾：画像缓存 + 统一 JSON + 落库（与 pipeline 的表格支线同语义）"""
    table = parsed["table"]
    # 多 sheet 时 table.name 已是 文件::Sheet，优先保留
    tname = table.get("name") or parsed["filename"]
    table["name"] = tname
    # 二次清洗（默认开）：只对"认不出"的表动手；正常表一字不改
    if config.get_param("table_normalizer.enabled", False) and not ctx["data"].get("skip_wash"):
        first = table_normalizer.normalize(table)
        if not first.get("recognized"):
            original = table
            orig_nonempty = sum(
                1 for r in (original.get("rows") or [])
                if any(v is not None and str(v).strip() != ""
                       for v in (r.values() if isinstance(r, dict) else r))
            )
            has_data = orig_nonempty > 0
            confirm = (ctx["data"].get("wash_confirm") or {}).get(tname)
            if not has_data:
                # 空表：不动（不洗、不问）
                pass
            elif confirm is None:
                # 先问用户：要不要二次洗？并记下"等回答"
                _ask_wash(ctx, tname, original, "未自动清洗")
                write_pending_wash(parsed.get("path") or "", tname)
            elif not confirm:
                # 用户说不洗：不动（保留原表）
                clear_pending_wash()
            else:
                # 用户确认要洗：先备份，再洗
                clear_pending_wash()
                ctx["data"].setdefault("tables_original", {})[tname] = original
                washed = table_normalizer.normalize_second_pass(original)
                if not washed.get("columns"):
                    # 洗成空：保留原表 + 报警 + 再问
                    ctx["data"].setdefault("wash_rejected", []).append(
                        {"table": tname, "reason": "washed_to_empty"})
                    _ask_wash(ctx, tname, original, "洗成空，已保留原表")
                    # 可选重后续（默认关）：显式开启且配了 Key，才让模型尝试恢复
                    if config.get_param("table_recover.on_wash_rejected", False):
                        api_key = ctx.get("api_key") or ""
                        if api_key:
                            rec = table_recover.recover(
                                original, api_key,
                                user_hint=ctx.get("user_input") or "")
                            if rec.get("status") == "recovered":
                                table = rec["table"]
                else:
                    washed_rows = len(washed.get("rows") or [])
                    if first.get("shape") == "headerless" or not washed.get("recognized"):
                        # 几何清洗救不了无表头（不会猜语义）→ 不采信，保留原表 + 报警 + 问
                        ctx["data"].setdefault("wash_rejected", []).append(
                            {"table": tname, "reason": "still_unrecognized"})
                        _ask_wash(ctx, tname, original, "洗了但仍认不出表头")
                    elif orig_nonempty >= 4 and washed_rows < orig_nonempty * 0.5:
                        # 洗坏但没变空：非空行骤减 → 不采信，保留原表 + 报警 + 再问
                        ctx["data"].setdefault("wash_rejected", []).append(
                            {"table": tname, "reason": "rows_shrunk",
                             "orig_rows": orig_nonempty, "washed_rows": washed_rows})
                        _ask_wash(ctx, tname, original,
                                  "非空行骤减（%d→%d）" % (orig_nonempty, washed_rows))
                    else:
                        # 洗完非空：用洗后的
                        table = washed
                        _issues = table_normalizer.consistency_issues(table)
                        if _issues:
                            ctx["data"].setdefault("structure_issues", []).append(
                                {"table": tname, "issues": _issues})
    profile = table_engine.profile_table(table)
    cache_db.init_cache_tables()
    if not cache_db.get_table_profile(parsed["path"], parsed["mtime"]):
        cache_db.set_table_profile(parsed["path"], parsed["mtime"], profile)
    unified = normalizer.normalize(
        [], "technical", tname, parsed["hash"],
        extra_meta={"path": parsed["path"], "columns": table["columns"],
                    "row_count": len(table["rows"]), "profile": profile})
    unified["structured_content"]["tables"].append(
        {"name": table["name"], "columns": table["columns"], "rows": table["rows"]})
    unified["database_ready"] = True
    unified["_external_tables"] = [table]
    db_writer.write_document(unified)
    ctx["data"].setdefault("unified", {})[tname] = unified
    ctx["data"].setdefault("tables", {})[tname] = table


# ------------------------------------------------------------------
# 3. AI 视觉分析能力（当前模型无视觉 → 优雅降级标注，不阻塞）
# ------------------------------------------------------------------

def cap_vision(ctx, task):
    """图片内容理解。当前文本模型无视觉能力，按蓝图做降级：标注并继续。"""
    parsed_store = ctx["data"].get("parsed", {})
    images = [n for n, p in parsed_store.items() if p.get("kind") == "image"]
    if not images:
        return _ok({"images": [], "note": "没有图片需要视觉分析"})
    notes = ctx["data"].setdefault("degrade_notes", [])
    for n in images:
        notes.append(f"「{n}」是图片，当前模型不支持视觉分析，已跳过（可换多模态 Key 后支持）")
    _report(ctx, f"🖼 {len(images)} 个图片已标注（视觉能力降级）")
    return _ok({"images": images, "degraded": True})


# ------------------------------------------------------------------
# 4. 文档类型判别能力
# ------------------------------------------------------------------

def cap_classify(ctx, task):
    """对已解析的文本文件做 6 类判别（带内容缓存），结果写回 parsed 条目"""
    parsed_store = ctx["data"].get("parsed", {})
    targets = _selected_names(ctx, task, parsed_store, want_kind="text")
    if not targets:
        return _fail("没有可判别的文本文件（前置 parse 无产出）")
    cache_db.init_cache_tables()
    done = []
    for name in targets:
        p = parsed_store[name]
        cached = cache_db.get_doc_type(p["hash"])
        if cached:
            p["doc_type"], p["classify_scores"] = cached["doc_type"], cached["scores"]
        else:
            doc_type, scores = doc_classifier.classify(p.get("text", ""))
            p["doc_type"], p["classify_scores"] = doc_type, scores
            cache_db.set_doc_type(p["hash"], doc_type, scores)
        done.append({"file": name, "doc_type": p["doc_type"]})
        _report(ctx, f"🧭 {name} → 「{p['doc_type']}」")
    return _ok(done)


# ------------------------------------------------------------------
# 5. 文本分块能力
# ------------------------------------------------------------------

def cap_split(ctx, task):
    """按文档类型策略分块 → ctx.data["chunks"][文件名]"""
    parsed_store = ctx["data"].get("parsed", {})
    targets = _selected_names(ctx, task, parsed_store, want_kind="text")
    if not targets:
        return _fail("没有可分块的文本文件（前置 parse 无产出）")
    chunks_store = ctx["data"].setdefault("chunks", {})
    out = {}
    for name in targets:
        p = parsed_store[name]
        doc_type = p.get("doc_type") or doc_classifier.classify(p.get("text", ""))[0]
        p["doc_type"] = doc_type  # classify 任务被跳过时的自愈（判别是纯本地评分，不违反 AI 决策原则）
        chunks = text_splitter.split(p.get("text", ""), doc_type)
        chunks_store[name] = chunks
        out[name] = len(chunks)
        _report(ctx, f"✂️ {name} 切成 {len(chunks)} 块")
    return _ok(out)


# ------------------------------------------------------------------
# 6. AI 调用能力（块级抽取 + 自我修正重跑 + 归一化 + 落库）
# ------------------------------------------------------------------

def cap_ai_call(ctx, task):
    """
    对有分块的文件逐块 AI 抽取；失败块按 review.rounds 自我修正重跑；
    归一化为统一 JSON 后落库。api_key 为空时 ai_caller 内部自动降级摘要。
    """
    chunks_store = ctx["data"].get("chunks", {})
    parsed_store = ctx["data"].get("parsed", {})
    targets = _selected_names(ctx, task, chunks_store)
    if not targets:
        return _fail("没有可抽取的文本块（前置 split 无产出）")
    unified_store = ctx["data"].setdefault("unified", {})
    chunk_store = ctx["data"].setdefault("chunk_results", {})
    rounds = int(config.get_param("review.rounds"))
    out = {}
    for name in targets:
        chunks = chunks_store[name]
        doc_type = parsed_store.get(name, {}).get("doc_type", "technical")
        results = []
        for c in chunks:
            r = ai_caller.call_ai_json(c, doc_type, ctx.get("api_key") or "")
            r.update({k: c[k] for k in ("start_offset", "end_offset")})
            r["content"] = c["content"]
            results.append(r)
        # 自我修正：只重跑问题块，最多 review.rounds 轮
        for _ in range(rounds):
            bad = [r for r in results if r["status"] != "ok"]
            if not bad:
                break
            for r in bad:
                origin = next(c for c in chunks if c["chunk_id"] == r["chunk_id"])
                retry = ai_caller.call_ai_json(origin, doc_type, ctx.get("api_key") or "")
                retry.update({k: origin[k] for k in ("start_offset", "end_offset")})
                retry["content"] = origin["content"]
                results[results.index(r)] = retry
        p = parsed_store[name]
        unified = normalizer.normalize(results, doc_type, name, p["hash"],
                                       extra_meta={"path": p["path"]})
        if any(r["status"] == "degraded" and r.get("summary") for r in results):
            unified["_has_degraded_summary"] = True
        unified["_chunk_results"] = results
        db_writer.write_document(unified)
        unified_store[name] = unified
        chunk_store[name] = results
        n_fail = unified["document_info"]["failed_chunks"]
        out[name] = {"chunks": len(chunks), "failed": n_fail}
        _report(ctx, f"🧩 {name} 抽取完成（{len(chunks)} 块，失败 {n_fail}）")
    return _ok(out)


# ------------------------------------------------------------------
# 6.5 全文分析能力（摘要/要点/结论/建议）
# ------------------------------------------------------------------

def cap_analyze(ctx, task):
    """对已解析文本文件做全文分析（不走分块，直接送全文给 AI）。
    产出存 ctx.data["analysis"][文件名] = {summary, key_points, conclusions, recommendations}。"""
    from pipeline.stage3_processor.ai_caller import analyze_document
    parsed_store = ctx["data"].get("parsed", {})
    targets = _selected_names(ctx, task, parsed_store, want_kind="text")
    if not targets:
        return _fail("没有可分析的文本文件（前置 parse 无产出）")
    analysis_store = ctx["data"].setdefault("analysis", {})
    out = {}
    for name in targets:
        p = parsed_store[name]
        text = p.get("text", "")
        doc_type = p.get("doc_type") or "technical"
        result = analyze_document(text, doc_type, ctx.get("api_key") or "")
        analysis_store[name] = result
        status = "✅" if result["status"] == "ok" else ("⚠️" if result["status"] == "degraded" else "❌")
        _report(ctx, f"🔍 {name} 分析{status}")
        out[name] = result["status"]
    return _ok(out)


def _selected_names(ctx, task, store: dict, want_kind: str = None):
    """从共享存储里选目标：task.input.file 指定单个；否则全量（可按 kind 过滤）"""
    inp = task.get("input") or {}
    name = inp.get("file") if isinstance(inp, dict) else None
    if name:
        return [name] if name in store else []
    names = []
    for n, v in store.items():
        if want_kind and v.get("kind") != want_kind:
            continue
        names.append(n)
    return names


# ------------------------------------------------------------------
# 7. 多表联查 / 数据查询能力
# ------------------------------------------------------------------


def cap_table_query(ctx, task):
    """
    两种形态（以"有没有现成表格"为准，operation_type 只是提示）：
    - 本次已解析出表格：联查→筛选→排序，产出 table_result
    - 没有表格且 operation_type=query：从本地知识库检索已处理数据
    """
    intent = ctx.get("intent") or {}
    tables = ctx["data"].get("tables") or {}
    if not tables:
        if intent.get("operation_type") == "query":
            return _db_query(ctx, intent)
        return _fail("没有可查询的表格数据（目录中没有表格文件）")
    result = _join_all(list(tables.values()), ctx)
    notes = ctx["data"].setdefault("filter_notes", [])
    inp = task.get("input") or {}

    # 筛选条件：任务 input 优先（AI 在任务级给了更精确的），否则用 8 维意图。
    # 列名对齐/条件解析统一走 semantic_match：本地确定优先，落空才按需 LLM 兜底。
    conditions = inp.get("filter_conditions") or intent.get("filter_conditions") or []
    parsed, cond_notes = semantic_match.parse_conditions(
        conditions, result["columns"], api_key=ctx.get("api_key") or "")
    notes.extend(cond_notes)
    for c in parsed:
        col, op, value = c["column"], c["op"], c["value"]
        try:
            filtered = table_engine.filter_rows(result, col, op, value)
            # 等值匹配落空时自动降级为包含匹配（AI 给的值可能只是单元格内容的一部分）
            if op == "==" and not filtered["rows"]:
                filtered = table_engine.filter_rows(result, col, "contains", value)
                if filtered["rows"]:
                    notes.append(f"条件「{c['source']}」等值无结果，已按包含匹配")
            result = filtered
        except ValueError as e:
            notes.append(str(e))

    # 排序（数值优先，其次字典序）
    sort_req = inp.get("sort_requirement") or intent.get("sort_requirement") or {}
    field, order = sort_req.get("field"), str(sort_req.get("order", "desc")).lower()
    if field:
        col, _ = semantic_match.match_column(result["columns"], str(field))
        if col:
            def key_fn(r):
                v = table_engine._to_number(r.get(col, ""))
                return (0, v) if v is not None else (1, str(r.get(col, "")))
            result["rows"].sort(key=key_fn, reverse=(order != "asc"))
            result["sorted_by"] = f"{col} {'降序' if order != 'asc' else '升序'}"
        else:
            notes.append(f"排序字段「{field}」在表中不存在，按原顺序输出")

    ctx["data"]["table_result"] = result
    _report(ctx, f"🔎 表格查询完成：{len(result['rows'])} 行")
    return _ok({"rows": len(result["rows"]), "columns": result["columns"]})


def cap_table_synthesize(ctx, task):
    """
    动态表格合成：按用户自然语言指令生成任意输出列（映射/筛选/联查均由 LLM 方案驱动，不写死业务）。
    产出 ctx.data["table_result"]，供 export 写出 CSV/XLSX。
    支持 regen_hint（硬门禁回滚后的结构化错误反馈）。
    """
    from pipeline.synth_contract.boot import ensure_contract_library, ContractLibraryError
    try:
        ensure_contract_library()
    except ContractLibraryError as e:
        return _fail("契约库不可用：" + str(e), retryable=False)
    tables = ctx["data"].get("tables") or {}
    if not tables:
        return _fail("没有可合成的表格数据（请先 parse 目录中的 Excel/CSV）", retryable=False)
    _report(ctx, f"🧬 正在根据指令动态合成表格（{len(tables)} 张源表）…")
    user_input = ctx.get("user_input") or ""
    hint = (ctx.get("data") or {}).get("regen_hint") or ""
    if hint:
        user_input = user_input + "\n\n【验证反馈，必须修正】\n" + hint
    result = table_synth.synthesize(
        user_input,
        tables,
        ctx.get("api_key") or "",
        control=ctx.get("control"),
        on_progress=lambda m: _report(ctx, m),
    )
    notes = ctx["data"].setdefault("filter_notes", [])
    for n in result.get("plan_notes") or []:
        notes.append(n)
    if not result.get("columns"):
        return _fail("表格合成失败：" + "；".join(notes[:3] or ["未知原因"]))
    ctx["data"]["table_result"] = result
    ctx["data"]["synth_plan"] = result.get("synth_plan")
    ctx["data"]["instruction_conflicts"] = list(result.get("instruction_conflicts") or [])
    ctx["data"]["sheet_notes"] = list(result.get("sheet_notes") or [])
    _report(ctx, f"🧬 合成完成：{len(result['rows'])} 行 × {len(result['columns'])} 列")
    return _ok({"rows": len(result["rows"]), "columns": result["columns"]})


def _join_all(tables: list, ctx):
    """≥2 个表时自动找同名列等值联查；联不上则取行数最多的表"""
    result = tables[0]
    for t in tables[1:]:
        key = table_engine.find_join_key(result, t)
        if key:
            result = table_engine.join_tables(result, t, key[0], key[1])
            _report(ctx, f"🤝 联查：{result['name']}（键 {key[0]}）")
        else:
            ctx["data"].setdefault("filter_notes", []).append(
                f"「{t['name']}」与主表无同名列，未参与联查")
            if len(t["rows"]) > len(result["rows"]):
                result = t
    return result


def _db_query(ctx, intent):
    """知识库检索：按目标对象关键词在 document/entity/fact/table_row 中 LIKE 查找"""
    import sqlite3
    keyword = str(intent.get("target_object") or "").strip()
    if not keyword:  # 没有明确对象时取指令里的非停用词片段兜底
        keyword = ctx["user_input"].strip()[:20]
    like = f"%{keyword}%"
    conn = sqlite3.connect(db_writer.DB_PATH)
    try:
        db_writer.init_tables()
        docs = conn.execute(
            "SELECT filename, doc_type, processed_at FROM document"
            " WHERE filename LIKE ? ORDER BY id DESC LIMIT 10", (like,)).fetchall()
        facts = conn.execute(
            "SELECT subject, predicate, object FROM fact"
            " WHERE subject LIKE ? OR object LIKE ? OR predicate LIKE ? LIMIT 20",
            (like, like, like)).fetchall()
        rows = conn.execute(
            "SELECT table_name, row_data FROM table_row WHERE row_data LIKE ? LIMIT 30",
            (like,)).fetchall()
    finally:
        conn.close()
    if not (docs or facts or rows):
        return _fail(f"知识库中没有检索到与「{keyword}」相关的数据（先整理过文件才能查询）",
                     retryable=False)  # 数据不存在，重跑也不会变
    lines = [f"🔎 知识库检索「{keyword}」："]
    if docs:
        lines.append("相关文档：" + "、".join(f"{d[0]}（{d[1]}）" for d in docs))
    if facts:
        lines.append("相关事实：")
        lines += [f"· {s} — {p} — {o}" for s, p, o in facts]
    if rows:
        lines.append(f"相关表格记录 {len(rows)} 条：")
        lines += [f"· [{tn}] {rd}" for tn, rd in rows[:15]]
    answer = "\n".join(lines)
    ctx["data"]["answer_text"] = answer
    return _ok({"hits": len(docs) + len(facts) + len(rows), "answer": answer})


# ------------------------------------------------------------------
# 8. 文件整理能力（移动/归类；记录 operations 供撤回）
# ------------------------------------------------------------------

def cap_file_organize(ctx, task):
    """执行 dispatcher 编译好的移动清单；同名冲突自动加序号，绝不覆盖"""
    moves = ctx.get("moves") or []
    if not moves:
        return _ok({"moved": 0, "note": "没有文件需要移动"})
    moved, failed = [], []
    for src, dst in moves:
        try:
            if not Path(src).exists():
                failed.append((Path(src).name, "文件已不存在"))
                continue
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            final = Path(dst)
            if final.exists():
                i = 1
                while True:
                    cand = final.with_name(f"{final.stem}_{i}{final.suffix}")
                    if not cand.exists():
                        final = cand
                        break
                    i += 1
            shutil.move(str(src), str(final))
            moved.append((src, final))
        except OSError as e:
            failed.append((Path(src).name, str(e)))
    ctx["operations"].extend({"type": "move", "src": str(s), "dst": str(d)}
                             for s, d in moved)
    _report(ctx, f"📦 移动 {len(moved)} 个文件" + (f"，失败 {len(failed)}" if failed else ""))
    out = {"moved": len(moved), "failed": [f"{n}（{r}）" for n, r in failed]}
    if not moved and failed:
        return _fail("全部移动失败：" + "；".join(out["failed"][:5]), out)
    return _ok(out)


# ------------------------------------------------------------------
# 9. 导出能力（Word/PPT/Excel/文本）
# ------------------------------------------------------------------

def cap_export(ctx, task):
    """
    严格按 intent.output_format 导出（语义驱动，不擅自改格式）：
      word → docx；excel/xlsx → 优先 xlsx；csv → csv；text/空 → 仅聊天，不落盘；
      ppt → 失败（由验证器要求用户改格式，不静默改成 word）。
    文件先写入 staging 子目录，验证通过后再提交到 FileMate报告/（见 task_runner）。
    """
    from agent_core import validator as _validator

    intent = ctx.get("intent") or {}
    fmt = str(intent.get("output_format") or "text").lower()
    notes = ctx["data"].setdefault("degrade_notes", [])
    title = str(intent.get("target_object") or "FileMate 整理报告")
    blocks = _build_report_blocks(ctx, title)
    table = ctx["data"].get("table_result")

    if fmt in ("text", ""):
        return _ok({"format": "text", "note": "按指令以文本形式输出，未生成文件"})
    if fmt == "ppt":
        return _fail("用户要求 PPT，但当前不支持；请改口令为 word / excel / csv",
                     retryable=False)

    # 暂存目录：验证失败可整目录回滚，不污染正式报告
    final_dir = Path(ctx["work_dir"]) / "FileMate报告"
    stage_dir = final_dir / ".staging"
    safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)[:40] or "整理报告"
    stage_dir.mkdir(parents=True, exist_ok=True)
    ctx["data"].setdefault("_delivery_snap", _validator.snapshot_delivery(ctx))

    def _commit_staged(staged_path: Path) -> str:
        final_dir.mkdir(parents=True, exist_ok=True)
        final = final_dir / staged_path.name
        # 同名不覆盖：加序号
        if final.exists():
            i = 1
            while True:
                cand = final.with_name(f"{final.stem}_{i}{final.suffix}")
                if not cand.exists():
                    final = cand
                    break
                i += 1
        staged_path.replace(final)
        path = str(final)
        ctx["data"].setdefault("report_paths", []).append(path)
        ctx["data"].setdefault("staged_paths", []).append(path)
        return path

    if fmt in ("excel", "xlsx", "csv"):
        if not table or not table.get("columns"):
            return _fail("没有可导出的表格数据（请先 table_synthesize / table_query）")
        if fmt == "csv":
            staged = stage_dir / f"{safe_title}.csv"
            _write_csv(staged, table)
            path = _commit_staged(staged)
            _report(ctx, f"📄 已导出 CSV：{Path(path).name}")
            return _ok({"format": "csv", "path": path})
        staged = stage_dir / f"{safe_title}.xlsx"
        ok, err = _write_xlsx(staged, table)
        if ok:
            path = _commit_staged(staged)
            _report(ctx, f"📄 已导出 Excel：{Path(path).name}")
            return _ok({"format": "xlsx", "path": path})
        # 仅当用户接受 csv 语义时才降级；用户明确 excel 时仍写 csv 但标记 degraded 供验证器判定
        staged_csv = stage_dir / f"{safe_title}.csv"
        _write_csv(staged_csv, table)
        path = _commit_staged(staged_csv)
        notes.append(f"Excel 写入不可用（{err}），已临时导出 CSV")
        _report(ctx, f"📄 已导出 CSV（Excel 降级）：{Path(path).name}")
        return _ok({"format": "csv", "path": path, "degraded": True})

    if fmt == "word":
        staged = stage_dir / f"{safe_title}.docx"
        rendered = word_renderer.render_report(staged, title, blocks)
        staged_path = Path(rendered)
        if staged_path.parent != stage_dir:
            # 渲染器若写到了别处，挪进 staging 再提交
            target = stage_dir / staged_path.name
            try:
                if staged_path.exists() and staged_path != target:
                    staged_path.replace(target)
                    staged_path = target
            except OSError:
                staged_path = Path(rendered)
        path = _commit_staged(staged_path) if staged_path.exists() else str(rendered)
        if path not in (ctx["data"].get("report_paths") or []):
            ctx["data"].setdefault("report_paths", []).append(path)
            ctx["data"].setdefault("staged_paths", []).append(path)
        _report(ctx, f"📄 已生成 Word 报告：{Path(path).name}")
        return _ok({"format": "word", "path": path})

    return _fail(f"未知输出格式：{fmt}，请使用 text/word/excel/csv", retryable=False)


def _write_csv(path: Path, table: dict):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=table["columns"], extrasaction="ignore")
        w.writeheader()
        for row in table["rows"]:
            w.writerow({c: row.get(c, "") for c in table["columns"]})


def _write_xlsx(path: Path, table: dict):
    try:
        from openpyxl import Workbook
    except ImportError:
        return False, "未安装 openpyxl"
    try:
        wb = Workbook()
        ws = wb.active
        ws.title = "结果"
        ws.append(list(table["columns"]))
        for row in table["rows"]:
            ws.append([row.get(c, "") for c in table["columns"]])
        wb.save(path)
        return True, ""
    except Exception as e:
        return False, str(e)


def _build_report_blocks(ctx, title: str) -> list:
    """把共享数据装配成报告块：概览 → 表格结果 → 各文档实体/事实 → 降级说明"""
    intent = ctx.get("intent") or {}
    blocks = [("p", f"用户指令：{ctx['user_input']}"),
              ("p", f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")]
    table = ctx["data"].get("table_result")
    if table:
        blocks.append(("h2", "一、数据结果"))
        if table.get("sorted_by"):
            blocks.append(("p", f"排序：{table['sorted_by']}    共 {len(table['rows'])} 行"))
        blocks.append(("table", {"columns": table["columns"],
                                 "rows": table["rows"][:int(config.get_param("report.max_table_rows"))]}))
    unified_store = ctx["data"].get("unified") or {}
    analysis_store = ctx["data"].get("analysis") or {}
    if unified_store or analysis_store:
        sec = "二" if table else "一"
        blocks.append((f"h2", f"{sec}、文档内容整理"))
        max_e, max_f = (int(config.get_param("report.max_entities")),
                        int(config.get_param("report.max_facts")))
        for name, u in unified_store.items():
            sc = u["structured_content"]
            blocks.append(("p", f"【{name}】类型「{u['document_info']['type']}」"
                                f" 实体 {len(sc['global_entities'])} · 事实 {len(sc['facts'])}"))
            ents = "、".join(f"{e['name']}({e['type']})"
                             for e in sc["global_entities"][:max_e])
            if ents:
                blocks.append(("p", f"实体：{ents}"))
            facts = "\n".join(f"· {f['subject']} — {f['predicate']} — {f['object']}"
                              for f in sc["facts"][:max_f])
            if facts:
                blocks.append(("p", f"关键事实：\n{facts}"))
        # 全文分析结果（摘要/要点/结论/建议）
        for name, a in analysis_store.items():
            blocks.append(("p", f"【{name}】全文分析"))
            if a.get("summary"):
                blocks.append(("p", f"摘要：{a['summary']}"))
            if a.get("key_points"):
                blocks.append(("p", "要点：\n" + "\n".join(f"· {k}" for k in a["key_points"])))
            if a.get("conclusions"):
                blocks.append(("p", "结论：\n" + "\n".join(f"· {c}" for c in a["conclusions"])))
            if a.get("recommendations"):
                blocks.append(("p", "建议：\n" + "\n".join(f"· {r}" for r in a["recommendations"])))
    for note in ctx["data"].get("degrade_notes", []):
        blocks.append(("p", f"※ {note}"))
    for note in ctx["data"].get("filter_notes", []):
        blocks.append(("p", f"※ {note}"))
    if intent.get("constraints"):
        blocks.append(("p", "遵循约束：" + "、".join(map(str, intent["constraints"]))))
    return blocks


# ------------------------------------------------------------------
# 10. 复核能力（复核兜底，不通过则局部重跑）
# ------------------------------------------------------------------

def cap_review(ctx, task):
    """
    硬门禁复核：独立验证器判定；失败则原子回滚本轮交付物，并把结构化错误写入
    ctx.data["validation_feedback"] 供下一轮合成/导出使用。
    """
    control = ctx.get("control")
    if control and not control.checkpoint():
        return _fail("用户结束任务", retryable=False)

    from agent_core.reviewer import review_execution
    from agent_core import validator

    result = review_execution(
        ctx["user_input"], _summarize(ctx),
        ctx.get("api_key") or "",
        intent=ctx.get("intent") or {},
        ctx=ctx,
    )
    ctx["data"]["review"] = result

    if result.get("action") == "retry" and config.get_param("review.hard_gate"):
        snap = ctx["data"].get("_delivery_snap")
        deleted = validator.rollback_delivery(ctx, snap=snap, delete_files=True)
        has_conflict = bool(ctx["data"].get("instruction_conflicts"))
        feedback = []
        for i in result.get("issues") or []:
            if i.get("severity") not in ("high", "medium"):
                continue
            code = i.get("code") or ""
            if code in ("ai_assist",) and has_conflict:
                continue
            # 模型裁定的问题一律写入反馈；本地候选码也保留
            if i.get("description"):
                feedback.append(i["description"])
        ctx["data"]["validation_feedback"] = feedback
        ctx["data"]["needs_regen"] = True
        _report(ctx, f"🔁 硬门禁未通过，已回滚 {len(deleted)} 个文件，准备重跑")
        # 返回 ok=False 以便调度器把相关能力纳入重跑集合
        return _fail("验证未通过：" + "；".join(feedback[:3]),
                     output=result, retryable=True)

    mark = "✅" if result.get("passed") else ("⚠️" if result.get("action") == "degrade" else "🔁")
    _report(ctx, f"{mark} 复核结论：{result.get('action')}"
                 + (f"（问题 {len(result.get('issues') or [])} 项）" if result.get("issues") else ""))
    return _ok(result)


def _summarize(ctx) -> dict:
    """装配给复核看的执行结果摘要：统计 + 实体/事实样例（控制体积但可核验）"""
    table = ctx["data"].get("table_result")
    unified_view = {}
    for n, u in (ctx["data"].get("unified") or {}).items():
        sc = u["structured_content"]
        unified_view[n] = {
            "type": u["document_info"]["type"],
            "entities": len(sc["global_entities"]),
            "facts": len(sc["facts"]),
            "failed_chunks": u["document_info"]["failed_chunks"],
            "sample_entities": [f"{e['name']}({e['type']})"
                                for e in sc["global_entities"][:8]],
            "sample_facts": [f"{f['subject']}-{f['predicate']}-{f['object']}"
                             for f in sc["facts"][:6]],
        }
    return {
        "moved": len(ctx.get("operations") or []),
        "files": ctx["data"].get("files", []),
        "parsed_ok": [n for n, p in (ctx["data"].get("parsed") or {}).items()
                      if p.get("kind") in ("text", "table")],
        "unified": unified_view,
        "table_result": ({"columns": table["columns"], "rows": len(table["rows"]),
                          "sample": table["rows"][:5]} if table else None),
        "reports": ctx["data"].get("report_paths", []),
        "notes": ctx["data"].get("degrade_notes", []) + ctx["data"].get("filter_notes", []),
    }


# ------------------------------------------------------------------
# 能力注册表（任务编排层按名调用）
# ------------------------------------------------------------------

CAPABILITIES = {
    "scan": cap_scan,
    "parse": cap_parse,
    "vision": cap_vision,
    "classify": cap_classify,
    "split": cap_split,
    "ai_call": cap_ai_call,
    "analyze": cap_analyze,
    "table_query": cap_table_query,
    "table_synthesize": cap_table_synthesize,
    "file_organize": cap_file_organize,
    "export": cap_export,
    "review": cap_review,
}
