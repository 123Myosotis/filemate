# agent_core/orchestrator.py
"""
智能体核心入口：识别"内容整理"类意图并编排完整管线。
- 意图词表来自参数 intent.content_keywords（config.json 可追加）
- 流程：扫描目录 → 逐文件跑四层管线 → 落库 → 生成 Word 报告 → 汇总成聊天消息
- 表格文件（CSV≥2 个且同名列）自动等值联查 + 计数聚合
- 复核由 pipeline_orchestrator 内部完成（失败块打回重跑，轮数由 review.rounds 参数控制）
"""
import re
from pathlib import Path

import config  # 可调参数统一从 config.json 的 params 段读取（严禁写死）

from pipeline.pipeline_orchestrator import run_pipeline
from pipeline.stage1_parser import file_parser
from pipeline.stage3_processor import table_engine
from storage import db_writer
from export import word_renderer


def _content_intent_re():
    """内容意图触发正则：由词表参数 intent.content_keywords 拼接（支持正则片段）"""
    return re.compile("|".join(config.get_param_list("intent.content_keywords")))


def _supported_exts():
    """可整理扩展名：解析器能力(TEXT∪TABLE) + 参数 intent.supported_exts 追加"""
    return (file_parser.TEXT_EXTS | file_parser.TABLE_EXTS
            | set(config.get_param_list("intent.supported_exts")))


def has_content_intent(text: str) -> bool:
    """是否为内容整理类指令（区别于移动文件类的"文件整理"）"""
    return bool(_content_intent_re().search(text))


def handle_content_intent(user_input: str, target_dir, progress=None, selected_files=None) -> str:
    """
    内容整理主入口（dispatcher 调用）。返回给聊天区展示的完整消息。
    progress: 可选回调 fn(msg)，收到每个阶段进度（当前为同步执行，进度随最终消息汇总）。
    selected_files: 上层（AI 意图解析）已明确选定的文件路径列表；给了就直接用，
                    不再做本地语义猜测，避免"AI 选了一套、本地又选一套"。
    """
    target_dir = Path(target_dir)
    logs = []  # 收集进度，最后择要附在消息尾部

    def report(msg):
        logs.append(msg)
        if progress:
            progress(msg)

    # ---- 1. 扫描可处理文件 ----
    supported = _supported_exts()
    all_files = [f for f in target_dir.iterdir()
                 if f.is_file() and f.suffix.lower() in supported]
    if not all_files:
        return ("📂 当前目录没有可整理的文件。\n"
                "我支持：TXT / Markdown / Word(docx) / PDF / CSV / XLSX。\n"
                "请先在右侧文件管理器进入目标目录，再告诉我整理什么。")

    # ---- 2. 确定目标文件：上层显式指定优先；否则本地语义筛选兜底 ----
    if selected_files is not None:
        actual = {f.name: f for f in all_files}
        files = [actual[n] for n in selected_files if n in actual]
        desc = "按 AI 意图解析选定"
        if not files:
            return ("🤔 AI 选定的文件在当前目录中不存在（可能已移动或改名），请确认后重试。")
    else:
        files, desc = _select_files(user_input, all_files)
        if files is None:
            # 有多个文件但指令里指不出目标 → 列出来反问，绝不擅自整目录
            limit = int(config.get_param("intent.max_list_files"))
            names = "\n".join(f"· {f.name}" for f in all_files[:limit])
            return ("🤔 当前目录有多个可整理的文件，请告诉我要整理哪一个，例如：\n"
                    f"「整理 {all_files[0].name} 的内容」\n\n{names}\n"
                    "也可以说「把这里的文件全部整理出报告」处理全部。")

    report(f"开始整理 {len(files)} 个文件（{desc}）…")
    doc_results, table_tables, profiles = [], [], {}

    # ---- 2. 逐文件跑管线 ----
    from config import get_api_key
    api_key = get_api_key()
    for f in files:
        result = run_pipeline(f, api_key, progress=report)
        if not result["ok"]:
            doc_results.append({"filename": f.name, "ok": False, "message": result["message"]})
            continue
        doc_results.append({"filename": f.name, "ok": True,
                            "unified": result["unified"],
                            "chunk_results": result["chunk_results"],
                            "doc_type": result["doc_type"]})
        if result["table"]:
            table_tables.append(result["table"])
            profiles[f.name] = result["profiles"]

    # ---- 3. 落库 + Word 报告（文本文档） ----
    db_writer.init_tables()
    report_paths = []
    for r in doc_results:
        if not r["ok"]:
            continue
        unified = r["unified"]
        unified["_chunk_results"] = r["chunk_results"]  # 供 chunk 明细落库
        db_writer.write_document(unified)
        report_paths.append(_render_doc_report(target_dir, r))

    # ---- 4. 表格联查 + 聚合（≥2 个表且能自动找到同名列） ----
    join_msg, agg_msg = "", ""
    if len(table_tables) >= 2:
        key = table_engine.find_join_key(table_tables[0], table_tables[1])
        if key:
            joined = table_engine.join_tables(table_tables[0], table_tables[1], key[0], key[1])
            join_msg = (f"🤝 自动联查：{table_tables[0]['name']} ⨝ {table_tables[1]['name']}"
                        f"（联接键：{key[0]}），得 {len(joined['rows'])} 行")
            # 联查结果再按第一列做计数聚合，给出"每类多少条"的统计
            group_col = next((c for c in joined["columns"]
                              if profiles_get(profiles, c) != "number"), joined["columns"][0])
            try:
                agg = table_engine.aggregate(joined, group_col, func="count")
                agg_msg = f"📈 聚合统计（按「{group_col}」计数）：\n{table_engine.to_display(agg, limit=10)}"
            except ValueError:
                pass
            # 联查结果也写进报告
            report_paths.append(_render_join_report(target_dir, joined, key[0]))
        else:
            join_msg = "🤝 检测到多个表格，但没有同名列可自动联查（可在指令里说明联接字段）"

    # ---- 5. 汇总聊天消息 ----
    lines = [f"📚 内容整理完成，共处理 {len(doc_results)} 个文件："]
    for r in doc_results:
        if not r["ok"]:
            lines.append(f"· ⚠️ {r['filename']} — {r['message']}")
            continue
        info = r["unified"]["document_info"]
        sc = r["unified"]["structured_content"]
        status = "✅" if info["failed_chunks"] == 0 else "⚠️"
        lines.append(
            f"· {status} {r['filename']} — 「{r['doc_type']}」{info['total_chunks']} 块"
            f"（失败 {info['failed_chunks']} / 降级 {info['degraded_chunks']}）"
            f"· 实体 {len(sc['global_entities'])} · 事实 {len(sc['facts'])}"
            f" · 表格 {len(sc['tables'])} · 冲突 {len(sc['conflicts'])}"
        )
    if join_msg:
        lines.append(join_msg)
    if agg_msg:
        lines.append(agg_msg)
    for p in report_paths:
        lines.append(f"📄 报告已生成：{p}")
    if not api_key:
        lines.append("ℹ️ 未配置聊天模型 API Key，本次为降级摘要模式；在「设置 → 聊天模型设置」配好 Key 可获得完整实体/事实抽取。")
    lines.append("（数据已存入本地知识库，重复整理同一文件会自动命中缓存）")
    return "\n".join(lines)


def profiles_get(profiles: dict, col: str):
    """从多表画像里查某列的类型（任一表说是 number 就算 number）"""
    for p in profiles.values():
        if col in p and p[col]["type"] == "number":
            return "number"
    return "text"


# 用户明确表示"整目录"的说法 / 泛指词表 → 已移入 config.PARAM_DEFAULTS
# （intent.all_words / intent.generic_stems，config.json 可追加）


def _select_files(user_input: str, files: list):
    """
    语义筛选目标文件，按明确程度依次尝试：
    引号内名字 → "全部/这个目录"等整目录说法 → 句中出现的文件名/主名
    → 提到的扩展名（"整理这个csv"）→ 文件名带X → 目录唯一文件。
    词表均来自参数（intent.all_words / intent.generic_stems，追加式可配置）。
    返回 (选中文件列表, 筛选说明)；全部策略落空且目录有多个文件时
    返回 (None, None)，由调用方列出文件反问用户，绝不擅自全目录处理。
    """
    low = user_input.lower()
    # 1) 引号里的名字最明确
    m = re.search(r"[「『\"“]([^」』\"”]{1,40})[」』\"”]", user_input)
    if m:
        hits = [f for f in files if m.group(1) in f.name]
        if hits:
            return hits, f"按「{m.group(1)}」匹配"

    # 2) 明确说了"全部/这个目录/这里" → 整目录
    if any(w in user_input for w in config.get_param_list("intent.all_words")):
        return files, "整目录"

    # 3) 句中直接出现完整文件名或主名（不含扩展名也算；泛指词除外）
    generic = set(config.get_param_list("intent.generic_stems"))
    hits = [f for f in files
            if f.name.lower() in low
            or (f.stem.lower() in low and len(f.stem) >= 2
                and f.stem not in generic)]
    if hits:
        return hits, "按句中文件名匹配"

    # 4) 提到扩展名："把这边几个csv统计一下" → 只处理 csv
    exts = set(re.findall(r"(?<![a-z0-9])(csv|tsv|xlsx|xls|docx|doc|pdf|txt|md)(?![a-z0-9])", low))
    if exts:
        hits = [f for f in files if f.suffix.lower().lstrip(".") in exts]
        if hits:
            return hits, f"按扩展名 {','.join(sorted(exts))} 匹配"

    # 5) "文件名带/含 X" 的写法
    m = re.search(r"文件名?[带含有]+([^\s，,。的]{1,20})", user_input)
    if m:
        hits = [f for f in files if m.group(1) in f.name]
        if hits:
            return hits, f"按「{m.group(1)}」匹配"

    # 6) 目录里只有一个可整理文件 → 没有歧义，直接处理
    if len(files) == 1:
        return files, "目录中唯一可整理文件"

    return None, None


# ------------------------------------------------------------------
# Word 报告渲染
# ------------------------------------------------------------------

def _render_doc_report(target_dir: Path, r: dict) -> str:
    """文本文档 → 整理报告 docx；返回文件路径"""
    unified = r["unified"]
    info = unified["document_info"]
    sc = unified["structured_content"]
    max_ents = int(config.get_param("report.max_entities"))
    max_facts = int(config.get_param("report.max_facts"))
    max_rows = int(config.get_param("report.max_table_rows"))
    blocks = [
        ("p", f"文档类型：{info['type']}    分块：{info['total_chunks']}"
              f"（失败 {info['failed_chunks']} / 降级 {info['degraded_chunks']}）"
              f"    整理时间：{info['processed_at']}"),
        ("h2", "一、关键实体"),
    ]
    ents = sc["global_entities"]
    blocks.append(("p", "、".join(f"{e['name']}({e['type']})×{len(e['source_chunks'])}"
                                 for e in ents[:max_ents]) if ents else "（未提取到实体）"))
    blocks.append(("h2", "二、关键事实"))
    facts = sc["facts"]
    blocks.append(("p", "\n".join(f"· {f['subject']} — {f['predicate']} — {f['object']}"
                                  f"（置信度 {f['confidence']:.0%}）" for f in facts[:max_facts])
                   if facts else "（未提取到事实）"))
    if sc["conflicts"]:
        blocks.append(("h2", "三、冲突提示（同一主语同一属性出现不同值）"))
        blocks.append(("p", "\n".join(
            f"· {c['subject']} 的「{c['predicate']}」存在多个值：{' / '.join(c['objects'])}"
            for c in sc["conflicts"])))
    blocks.append(("h2", "四、章节内容"))
    content_by_id = {c["chunk_id"]: c for c in r["chunk_results"]}
    for ch in sc["chapters"]:
        blocks.append(("p", f"【{ch['section_path']}】"))
        for cid in ch["chunk_ids"]:
            c = content_by_id.get(cid, {})
            if c.get("status") == "degraded" and c.get("summary"):
                blocks.append(("p", f"[摘要] {c['summary']}"))
            elif c.get("content"):
                text = re.sub(r"\s+", " ", c["content"]).strip()
                blocks.append(("p", ("…" if cid != ch["chunk_ids"][0] else "") + text[:160]
                               + ("……" if len(text) > 160 else "")))
            else:
                blocks.append(("p", f"[{c.get('status', 'failed')}] {c.get('error', '无内容')}"))
    for t in sc["tables"]:
        blocks.append(("h2", f"表格：{t['name']}"))
        blocks.append(("table", {"columns": t["columns"], "rows": t["rows"][:max_rows]}))

    out = target_dir / "FileMate报告" / f"{Path(info['filename']).stem}_整理报告.docx"
    return word_renderer.render_report(out, f"{info['filename']} · 内容整理报告", blocks)


def _render_join_report(target_dir: Path, joined: dict, join_key: str) -> str:
    """联查结果 → 单独报告 docx；返回文件路径"""
    blocks = [("p", f"联接键：{join_key}    结果行数：{len(joined['rows'])}"),
              ("table", {"columns": joined["columns"],
                         "rows": joined["rows"][:int(config.get_param("report.max_join_rows"))]})]
    out = target_dir / "FileMate报告" / f"{joined['name']}_联查报告.docx"
    return word_renderer.render_report(out, f"{joined['name']} 联查报告", blocks)
