# agent_core/task_planner.py
"""
任务规划层（Agent 的"大脑"）：
用户指令 → 意图解析（8 维 JSON）→ 任务拆解（DAG JSON），全部由 AI 决策，本地只做结构校验。

- parse_and_plan：一次 AI 调用同时产出意图与任务 DAG（默认路径，省一轮请求）
- parse_intent / plan_tasks：单独的两步（蓝图标准流程，供测试与调试）
- AI 只输出严格 JSON；本地只做白名单规范化（能力名、依赖、task_id 唯一性）
- 无 Key / 网络失败 / JSON 连续非法 → 返回 None，由调用方明确提示（不走本地规则兜底）
"""
import json
import time

import config
from agent_core.capabilities import CAPABILITIES

# ------------------------------------------------------------------
# 系统级提示词（全局设定，决定 Agent 行为基调）
# ------------------------------------------------------------------

SYSTEM_PROMPT = """你是 FileMate 智能数据助手，核心能力是根据用户指令自动拆解任务并调用能力执行。

【行为准则】
1. 你是一个执行者，不是咨询顾问。用户让你做什么，你就直接去做，不要先给方案。
2. 除非用户明确问"应该怎么做"或"有什么建议"，否则一律按执行模式规划任务。
3. 执行模式意味着：拆解任务 → 调用能力 → 输出结果，中间不输出方案或计划。
4. 如果用户指令中包含"不要给方案""直接做""直接操作"等关键词，必须按执行处理。
5. 如果用户指令不明确，优先按最合理的执行路径推进，而不是停下来问用户。

【能力列表】（task 的 capability 字段只能从这里选）
- scan：扫描指定目录下的所有文件
- parse：解析 txt/word/excel/csv/pdf 等文件为文本或表格
- vision：对图片和扫描件进行内容理解（当前环境可能降级为标注跳过）
- classify：判断文档类型（technical/novel/guide/project/contract/academic）
- split：对大文本分块，防止 token 超限
- ai_call：调用 AI 对文本块做结构化抽取（实体/事实/表格）
- analyze：对全文做摘要/要点/结论/建议
- table_query：表格联查/筛选/排序/聚合，或从知识库检索已处理数据（仅机械筛选已有列）
- table_synthesize：按用户口述规则动态合成新表（映射/自定义输出列）；凡「导出新表/自定义列」必须用它，禁止写死业务
- file_organize：移动、归类文件（目标清单由系统编译给出）
- export：严格按 output_format 导出（word→docx，excel→xlsx，csv→csv，text→不落盘；禁止擅自改格式）
- review：独立验证门禁（失败回滚重跑，不能只靠模型自评）"""

# ------------------------------------------------------------------
# 意图解析提示词（8 维 JSON，蓝图标准）
# ------------------------------------------------------------------

_INTENT_PROMPT = """请解析以下用户指令，输出 JSON 格式的解析结果。

解析维度：
1. operation_type：content_organize（整理文件内容：提取数据/关联信息/生成报告）
   | file_organize（整理文件本身：移动/归类/重命名）| query（查询已处理的数据）| other
   —— query 仅用于"查之前已整理入库的数据"；只要指令是针对当前目录文件做提取/筛选/统计/出报告，一律算 content_organize
2. execution_mode：execute（执行模式）| consult（咨询模式）
   —— "帮我把/直接/执行/操作/运行/不要给方案/不用解释/直接做" → execute；
      "怎么/如何/建议/方案/步骤" → consult；没有明显倾向默认 execute。
3. file_scope：文件范围描述（指定文件夹/指定文件/全部文件/模糊描述）
4. target_object：用户想找的数据或物品是什么（如"A型号设备""客户名单"）
5. filter_conditions：筛选条件数组，尽量写成 "列名=值" / "列名>值" / "列名包含值" 形式
6. sort_requirement：{"field": "排序字段", "order": "asc|desc"}，无排序要求给 {}
7. output_format：text | word | excel | csv | ppt | ""（未指定给 ""；要表格文件时优先 excel）
8. constraints：用户特别强调的要求数组（如"不要给方案"）

用户指令：{user_input}

只输出 JSON 对象，禁止输出 JSON 以外的任何文字：
{{"operation_type":"","execution_mode":"execute","file_scope":"","target_object":"",
"filter_conditions":[],"sort_requirement":{{}},"output_format":"","constraints":[]}}"""

# ------------------------------------------------------------------
# 任务拆解提示词（DAG JSON）
# ------------------------------------------------------------------

_PLAN_PROMPT = """{system_prompt}

请将以下用户指令拆解为可执行的子任务列表（DAG）。

【用户指令】{user_input}

【意图解析结果】{intent_json}

【当前工作目录内的文件清单】（文件名必须原样照抄，不得编造）
{file_list}

【系统已编译好的文件移动清单】（file_organize 任务直接使用，moves 为空则不要排 file_organize）
{moves_summary}

【拆解要求】
每个子任务包含：
- task_id：任务编号（英文，如 scan_1 / parse_a / export_1）
- task_name：任务名称（中文简述）
- capability：从能力列表选择
- dependencies：依赖的前置 task_id 列表（无依赖给 []）
- parallel_possible：与其他任务无依赖时可否并行（true/false）
- input：任务输入。parse/classify/split/ai_call 可按文件拆分：{{"file": "精确文件名"}}
- expected_output：期望的输出格式简述

【编排规则】
- 依赖关系：B 依赖 A 的结果 → B 的 dependencies 含 A
- 独立关系：互不依赖 → parallel_possible 标 true（系统会并行执行）
- 合并关系：需要多个前置结果的（如 export/review）→ dependencies 列全前置任务
- 多文件处理时，parse 应按文件拆成多个可并行任务；classify/split/ai_call 同理
- 最后必须排一个 review 任务兜底复核
- 纯文件整理（只有移动清单）只需 file_organize + review
- 查询类指令（operation_type=query）只需 table_query + review
- 若同时存在内容处理与 file_organize，file_organize 必须依赖全部内容类任务（移动前要先读到内容）
- **文档内容整理（非表格合成）**：scan → parse → classify → split → ai_call → analyze → export → review
- **表格动态合成/导出 Excel·CSV（用户给映射规则、自定义列、要新表）**：
  scan → parse → **table_synthesize** → export → review
  —— output_format 必须设为 excel 或 csv；不要走 classify/split/ai_call/analyze
- **仅机械筛选已有列**：scan → parse → table_query → export → review
- 按执行顺序排列任务

只输出 JSON 对象：
{{"tasks":[{{"task_id":"","task_name":"","capability":"scan","dependencies":[],
"parallel_possible":false,"input":{{}},"expected_output":""}}]}}"""

# ------------------------------------------------------------------
# 合并提示词（默认路径：一次调用同时产出意图 + 任务 DAG）
# ------------------------------------------------------------------

_COMBINED_PROMPT = """{system_prompt}

请完成两件事：1) 按 8 个维度解析用户意图；2) 把用户指令拆解为可执行的任务 DAG。

【用户指令】{user_input}

【当前工作目录内的文件清单】（文件名必须原样照抄，不得编造）
{file_list}

【表格文件的真实列名】（filter_conditions 和 sort_requirement 必须从这里引用列名，严禁臆造）
{table_hints}

【系统已编译好的文件移动清单】（file_organize 任务直接使用，moves 为空则不要排 file_organize）
{moves_summary}

【意图解析维度】
operation_type：content_organize|file_organize|query|other
  —— query 仅用于"查之前已整理入库的数据"（如"查一下华科的采购记录"且目录里没有相关文件）；
     只要指令是针对当前目录文件做提取/筛选/统计/出报告，一律算 content_organize
execution_mode：execute|consult（"不要给方案/直接做"等 → execute；"怎么/如何/建议/方案" → consult；默认 execute）
file_scope / target_object / filter_conditions（"列名=值" 形式数组，值写纯值：2024 不要写成 2024年；
  文本类值优先用"列名包含值"，如 设备型号包含A型号；数值/年份用"列名=值"）
sort_requirement（{{"field":"","order":"asc|desc"}}，无则 {{}}）
output_format：text|word|excel|csv|ppt|""    constraints：特别要求数组
  —— 用户要「导出表格/Excel/CSV/新表」等 → output_format=excel（或 csv）

【任务拆解要求】
- capability 只能从能力列表选：{capabilities}
- 依赖进 dependencies；无依赖且互不干扰标 parallel_possible=true
- 多文件时 parse/classify/split/ai_call 按文件拆成多个并行任务，input 填 {{"file":"精确文件名"}}；
  但文件超过 5 个时不要逐文件拆，每类能力只排一个任务（input 留空 {{}}，系统内部会批量处理），避免任务列表过长
- 合并型任务（export/review）的 dependencies 列全前置任务
- 最后必须排 review 任务；纯移动只需 file_organize+review；查询只需 table_query+review
- 若同时存在内容处理与 file_organize，file_organize 必须依赖全部内容类任务（移动前要先读到内容）
- **文档内容整理**：scan → parse → classify → split → ai_call → analyze → export → review；默认 word
- **表格动态合成（口述映射、自定义输出列、归属/评价/问题汇总、导出新表）**：
  scan → parse → table_synthesize → export → review；output_format 必须为 excel 或 csv
  —— 禁止把这类需求拆成只会筛已有列的 table_query
- **仅机械筛选已有列**：scan → parse → table_query → export → review
- 按执行顺序排列

只输出 JSON 对象：
{{"intent":{{"operation_type":"","execution_mode":"execute","file_scope":"","target_object":"",
"filter_conditions":[],"sort_requirement":{{}},"output_format":"","constraints":[]}},
"tasks":[{{"task_id":"","task_name":"","capability":"scan","dependencies":[],
"parallel_possible":false,"input":{{}},"expected_output":""}}]}}"""

_VALID_OPS = {"content_organize", "file_organize", "query", "other"}
_VALID_MODES = {"execute", "consult"}
_VALID_FMTS = {"text", "word", "excel", "csv", "ppt", ""}


# ------------------------------------------------------------------
# 对外接口
# ------------------------------------------------------------------

def parse_intent(user_input: str, api_key: str = None):
    """意图解析（蓝图第一步）。返回 8 维 dict；失败返回 None。"""
    api_key = api_key if api_key is not None else config.get_api_key()
    if not api_key:
        return None
    data = _call_json(_INTENT_PROMPT.format(user_input=user_input), api_key)
    return normalize_intent(data) if data is not None else None


def plan_tasks(user_input: str, intent: dict, file_names: list,
               moves_summary: str = "（无）", api_key: str = None):
    """任务拆解（蓝图第二步）。返回任务列表；失败返回 None。"""
    api_key = api_key if api_key is not None else config.get_api_key()
    if not api_key:
        return None
    prompt = _PLAN_PROMPT.format(
        system_prompt=SYSTEM_PROMPT, user_input=user_input,
        intent_json=json.dumps(intent, ensure_ascii=False),
        file_list=_fmt_files(file_names), moves_summary=moves_summary)
    data = _call_json(prompt, api_key)
    if data is None:
        return None
    tasks = data.get("tasks") if isinstance(data, dict) else None
    return normalize_tasks(tasks)


def parse_and_plan(user_input: str, file_names: list, moves_summary: str = "（无）",
                   api_key: str = None, table_hints: str = ""):
    """默认路径：一次 AI 调用同时产出意图与任务 DAG。返回 {"intent", "tasks"}；失败 None。
    table_hints: 表格文件的真实列名提示（供 filter_conditions 精确引用，禁止臆造列名）。"""
    api_key = api_key if api_key is not None else config.get_api_key()
    if not api_key:
        return None
    prompt = _COMBINED_PROMPT.format(
        system_prompt=SYSTEM_PROMPT, user_input=user_input,
        file_list=_fmt_files(file_names), moves_summary=moves_summary,
        capabilities="/".join(CAPABILITIES), table_hints=table_hints or "（无表格文件）")
    data = _call_json(prompt, api_key)
    if data is None or not isinstance(data, dict):
        return None
    intent = normalize_intent(data.get("intent"))
    tasks = normalize_tasks(data.get("tasks"))
    return {"intent": intent, "tasks": tasks}


# ------------------------------------------------------------------
# 规范化（白名单校验：能力名/依赖/task_id 唯一；非法项剔除而不是炸掉）
# ------------------------------------------------------------------

def normalize_intent(data) -> dict:
    """8 维意图规范化：非法值回退默认，绝不把脏数据交给下游"""
    data = data if isinstance(data, dict) else {}
    op = data.get("operation_type")
    mode = data.get("execution_mode")
    fmt = data.get("output_format")
    fmt_norm = str(fmt).lower() if str(fmt or "").lower() in _VALID_FMTS else ""
    # 不擅自默认 word/excel：空格式表示「按语义再解析 / 文本交付」
    op = op if op in _VALID_OPS else "other"
    sort_req = data.get("sort_requirement")
    return {
        "operation_type": op,
        "execution_mode": mode if mode in _VALID_MODES else "execute",
        "file_scope": str(data.get("file_scope") or ""),
        "target_object": str(data.get("target_object") or ""),
        "filter_conditions": [str(c) for c in data.get("filter_conditions") or []
                              if str(c).strip()],
        "sort_requirement": sort_req if isinstance(sort_req, dict) else {},
        "output_format": fmt_norm,
        "constraints": [str(c) for c in data.get("constraints") or [] if str(c).strip()],
    }


def normalize_tasks(tasks) -> list:
    """任务列表规范化：能力白名单、依赖合法化、task_id 去重、input 统一为 dict"""
    if not isinstance(tasks, list):
        return []
    out, seen = [], set()
    for i, t in enumerate(tasks, 1):
        if not isinstance(t, dict):
            continue
        cap = t.get("capability")
        if cap not in CAPABILITIES:
            continue  # 未注册的能力直接剔除（蓝图：留 other 兜底由 review 兜住）
        tid = str(t.get("task_id") or f"task_{i}").strip() or f"task_{i}"
        base, n = tid, 2
        while tid in seen:
            tid = f"{base}_{n}"
            n += 1
        seen.add(tid)
        inp = t.get("input")
        out.append({
            "task_id": tid,
            "task_name": str(t.get("task_name") or cap)[:int(
                config.get_param("runner.max_task_name_len"))],
            "capability": cap,
            "dependencies": [str(d) for d in t.get("dependencies") or []
                             if isinstance(d, (str, int))],
            "parallel_possible": bool(t.get("parallel_possible")),
            "input": inp if isinstance(inp, dict) else ({"file": inp} if inp else {}),
            "expected_output": str(t.get("expected_output") or ""),
        })
    # 依赖里指向不存在任务/自身的边剔除，防止拓扑死锁
    valid_ids = {t["task_id"] for t in out}
    for t in out:
        t["dependencies"] = [d for d in t["dependencies"]
                             if d in valid_ids and d != t["task_id"]]
    return out


# ------------------------------------------------------------------
# AI 调用（JSON 模式 + 重试；与 intent_router 同款可靠性策略）
# ------------------------------------------------------------------

LAST_ERROR = ""  # 最近一次规划失败的具体原因（供上层展示/排查，不静默）


def _call_json(prompt: str, api_key: str):
    global LAST_ERROR
    from ai.client import chat_json
    max_retries = int(config.get_param("planner.max_retries"))
    waits = list(config.get_param("planner.retry_waits"))
    for attempt in range(max_retries):
        data = chat_json(
            prompt, api_key,
            max_tokens=int(config.get_param("planner.max_tokens")),
            temperature=config.get_param("planner.temperature"),
            timeout=int(config.get_param("planner.timeout")),
        )
        if data is not None:
            return data
        LAST_ERROR = f"第{attempt + 1}次尝试失败：模型未返回合法 JSON"
        print(f"[task_planner] {LAST_ERROR}")
        if attempt < max_retries - 1:
            time.sleep(waits[min(attempt, len(waits) - 1)])
    return None


def _fmt_files(file_names: list) -> str:
    if not file_names:
        return "（空文件夹）"
    limit = int(config.get_param("router.max_list_files"))
    names = list(file_names)[:limit]
    body = "\n".join(f"{i}. {n}" for i, n in enumerate(names, 1))
    if len(file_names) > limit:
        body += f"\n...（共 {len(file_names)} 个，仅列前 {limit} 个）"
    return body


def moves_to_summary(moves: list) -> str:
    """把编译好的 (src, dst) 清单转成给 AI 看的简述"""
    if not moves:
        return "（无）"
    return "\n".join(f"· {s.name} → {d.parent.name}/{d.name}" for s, d in moves[:50])
