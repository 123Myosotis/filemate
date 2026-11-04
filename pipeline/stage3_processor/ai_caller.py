# pipeline/stage3_processor/ai_caller.py
"""
Stage3 AI 抽取器：单块内容 → 结构化 JSON（实体/事实/表格）。
可靠性策略（方案 Stage3 要求）：
- 最多 3 次重试，等待 2s/4s/6s 递增
- 触发条件：超时 / API 错误 / 输出不是合法 JSON / 缺少必需字段
- 连续失败 → 降级为摘要（degraded）；摘要也失败 → failed，记录 error
- 失败块不静默：status 与 error 一直保留到统一 JSON，供复核层重跑
未配置 API Key 时走本地截断摘要（degraded），保证管线仍然可用。
"""
import json  # 解析 AI 输出
import re  # 剥掉 ```json 围栏
import time  # 重试等待
import requests  # HTTP
from requests.exceptions import RequestException

import config  # 可调参数统一从 config.json 的 params 段读取（严禁写死）


def _extract_char_limit() -> int:
    """抽取/全文分析的输入上限：默认顶到窗口填充比，params 显式写了才用更小值。"""
    from ai.context_budget import effective_input_chars
    return effective_input_chars(
        explicit_key="extract.prompt_chars",
        reserved_output=int(config.get_param("extract.max_tokens")),
    )


def _summary_char_limit() -> int:
    from ai.context_budget import effective_input_chars
    return effective_input_chars(
        explicit_key="extract.summary_chars",
        reserved_output=int(config.get_param("extract.max_tokens")),
    )

# 要求 AI 输出的块级 JSON 结构（字段与方案一致）
_PROMPT_TEMPLATE = """你是文档结构化抽取引擎。阅读以下属于《{doc_type}》类型文档的片段（片段编号 {chunk_id}，所属章节：{section_path}），抽取结构化信息。

只输出一个 JSON 对象，不要输出任何其他文字，结构如下：
{{
  "entities": [{{"name": "实体名", "type": "人物|组织|地点|概念|技术|其他"}}],
  "facts": [{{"subject": "主语", "predicate": "关系/属性", "object": "宾语/值", "confidence": 0.9}}],
  "tables": [{{"name": "表格名", "columns": ["列1"], "rows": [{{"列1": "值"}}]}}]
}}
要求：
- entities 最多 10 条，去重；facts 最多 15 条，confidence 为 0~1 的小数
- 数值/日期/金额保持原文写法，禁止改写或换算
- 片段中没有表格则 tables 为空数组；没有把握的不要编造
- 片段可能以"……"开头，那是上一块的重叠上下文，不必为其生成条目

文档片段：
{content}"""


def call_ai_json(chunk: dict, doc_type: str, api_key: str) -> dict:
    """
    单块结构化抽取入口。chunk 需含 chunk_id/section_path/content。
    返回块级结果 dict：{chunk_id, section_path, status: ok|degraded|failed,
                       entities, facts, tables, error}
    """
    result = _blank_result(chunk)
    if not api_key:
        # 未配置 Key：本地截断摘要降级，管线继续走，绝不中断
        result["status"] = "degraded"
        result["summary"] = _local_summary(chunk["content"])
        result["error"] = "未配置 API Key，已降级为本地摘要"
        return result

    prompt = _PROMPT_TEMPLATE.format(
        doc_type=doc_type, chunk_id=chunk["chunk_id"],
        section_path=chunk["section_path"],
        content=chunk["content"][:_extract_char_limit()],
    )
    # 重试策略全部参数化：次数、等待序列、超时、温度、输出上限
    max_retries = int(config.get_param("extract.max_retries"))
    waits = list(config.get_param("extract.retry_waits"))
    timeout = int(config.get_param("extract.timeout"))
    temperature = config.get_param("extract.temperature")
    max_tokens = int(config.get_param("extract.max_tokens"))

    from ai.client import chat_completion
    last_err = ""
    for attempt in range(max_retries):
        try:
            resp = chat_completion(
                [{"role": "user", "content": prompt}],
                api_key,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={"type": "json_object"},
                timeout=timeout,
            )
            content = resp["choices"][0]["message"]["content"]
            data = _parse_json(content)
            return _fill_result(result, data)
        except (RequestException, KeyError, ValueError) as e:
            last_err = f"第{attempt + 1}次尝试失败：{e}"
            if attempt < max_retries - 1:
                time.sleep(waits[min(attempt, len(waits) - 1)])

    return _degrade(chunk, api_key, result, last_err)


# ------------------------------------------------------------------
# 内部工具
# ------------------------------------------------------------------

def _blank_result(chunk: dict) -> dict:
    return {
        "chunk_id": chunk["chunk_id"],
        "section_path": chunk["section_path"],
        "status": "ok",
        "entities": [], "facts": [], "tables": [],
        "summary": "", "error": "",
    }


def _parse_json(text: str) -> dict:
    """剥掉 ```json 围栏后解析；不是 dict 或解析失败抛 ValueError（触发重试）"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)  # 剥围栏
    if m:
        text = m.group(1).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("AI 输出不是 JSON 对象")
    return data


def _fill_result(result: dict, data: dict) -> dict:
    """把 AI 输出规整进统一块结构；缺失字段给默认值，条数上限走参数"""
    max_entities = int(config.get_param("extract.max_entities"))
    max_facts = int(config.get_param("extract.max_facts"))
    result["status"] = data.get("status", "ok") if data.get("status") in ("ok", "degraded") else "ok"

    entities = []
    for e in data.get("entities", []) or []:
        if isinstance(e, dict) and e.get("name"):
            entities.append({"name": str(e["name"]).strip(),
                             "type": str(e.get("type", "其他")).strip() or "其他"})
    result["entities"] = entities[:max_entities]

    facts = []
    for f in data.get("facts", []) or []:
        if isinstance(f, dict) and f.get("subject") and f.get("predicate"):
            try:
                conf = float(f.get("confidence", 0.8))
            except (TypeError, ValueError):
                conf = 0.8
            facts.append({"subject": str(f["subject"]).strip(),
                          "predicate": str(f["predicate"]).strip(),
                          "object": str(f.get("object", "")).strip(),
                          "confidence": max(0.0, min(conf, 1.0))})
    result["facts"] = facts[:max_facts]

    tables = []
    for t in data.get("tables", []) or []:
        if isinstance(t, dict) and t.get("columns") and t.get("rows"):
            tables.append({"name": str(t.get("name", "表格")),
                           "columns": [str(c) for c in t["columns"]],
                           "rows": [r if isinstance(r, dict) else {} for r in t["rows"]]})
    result["tables"] = tables
    return result


def _degrade(chunk: dict, api_key: str, result: dict, last_err: str) -> dict:
    """三次重试失败 → 降级为 AI 摘要；摘要也失败 → failed（记录 error，不静默）"""
    from ai.client import ask_deepseek  # 延迟导入，避免循环依赖
    try:
        summary = ask_deepseek(
            f"请用 200 字以内客观总结以下文档片段的关键信息：\n\n"
            f"{chunk['content'][:_summary_char_limit()]}",
            api_key,
        )
    except Exception as e:  # 网络层抛异常也兜住
        summary = f"摘要生成失败：{e}"
    if summary.startswith(("请求失败", "响应解析异常")) or not summary.strip():
        result["status"] = "failed"
        result["error"] = f"抽取与摘要均失败。{last_err}"
        return result
    result["status"] = "degraded"
    result["summary"] = summary.strip()
    result["error"] = last_err  # 保留原始失败原因，复核层可见
    return result


def _local_summary(content: str) -> str:
    """无 Key 时的本地摘要：取前 150 字 + 末 50 字"""
    content = re.sub(r"\s+", " ", content).strip()
    if len(content) <= 200:
        return content
    return content[:150] + "……" + content[-50:]


# ------------------------------------------------------------------
# 全文分析（总结/要点/结论/建议）
# ------------------------------------------------------------------

_ANALYSIS_PROMPT = """你是文档分析助手。请通读以下《{doc_type}》类型文档的全文，输出结构化分析。

只输出一个 JSON 对象，不要输出任何其他文字，结构如下：
{{
  "summary": "200字以内的全文摘要，说明文档主题与核心内容",
  "key_points": ["要点1", "要点2", "要点3"],
  "conclusions": ["结论1", "结论2"],
  "recommendations": ["建议1", "建议2"]
}}
要求：
- key_points 最多 8 条，conclusions 最多 5 条，recommendations 最多 5 条
- 客观基于原文，不要编造文档中没有的信息
- 若文档不适合某项（如纯技术文档无需建议），对应字段返回空数组

文档全文：
{content}"""


def analyze_document(text: str, doc_type: str, api_key: str) -> dict:
    """对文档全文做分析：摘要/要点/结论/建议。
    无 API Key 时降级为本地截断摘要。
    返回 {summary, key_points[], conclusions[], recommendations[], status, error}"""
    blank = {"summary": "", "key_points": [], "conclusions": [],
             "recommendations": [], "status": "ok", "error": ""}
    if not text or not text.strip():
        blank["status"] = "failed"
        blank["error"] = "文档无文本内容"
        return blank
    if not api_key:
        blank["status"] = "degraded"
        blank["summary"] = _local_summary(text)
        blank["error"] = "未配置 API Key，已降级为本地摘要"
        return blank

    prompt = _ANALYSIS_PROMPT.format(
        doc_type=doc_type,
        content=text[:_extract_char_limit()],
    )
    max_retries = int(config.get_param("extract.max_retries"))
    waits = list(config.get_param("extract.retry_waits"))
    timeout = int(config.get_param("extract.timeout"))
    from ai.client import chat_completion
    last_err = ""
    for attempt in range(max_retries):
        try:
            resp = chat_completion(
                [{"role": "user", "content": prompt}],
                api_key,
                max_tokens=int(config.get_param("extract.max_tokens")),
                temperature=config.get_param("extract.temperature"),
                response_format={"type": "json_object"},
                timeout=timeout,
            )
            content = resp["choices"][0]["message"]["content"]
            data = _parse_json(content)
            blank["summary"] = str(data.get("summary", "")).strip()
            blank["key_points"] = [str(x).strip() for x in (data.get("key_points") or []) if str(x).strip()]
            blank["conclusions"] = [str(x).strip() for x in (data.get("conclusions") or []) if str(x).strip()]
            blank["recommendations"] = [str(x).strip() for x in (data.get("recommendations") or []) if str(x).strip()]
            return blank
        except (RequestException, KeyError, ValueError) as e:
            last_err = f"第{attempt + 1}次尝试失败：{e}"
            if attempt < max_retries - 1:
                time.sleep(waits[min(attempt, len(waits) - 1)])
    blank["status"] = "degraded"
    blank["summary"] = _local_summary(text)
    blank["error"] = last_err
    return blank
