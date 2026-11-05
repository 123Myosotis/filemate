# config.py
"""配置管理：读写本地 config.json（API Key 等）"""
import json  # 负责配置数据的 JSON 序列化与反序列化
from pathlib import Path  # 跨平台的路径对象

# 配置文件固定存放在项目根目录（与本文件同目录）下的 config.json
CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    """读取整个配置文件并返回字典；文件不存在或内容损坏时返回空字典"""
    if CONFIG_PATH.exists():  # 配置文件存在才尝试读取
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))  # 以 UTF-8 读出文本并解析为 dict
        except Exception:
            return {}  # 解析失败（如文件被写坏）时静默降级为空配置，避免程序崩溃
    return {}  # 尚未生成过配置文件时返回空配置


def save_config(data: dict):
    """把配置字典写回 config.json；ensure_ascii=False 保留中文，indent=2 便于人工查看"""
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_api_key() -> str:
    """读取聊天模型 API Key（兼容旧字段 zhipu_api_key / deepseek_api_key）。"""
    data = load_config()
    return (data.get("api_key")
            or data.get("zhipu_api_key")
            or data.get("deepseek_api_key")
            or "")


def set_api_key(key: str):
    """写入/更新聊天模型 API Key，同时保留配置文件中已有的其他字段"""
    data = load_config()
    data["api_key"] = key
    # 清理旧字段，避免两套 Key 混淆
    data.pop("zhipu_api_key", None)
    data.pop("deepseek_api_key", None)
    save_config(data)


def set_param(key: str, value):
    """写入/更新 params 段中的单个键；不碰 API Key 等顶层字段。"""
    data = load_config()
    params = data.get("params")
    if not isinstance(params, dict):
        params = {}
    params[key] = value
    data["params"] = params
    save_config(data)


# ------------------------------------------------------------------
# 可调参数注册表（单一事实来源）
# 严禁在业务代码里写死参数：所有可调参数在此登记默认值，
# config.json 的 "params" 段按同名键覆盖即可生效，例如：
#   "params": { "api.model": "glm-4-flash", "review.rounds": 5,
#               "chunk.max_tokens.technical": 6000 }
# ------------------------------------------------------------------
PARAM_DEFAULTS = {
    # ---- Word 编辑（A–E 任务级；预算单位 token） ----
    "word_office.budget": {"A": 0, "B": 2000, "C": 8000, "D": 32000, "E": 4000},
    # ---- API 客户端（智谱 OpenAI 兼容：base 配到 v4，客户端自动拼 /chat/completions） ----
    "api.base_url": "https://open.bigmodel.cn/api/paas/v4",
    "api.model": "glm-4-flash",
    "api.temperature": 0.7,
    "api.max_tokens": 8192,
    "api.timeout": 120,               # 秒
    # ---- 内容抽取（结构化，低温度） ----
    "extract.temperature": 0.2,
    "extract.max_tokens": 2000,
    "extract.timeout": 90,            # 秒
    "extract.max_retries": 3,         # 单块最大尝试次数
    "extract.retry_waits": [2, 4, 6], # 重试等待秒数（递增）
    "extract.max_entities": 10,       # 每块实体上限
    "extract.max_facts": 15,          # 每块事实上限
    "extract.prompt_chars": 8000,     # 仅当 params 显式设置时才截断；否则跟上下文预算
    "extract.summary_chars": 4000,    # 降级摘要的原文截断（显式设置才生效）
    # ---- 上下文窗口（分块/规划/复核共用；按模型名覆盖） ----
    "llm.context_fill_ratio": 0.8,    # 输入+输出占用窗口的比例，未到此比例不切
    "llm.context_tokens": 128000,     # 模型不在对照表时的窗口
    "llm.context_by_model": {
        "glm-4-flash": 128000,
        "glm-4-flashx": 128000,
        "glm-4-air": 128000,
        "glm-4.5-air": 128000,
        "glm-4-plus": 128000,
        "glm-4": 128000,
        "glm-4-long": 1000000,
        "glm-4-0520": 128000,
        "GLM-4-Flash-250414": 110000,
        "glm-4-flash-250414": 110000,
    },
    "llm.prompt_overhead_tokens": 1500,  # 模板/系统说明预留（GUI：预算开销）
    "llm.reserved_output_tokens": 4000,  # 块分析/分块时为模型回答预留（GUI：预算开销）
    "llm.min_input_tokens": 2048,
    "llm.chars_per_token": 1.0,       # 中文场景按 1 字 1 token 估算，避免超窗
    # ---- 新链路 analyze_blocks（速率限制重试；与超预算路径独立） ----
    "api.max_retries_per_request": 6,  # 兼容旧 GUI；统一重试以 api.max_retry_seconds 为准
    "api.max_retry_seconds": 900,  # 单次请求重试墙钟上限（秒）
    "api.empty_retry_max_tokens": 1024,  # 空正文截断后降到的 max_tokens
    "question.repeat_prompt_threshold": 3,  # 同一疑问回复几次后界面提示（GUI，默认 3）
    "review.reanalyze_max": 1,             # 同一 mapping 因复核打回的重分析上限（GUI）
    "review.invalid_threshold": 3,         # 连续无效复核次数后转人机（GUI，默认 3）
    "review.model": "GLM-4-Flash-250414",  # 复核专用模型；空字符串仍沿用 api.model
    # 复核切包预算（fill/reserved/overhead 空 = 沿用 llm.*；不再使用 review_budget_tokens）
    "review.context_tokens": 110000,      # 复核上下文；与 GLM-4-Flash-250414 窗口一致
    "review.fill_ratio": None,            # 空则 llm.context_fill_ratio
    "review.reserved_output_tokens": None,  # 空则 llm.reserved_output_tokens；兼 max_tokens
    "review.prompt_overhead_tokens": None,  # 空则 llm.prompt_overhead_tokens
    "review.small_table_ratio": 0.5,      # 整表 prompt ≤ review_B×该比例 → 整包不切
    "review.max_packages": 30,            # 单次复核包数硬上限
    "review.inter_package_delay_sec": 1,  # 串行包间间隔（秒）
    # ---- 跨表合并（pipeline/block_cross_table；缺省不启用） ----
    "cross_table.enabled": False,
    "cross_table.replay_if_log_present": True,
    "cross_table.decision_log_filename": "cross_table_decision_log.json",
    "cross_table.name_normalize_ops": ["trim", "casefold", "width_fold"],
    "cross_table.value_sample_policy": "inherit_field_digest",
    "cross_table.proposal_max_calls_per_kind": None,
    # ---- 记忆纠错 Web（M4；仅设置端口；启停不在 GUI） ----
    "memory_correction.port": 8765,
    # ---- M5-CPU-Merge / M5.2（不合并 CURRENT；双引擎自学习） ----
    "memory_rag.maibot_data_dir": "",
    "memory_rag.self_learning_engine": "chat",
    "memory_rag.meaning_equivalence_mode": "ai",
    "memory_rag.egress_policy": "allow_external",
    "memory_rag.dialogue_review_enabled": True,
    "memory_rag.embedded_model_path": "",
    "memory_rag.meaning_similarity_threshold": 0.92,
    "memory_rag.meaning_fallback_threshold": 0.6,
    "memory_rag.jargon_inference_thresholds": [4, 8, 25, 100],
    "memory_rag.jargon_stopwords": [
        "的", "了", "是", "在", "和", "与", "或", "及", "等", "也", "都", "就",
        "而", "被", "把", "对", "从", "到", "为", "以", "其", "这", "那",
        "一个", "没有", "可以", "因为", "所以", "如果", "但是",
    ],
    "memory_rag.fusion.rrf_k": 60,
    "memory_rag.fusion.vector_weight": 0.7,
    "memory_rag.fusion.bm25_weight": 0.3,
    "memory_rag.retrieval.sparse_top_k": 40,
    "memory_rag.retrieval.dense_top_k": 40,
    "memory_rag.retrieval.hybrid_final_top_k": 10,
    # ---- AI 意图解析（用户指令 → 模式×领域） ----
    "router.temperature": 0.1,        # 意图解析要求稳定，低温度
    "router.max_tokens": 800,
    "router.timeout": 30,             # 秒
    "router.max_retries": 2,          # JSON 解析失败的重试次数
    "router.retry_waits": [2, 4],
    "router.max_list_files": 100,     # 送给 AI 的目录清单最多多少个文件
    "router.max_list_subdirs": 10,    # 意图解析时展开内部文件的子文件夹数量上限
    "router.correction_rounds": 1,    # 决策无法落地时，带错误反馈让 AI 自我修正的轮数
    "router.skip_dirs":               # 扫描时跳过的项目垃圾目录（避免 AI 被无关文件淹没）
        [".git", ".venv", "venv", "env", "__pycache__", ".idea", ".vscode",
         "node_modules", ".gradle", "build", "dist", "target", ".mypy_cache",
         ".pytest_cache", ".tox", ".DS_Store", "__MACOSX", "FileMate报告",
         ".staging"],
    # ---- 分块（每种文档类型可单独调） ----
    "chunk.overlap.technical": 200,   "chunk.max_tokens.technical": 4000,
    "chunk.overlap.novel": 300,       "chunk.max_tokens.novel": 3000,
    "chunk.overlap.guide": 150,       "chunk.max_tokens.guide": 3500,
    "chunk.overlap.project": 200,     "chunk.max_tokens.project": 3500,
    "chunk.overlap.contract": 100,    "chunk.max_tokens.contract": 4000,
    "chunk.overlap.academic": 200,    "chunk.max_tokens.academic": 4000,
    # ---- 动态任务编排（意图解析→任务拆解→DAG 调度） ----
    "planner.temperature": 0.1,       # 任务规划要求稳定，低温度
    "planner.max_tokens": 4000,       # 任务列表 JSON 的输出上限（多文件 DAG 较长，截断会导致整体失败）
    "planner.timeout": 60,            # 秒
    "planner.max_retries": 3,         # JSON 解析失败的重试次数
    "planner.retry_waits": [2, 4, 6],
    "planner.table_hints_chars": 3500,  # 仅当 params 显式设置时才截断表头
    "runner.max_workers": 4,          # 并行任务的最大线程数
    "runner.task_retries": 1,         # 单任务瞬时错误的自动重试次数
    "runner.max_task_name_len": 40,   # 任务名截断展示长度
    # ---- 表格规范化 / 二次制表（table_normalizer，纯几何清洗，无 LLM） ----
    "table_normalizer.max_top_bands": 3,   # 表头上方最多去掉几行标题/抬头
    "table_normalizer.matrix_min_rows": 5, # 展开为长表至少需要多少行，否则不判定为矩阵
    "table_normalizer.enabled": True,      # 二次清洗总开关（默认开；只对"认不出"的表动手，正常表/空表不动）
    # ---- 语义匹配（semantic_match：列名对齐 / 条件解析；本地确定优先） ----
    "semantic_match.llm_fallback": False,  # 本地落空时才调 LLM 兜底（默认关；开启需 API Key）
    # ---- 表格结构恢复（table_recover：识别不出表头时让模型恢复；仍不行则五段式求证） ----
    "table_recover.llm_recover": False,    # 识别不出表头时是否调 LLM 尝试恢复结构（默认关）
    "table_recover.on_wash_rejected": False,  # 洗成空表但有数据时是否让模型尝试恢复（默认关；会调 LLM）
    "table_recover.summary_rows": 8,       # 送给模型的网格摘要行数
    # ---- 解析 / 复核 / 交互展示 ----
    "parser.max_size_mb": 50,         # 单文件大小上限
    "review.rounds": 2,               # 硬门禁失败后的回滚重跑轮数（防死循环）
    "review.hard_gate": True,         # True=验证失败必须回滚，禁止交付不合格文件
    "review.ai_gate": True,           # True=由模型根据证据裁定 pass/retry；本地只供事实
    "review.require_external": True,  # 必须过独立验证器，不能只靠模型自评
    "review.dimensions":              # 复核维度（动态可配）
        ["数据准确性", "需求符合度", "关联正确性", "格式完整性", "输出格式", "完整性校验"],
    "review.rollback_caps":           # 验证失败时需要清空并重跑的能力
        ["table_synthesize", "table_query", "export", "ai_call", "analyze"],
    "intent.max_list_files": 15,      # 反问时最多列出几个文件
    "report.max_entities": 30,        # Word 报告实体条数
    "report.max_facts": 30,           # Word 报告事实条数
    "report.max_table_rows": 50,      # Word 报告单表行数
    "report.max_join_rows": 100,      # 联查报告行数
    # ---- 动态表格合成（LLM 出方案，本地机械执行；无业务写死） ----
    "synth.temperature": 0.1,
    "synth.max_tokens": 4000,
    "synth.timeout": 90,
    "synth.max_retries": 2,
    "synth.retry_waits": [2, 4],
    "synth.sample_rows": 5,           # 送给模型的每表样例行数
    "synth.max_distinct": 40,         # 关键列去重样例上限
    "synth.direct_row_cap": 80,       # 小表可让模型直接吐行；超过则只出方案
    "synth.schema_chunk_tables": 8,   # 仅当 params 显式设置时才按表个数再切一刀
    "synth.max_plan_chars": 12000,    # 仅当 params 显式设置时才作为规划上限
    "synth.anchor_budget_ratio": 0.35,  # 小表整包最多占用单批预算的比例（每批都带上）
    "synth.small_payload_ratio": 0.12,  # 摘要短于该比例视为小表，跨批钉住
    "synth.index_max_cols": 12,         # 分批目录里每表最多列出的列名数（防目录吃光窗口）
    "synth.profile_scan_rows": 4000,  # 大表画像最多扫描行数（步进取样，不扫完全表）
    "synth.ai_enrich": True,          # 用 AI 动态拆解用户全部条件/列/类别（禁止靠写死单规则）
    "synth.ai_category": True,        # 相近语义类别交给 AI 精分
    "synth.ai_category_batch": 80,    # 每批送给 AI 的待归类样例数（增大减轮次）
    "synth.ai_category_max_unique": 48,  # 去重后最多问 AI 多少条（控时）
    "synth.category_hi": 0.78,        # 本地高置信直接归类，少送 AI
    "synth.category_lo": 0.52,        # 低于此分直接丢弃，不送 AI
    "synth.ai_link": True,            # 实体对齐多候选时问 AI，不靠本地猜
    "synth.ai_link_batch": 30,
    "synth.ai_link_max": 40,
    "synth.join_min_overlap": 3,      # 硬联接至少要有这么多取值对得上，否则不联
    "synth.layout_prefix_ratio": 0.6,  # 短名只是长名的前缀且短于该比例时，视为限定词而不是实体
    "synth.ai_bind": True,            # 输出列→源列一律由 AI 裁定（取消本地相似度打分）
    "synth.ai_bind_max_tokens": 1200,
    "synth.bind_candidates_per_out": 6,  # 阶段1每输出列最多保留候选源列数
    "synth.bind_sample_vals": 8,         # 阶段2每列送给模型的去重样例数
    "synth.bind_profile_scan_rows": 0,   # 0=用 synth.profile_scan_rows；画像扫描上限
    "synth.join_plan_max_tables": 12,    # 一次联表方案最多覆盖几张右表
    "synth.join_chain_rounds": 2,        # 桥表先联上后，用新列再问未成功的右表
    "synth.structure_tokens_per_block": 180,  # 结构判断输出随块数放大，未显式收紧时不超过 api.max_tokens
    "synth.ai_enrich_max_tokens": 2500,
    # ---- 合成耗时：整链缓存/并行/早退（数值一律可配，禁止写死在代码） ----
    "synth.structure_cache": True,           # 同目录同表结构指纹复用结构判断
    "synth.parallel_ai_workers": 4,          # 二次判断等可并行 AI 的线程数（1=串行）
    "synth.skip_sparse_layout_pass": True,   # 关联已抄齐维列时跳过第二次实体对齐 AI
    "synth.sparse_fill_min_rate": 0.15,      # 维列平均填充率≥此值视为已抄齐
    "synth.ai_catalog_max_left": 120,        # 近义目录对齐最多问多少个左值（控时）
    "synth.max_relation_decide_rounds": 3,   # 表间关联方案最多问几轮（含零命中换键）
    "synth.bind_review": True,              # 绑定后是否再跑常值/填充复核 AI
    # ---- 语义判断词表（追加式，见 get_param_list） ----
    "intent.content_keywords":        # 内容整理意图触发词（可用正则片段）
        ["整理出", "提取", "汇总", "统计", "联查", "关联", "找出来",
         "内容整理", "生成.{0,8}报告", "总结", "梳理", "导出", "合成"],
    "intent.table_export_signals":    # 触发动态合成+表格导出的说法（正则片段）
        ["导出", "excel", "xlsx", "csv", "表格", "列出行", "合成",
         "汇总成表", "输出.{0,6}表", "整理出", "给我的表", "新表"],
    "intent.all_words":               # 明确表示"整目录"的说法
        ["全部", "所有", "整个目录", "这个目录", "该目录", "目录里",
         "文件夹里", "这里", "每一个", "每个文件", "这几个", "每份"],
    "intent.generic_stems":           # 视为泛指词的文件主名（不当作指名道姓）
        ["文件", "文档", "数据", "表格", "内容", "资料", "报告", "测试", "记录", "日志"],
    "intent.supported_exts": [],      # 追加的可整理扩展名（默认含解析器支持的全部格式）
    "fileops.action_words":           # 文件整理动作词
        ["整理", "移动", "分类", "归档", "备份", "归类", "移到", "放到", "放入"],
    "fileops.ai_only_words":          # 命中即跳过规则、交给 AI 的模糊词
        ["内容", "主题", "有关", "相关", "为什么", "怎么", "如何", "找出", "总结"],
    # 类型词表（整体可覆盖）：类型词 → {exts: 扩展名列表, folder: 默认文件夹}
    "fileops.type_words": {
        "图片":  {"exts": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"], "folder": "图片"},
        "照片":  {"exts": [".jpg", ".jpeg", ".png"], "folder": "图片"},
        "视频":  {"exts": [".mp4", ".mov", ".avi", ".mkv", ".flv"], "folder": "视频"},
        "音频":  {"exts": [".mp3", ".wav", ".flac", ".m4a"], "folder": "音频"},
        "音乐":  {"exts": [".mp3", ".wav", ".flac", ".m4a"], "folder": "音频"},
        "文档":  {"exts": [".pdf", ".doc", ".docx", ".txt", ".md"], "folder": "文档"},
        "表格":  {"exts": [".xlsx", ".xls", ".csv"], "folder": "表格"},
        "演示":  {"exts": [".ppt", ".pptx"], "folder": "演示"},
        "压缩包": {"exts": [".zip", ".rar", ".7z"], "folder": "压缩包"},
        "程序":  {"exts": [".exe", ".msi"], "folder": "程序"},
        "安装包": {"exts": [".exe", ".msi"], "folder": "程序"},
    },
}


def param_is_user_set(key: str) -> bool:
    """config.json 的 params 是否显式写了该键。未写则走动态预算，不用注册表里的保守上限。"""
    params = load_config().get("params", {})
    return isinstance(params, dict) and key in params


def get_param(key: str, default=None):
    """读取可调参数：config.json 的 params 段优先，未配置时回退注册表默认值。

    cross_table.evidence_min_len 在用户未覆盖时读当前契约 cross_table.evidence_min_len，
    再回退注册表。比较逻辑不得另写长度字面量。
    """
    params = load_config().get("params", {})
    if key in params:
        return params[key]
    contract_fields = {
        "cross_table.rank_top_n": "rank_top_n",
        "cross_table.proposal_max_calls_per_kind": "proposal_max_calls_per_kind",
        "cross_table.user_retry_max_rounds": "user_retry_max_rounds",
        "cross_table.trace_dir": "trace_dir",
        "cross_table.trace_filename_pattern": "trace_filename_pattern",
        "cross_table.sample_preview_cap": "sample_preview_cap",
    }
    if key in contract_fields:
        try:
            from pipeline.synth_contract.registry import load_contract

            cross = (load_contract().data or {}).get("cross_table") or {}
            field = contract_fields[key]
            if field in cross:
                return cross[field]
        except Exception:
            pass
    return PARAM_DEFAULTS.get(key, default)


def get_param_list(key: str):
    """
    词表类参数（追加合并语义）：
    默认词表来自 PARAM_DEFAULTS，config.json params 中同名键的词会"追加"进去
    （去重保序）而不是整体覆盖——用户只需写想新增的词，不必抄全默认词表。
    """
    base = list(PARAM_DEFAULTS.get(key, []))
    extra = load_config().get("params", {}).get(key, [])
    if isinstance(extra, str):  # 允许只写一个词的字符串形式
        extra = [extra]
    for w in extra or []:
        if w not in base:
            base.append(w)
    return base
