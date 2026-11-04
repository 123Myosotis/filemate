# pipeline/stage3_processor/semantic_match.py
"""
语义匹配（P1）：列名对齐 + 筛选条件解析。

原则（与 synth_contract / block_cross_table 一致）：
- 本地确定优先：NFKC 宽度折叠 + casefold + 去空格；精确 → 包含 → 反向包含。
- 只有本地落空、且允许、且有 API Key 时，才批量问一次 LLM；返回列必须原样在候选里。
- 读不到 Key / 网络失败 / 模型乱回 → 回退本地结果，不抛异常，不影响主链路。

本模块只依赖标准库；ai.client 在 LLM 分支里懒加载，避免测试环境硬依赖 requests。
"""

from __future__ import annotations

import json
import re
import unicodedata

# 支持的操作符（与 table_engine._OPS 对齐；"包含" 归一为 contains）
_OPS = {
    "==": "==", "=": "==", "!=": "!=", ">": ">", "<": "<",
    ">=": ">=", "<=": "<=", "包含": "contains", "contains": "contains",
}
_COND_RE = re.compile(r"^(.+?)\s*(>=|<=|!=|==|=|>|<|包含|contains)\s*(.+)$")
_UNIT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(年|月|日|元|万|个|台|件|次|人)$")

# 列名对齐器提示词（严格契约：column 必须在候选里；confidence<0.5 必须给空）
_ALIGN_PROMPT = """你是表格列名对齐器。本地规则没能把用户提到的字段对齐到表格列，需要你判断最接近的列。

【候选列】
{columns}

【用户提到的字段】
{queries}

对每个用户字段，从候选列里选一列最匹配的（可多对一），或判定无匹配。
匹配依据：语义同义（「站点」≈「变电站」「站点名称」）、包含关系、缩写、常见错别字。

只输出一个 JSON 对象，禁止输出 JSON 以外的任何文字、不要 markdown 代码块：
{{"matches": [{{"query": "用户字段原文", "column": "候选列名", "confidence": 0.0}}]}}

硬规则：
1. column 必须原样来自【候选列】，禁止编造；无匹配时 column 给空字符串。
2. matches 必须覆盖所有用户字段，且顺序与输入一致。
3. confidence 低于 0.5 的，column 必须给空字符串（宁可不匹配，不许瞎配）。
"""


# ------------------------------------------------------------------
# 文本归一
# ------------------------------------------------------------------

def normalize_text(text) -> str:
    """NFKC 宽度折叠 + casefold + 去首尾空格；None → ""。"""
    if text is None:
        return ""
    return unicodedata.normalize("NFKC", str(text)).casefold().strip()


def _score(needle: str, hay: str) -> float:
    """包含匹配的分数：越接近整串越高。"""
    if not needle or not hay:
        return 0.0
    if needle == hay:
        return 1.0
    if needle in hay:
        return 0.5 + 0.5 * (len(needle) / len(hay))
    if hay in needle:
        return 0.4 + 0.4 * (len(hay) / len(needle))
    return 0.0


def match_column(columns, query):
    """列名对齐。返回 (列名|None, 分数)。本地确定，不调 LLM。"""
    q = normalize_text(query)
    if not q or not columns:
        return None, 0.0
    best, best_score = None, 0.0
    for col in columns or []:
        s = _score(q, normalize_text(col))
        if s > best_score:
            best, best_score = col, s
    if best is not None and best_score >= 0.5:
        return best, round(best_score, 3)
    return None, 0.0


# ------------------------------------------------------------------
# 条件解析
# ------------------------------------------------------------------

def strip_unit(value) -> str:
    """机械归一：2024年→2024、3台→3；其它原样。"""
    s = str(value or "").strip()
    m = _UNIT_RE.match(s)
    return m.group(1) if m else s


def _split_condition(cond):
    """拆出 (label, op, value)；结构不符返回 None。"""
    m = _COND_RE.match(str(cond or "").strip())
    if not m:
        return None
    op = _OPS.get(m.group(2))
    if not op:
        return None
    return m.group(1).strip(), op, m.group(3)


def parse_condition(cond, columns):
    """解析单个条件字符串 → {column, op, value, source} 或 None（本地机械解析）。"""
    parts = _split_condition(cond)
    if not parts:
        return None
    label, op, value = parts
    col, _ = match_column(columns, label)
    if not col:
        return None
    return {"column": col, "op": op, "value": strip_unit(value), "source": str(cond).strip()}


def _llm_enabled() -> bool:
    try:
        import config
        v = config.get_param("semantic_match.llm_fallback")
        return True if v is None else bool(v)
    except Exception:
        return True


def _chat_json(prompt, api_key):
    """懒加载 ai.client；失败返回 None。"""
    try:
        from ai.client import chat_json
        return chat_json(prompt, api_key, max_tokens=1200, temperature=0.0, timeout=30)
    except Exception:
        return None


def align_columns(columns, queries, api_key=None):
    """LLM 列名对齐（严格按 _ALIGN_PROMPT 契约）。

    返回与 queries 等长的 [{query, column|None, confidence}]：
    - column 必须原样在候选里，否则视为无匹配；
    - confidence < 0.5 视为无匹配；
    - 模型少给/顺序错 → 缺项按无匹配，不编造。
    无 Key / 网络失败 / JSON 非法 → 全部无匹配，不抛异常。
    """
    cols = [str(c) for c in (columns or [])]
    qs = [str(q) for q in (queries or [])]
    empty = [{"query": q, "column": None, "confidence": 0.0} for q in qs]
    if not cols or not qs:
        return empty
    if api_key is None:
        try:
            import config
            api_key = config.get_api_key()
        except Exception:
            api_key = ""
    if not api_key:
        return empty
    prompt = _ALIGN_PROMPT.format(
        columns=json.dumps(cols, ensure_ascii=False),
        queries=json.dumps(qs, ensure_ascii=False),
    )
    raw = _chat_json(prompt, api_key)
    rows = None
    if isinstance(raw, dict):
        rows = raw.get("matches")
        if rows is None and isinstance(raw.get("_list"), list):
            rows = raw["_list"]
    if not isinstance(rows, list):
        return empty
    out = []
    for i, q in enumerate(qs):
        col = None
        conf = 0.0
        if i < len(rows) and isinstance(rows[i], dict):
            r = rows[i]
            raw_col = str(r.get("column") or "").strip()
            try:
                conf = float(r.get("confidence") or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            if raw_col in cols and conf >= 0.5:
                col = raw_col
            else:
                conf = 0.0
        out.append({"query": q, "column": col, "confidence": conf})
    return out


def _embed_texts(texts):
    """按「向量来源」(dense_provider) 把一批文本 embed 成向量；不可用返回 None。

    - off      → 不用向量（回退本地字符串/LLM）
    - external → 调外部 embedding API
    - local    → 本地模型未接入 semantic_match（返回 None）
    """
    try:
        from pipeline.block_cross_table.memory_rag.dense_provider import (
            resolve_dense_provider,
        )
        if resolve_dense_provider().dense_provider != "external":
            return None
        from pipeline.block_cross_table.memory_rag.external_embedding_config import (
            resolve_external_embedding,
        )
        resolved = resolve_external_embedding()
        if not (resolved.api_key_present and resolved.base_url and resolved.model):
            return None
        from pipeline.block_cross_table.memory_rag.egress_audit import (
            assert_external_egress_allowed,
        )
        assert_external_egress_allowed(channel="embed")
        import requests
        url = resolved.base_url.rstrip("/") + "/embeddings"
        resp = requests.post(
            url,
            headers={"Authorization": "Bearer " + resolved.api_key,
                     "Content-Type": "application/json"},
            json={"model": resolved.model, "input": [str(t) for t in texts]},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        vecs = [d.get("embedding") for d in (data.get("data") or [])]
        if len(vecs) != len(texts) or not vecs or not vecs[0]:
            return None
        return [[float(x) for x in v] for v in vecs]
    except Exception:
        return None


def _cosine(a, b):
    import math
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(y * y for y in b))
    if not da or not db:
        return 0.0
    return num / (da * db)


_EMB_CACHE = {}


def vector_match(columns, query, *, threshold=0.6):
    """向量召回：返回 (column|None, score)。列向量按列名集合缓存；embedding 不可用返回 (None, 0.0)。"""
    if not columns or not query:
        return None, 0.0
    key = tuple(str(c) for c in columns)
    vecs = _EMB_CACHE.get(key)
    if vecs is None:
        emb = _embed_texts([str(c) for c in columns])
        if emb is None:
            return None, 0.0
        vecs = dict(zip(key, emb))
        _EMB_CACHE[key] = vecs
    qv = _embed_texts([str(query)])
    if qv is None:
        return None, 0.0
    q = qv[0]
    best, best_score = None, 0.0
    for col in key:
        s = _cosine(q, vecs[col])
        if s > best_score:
            best, best_score = col, s
    if best is not None and best_score >= threshold:
        return best, round(best_score, 3)
    return None, 0.0


def _llm_parse_conditions(failed, columns, api_key):
    """把本地解析不了的条件批量问一次 LLM；严格校验列在候选里，按下标回填 source。"""
    payload = {"columns": [str(c) for c in columns], "conditions": list(failed)}
    op_list = " ".join(sorted(set(_OPS.values())))
    body = (
        "你是表格筛选条件解析器。把 conditions 里每条自然语言条件解析成结构化条件。\n"
        "返回 JSON 对象 {\"conditions\": [{\"column\": \"\", \"op\": \"\", \"value\": \"\"}]}，"
        "顺序与输入一致。\n"
        "规则：\n"
        "1. column 必须从 columns 里原样选一列，禁止编造；\n"
        "2. op 只能是 {OPS} 之一；\n"
        "3. value 是纯值（2024 不要写成 2024年；A型号 保持原文）；\n"
        "4. 无法解析的条件对应项给 {\"column\": \"\", \"op\": \"\", \"value\": \"\"}。\n"
    )
    prompt = body.replace("{OPS}", op_list) + json.dumps(payload, ensure_ascii=False)
    raw = _chat_json(prompt, api_key)
    rows = None
    if isinstance(raw, dict):
        rows = raw.get("conditions")
        if rows is None and isinstance(raw.get("_list"), list):
            rows = raw["_list"]
    if not isinstance(rows, list):
        return []
    out = []
    for i, r in enumerate(rows):
        source = failed[i] if i < len(failed) else ""
        if not isinstance(r, dict):
            continue
        col, _ = match_column(columns, r.get("column"))
        op = str(r.get("op") or "").strip()
        value = strip_unit(r.get("value"))
        if not col or op not in _OPS.values() or value == "":
            continue
        out.append({"column": col, "op": op, "value": value, "source": source})
    return out


def parse_conditions(conditions, columns, api_key=None, allow_llm=None):
    """批量解析条件 → (parsed, notes)。

    - 本地先解析；能解析的直接用。
    - 结构可拆但列名对不上的：走 align_columns（严格对齐契约）补列名。
    - 连结构都拆不出的自由句式：走一次直接条件解析（_llm_parse_conditions）。
    - 仍未解析的写入 notes（调用方展示 / 跳过），不静默丢。
    """
    if allow_llm is None:
        allow_llm = _llm_enabled()
    conditions = [str(c) for c in (conditions or []) if str(c).strip()]
    parsed = []
    pending = []        # (source, label, op, value)：结构可拆、列名待对齐
    failed = []         # 结构性失败（连 op 都拆不出）
    label_failed = []   # 结构可拆，但列名对齐不上
    for c in conditions:
        p = parse_condition(c, columns)
        if p:
            parsed.append(p)
            continue
        parts = _split_condition(c)
        if parts:
            pending.append((c.strip(),) + parts)
        else:
            failed.append(c.strip())

    if pending:
        # 1) 向量召回（配了 embedding 才生效；否则返回 None，不影响后续）
        unresolved = []
        for source, label, op, value in pending:
            col, _ = vector_match(columns, label)
            if col:
                parsed.append({"column": col, "op": op,
                               "value": strip_unit(value), "source": source})
            else:
                unresolved.append((source, label, op, value))
        pending = unresolved

    if pending and allow_llm and api_key:
        labels = list(dict.fromkeys(p[1] for p in pending))
        aligned = align_columns(columns, labels, api_key=api_key)
        mapping = {a["query"]: a["column"] for a in aligned}
        for source, label, op, value in pending:
            col = mapping.get(label)
            if col:
                parsed.append({"column": col, "op": op,
                               "value": strip_unit(value), "source": source})
            else:
                label_failed.append(source)
    elif pending:
        label_failed.extend(p[0] for p in pending)

    if failed and allow_llm and api_key:
        got = _llm_parse_conditions(failed, columns, api_key)
        parsed.extend(got)
        used = {p["source"] for p in got}
        failed = [c for c in failed if c not in used]

    notes = ([f"筛选条件「{c}」无法解析，已跳过" for c in failed]
             + [f"筛选条件「{c}」的列在表中不存在，已跳过" for c in label_failed])
    return parsed, notes
