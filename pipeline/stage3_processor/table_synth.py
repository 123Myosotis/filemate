# pipeline/stage3_processor/table_synth.py
"""
动态表格合成：根据用户自然语言指令，由 LLM 生成「合成方案」，再由本地机械执行。

设计原则：
- 不写死业务映射；列、取值、类别全部来自当次指令和表结构
- LLM 只产出可执行 JSON 方案；本地负责 join / map / filter / project / 导出列
- 任意目录、任意表格、任意输出列需求，走同一套能力
"""
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import config
from ai.client import chat_json
from pipeline.stage3_processor import table_engine

def _fit_model_text(text: str, reserved_output: int = None) -> str:
    """送进模型的文本顶到当前窗口预算，而不是固定几千字。"""
    from ai.context_budget import effective_input_chars
    if reserved_output is None:
        reserved_output = int(config.get_param("synth.max_tokens") or 0)
    return (text or "")[:effective_input_chars(reserved_output=reserved_output)]


def _structure_max_tokens(n_blocks: int) -> int:
    """结构判断的输出额度随块数变；用户在 params 里写死了上限时才不再放大。"""
    floor = int(config.get_param("synth.ai_bind_max_tokens") or 0)
    if config.param_is_user_set("synth.ai_bind_max_tokens"):
        return max(1, floor)
    per = int(config.get_param("synth.structure_tokens_per_block") or 1)
    ceiling = int(config.get_param("api.max_tokens") or floor or 1)
    return max(1, min(ceiling, max(floor, per * max(1, n_blocks))))


_SYNTH_PROMPT = """你是表格合成规划器。根据用户指令和现有表格结构，输出一份可被程序机械执行的合成方案 JSON。
本地只做契约校验与机械执行：不模糊匹配、不自动向下填充、不翻译颜色、不按行数选主表。
列名、目录 ID、坐标、填充规则必须来自你的输出；缺字段会被整批拒绝。

【用户指令】
{user_input}

【可用表格】（name / columns / sample_rows / 部分列去重样例）
{schema}

【契约版本】必须原样回传：
  contract_version = "{contract_version}"
  contract_hash = "{contract_hash}"

【DSL 顺序（每步须带 in_scope / out_scope；下游 in_scope 被上游 out_scope 覆盖）】
read_range → apply_fills → drop_rows → project → align → filter → derive → aggregate → reconcile

【输出 JSON】只输出一个对象（字段名固定）：
{{
  "contract_version": "{contract_version}",
  "contract_hash": "{contract_hash}",
  "status": "ready",
  "required_judgment_types": ["binding", "join"],
  "output_columns": ["列A", "列B"],
  "primary_table": "源表文件名（必须来自可用表格）",
  "catalog": [
    {{"catalog_id": "#1", "field_name": "列A",
      "scope": {{"file_id": "文件", "sheet": "表", "region": "r0"}}}}
  ],
  "bindings": [
    {{
      "scope": {{"file_id": "文件", "sheet": "表", "region": "r0"}},
      "source_file": "文件", "source_sheet": "表",
      "header_path": [], "orig_col_name": "源列",
      "cell_coord_or_range": "A1",
      "target_field_id": "#1", "target_field_name": "列A",
      "confidence": 0.9, "ambiguous_reason": "", "alternatives": []
    }}
  ],
  "contract_joins": [],
  "regions": [],
  "sheet_data_coords": [],
  "dsl_steps": [],
  "judgments": [],
  "fills": [],
  "joins": [],
  "column_bindings": {{}},
  "value_mappings": [],
  "derived_columns": [],
  "keep_row_when": [],
  "dedupe_by": [],
  "group_by": [],
  "notes": "一句话"
}}

规则：
1. 必须输出 contract_version 与 contract_hash，且与上方给定值完全一致。
2. status=ready 才可执行；有冲突时 status=await_user 并给出冲突项，不要 silently 改表。
3. output_columns 覆盖用户点名的全部输出列；绑定用 bindings[].target_field_id（目录 ID）。
4. 空单元格保持空，除非 fills 给出带坐标与 fill_source_coord 的填充规则。
5. 关联键、筛选、分组、度量只引用 catalog_id；join 用 contract_joins（含左右 scope、catalog_id、join_type、expected_cardinality、grain）。
6. 不要编造源表中不存在的列名；不要输出模糊匹配/色名翻译规则。
"""


def _profile_rows(rows: list) -> list:
    """大表只做步进取样，避免为了画像扫完十几万行。"""
    cap = int(config.get_param("synth.profile_scan_rows") or 0)
    if cap <= 0 or len(rows) <= cap:
        return rows
    step = max(1, len(rows) // cap)
    return rows[::step][:cap]


def build_schema_blurb(tables: dict, sample_n: int = None, max_dist: int = None) -> str:
    """把多表压缩成给模型看的结构摘要（不灌全量数据）。"""
    if sample_n is None:
        sample_n = int(config.get_param("synth.sample_rows"))
    if max_dist is None:
        max_dist = int(config.get_param("synth.max_distinct"))
    parts = []
    for name, t in tables.items():
        cols = t.get("columns") or []
        rows = t.get("rows") or []
        scan = _profile_rows(rows)
        lines = [f"### 表名: {name}", f"列: {', '.join(cols)}", f"行数: {len(rows)}"]
        if len(scan) != len(rows):
            lines.append(f"画像抽样: {len(scan)}/{len(rows)} 行（步进，非全表灌入）")
        # 短文本列给去重样例，帮助模型选筛选列/映射列
        for col in cols[:12]:
            vals, seen = [], set()
            for r in scan:
                v = str(r.get(col, "")).strip()
                if not v or v in seen:
                    continue
                seen.add(v)
                vals.append(v)
                if len(vals) >= max_dist:
                    break
            if vals and len(vals) <= max_dist:
                preview = "、".join(vals[:15])
                if len(vals) > 15:
                    preview += f"…(共{len(vals)}种)"
                lines.append(f"  · {col} 样例: {preview}")
        for i, r in enumerate(scan[:sample_n], 1):
            slim = {c: r.get(c, "") for c in cols}
            lines.append(f"  样例行{i}: {json.dumps(slim, ensure_ascii=False)}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts) if parts else "（无表格）"


def _plan_budget_chars() -> int:
    from ai.context_budget import effective_input_chars
    return effective_input_chars(
        explicit_key="synth.max_plan_chars",
        reserved_output=int(config.get_param("synth.max_tokens")),
    )


def _schema_index(tables: dict, max_cols: int = None) -> str:
    """每批都带上的全局目录：让模型看见没展开的大表/小表，才能写跨表关联。"""
    if max_cols is None:
        max_cols = int(config.get_param("synth.index_max_cols") or 12)
    max_cols = max(1, max_cols)
    lines = ["### 全部表目录（关联时以这里的表名为准，未展开的表同样存在）"]
    for name, t in tables.items():
        cols = t.get("columns") or []
        nrows = len(t.get("rows") or [])
        shown = ", ".join(str(c) for c in cols[:max_cols])
        if len(cols) > max_cols:
            shown += f"…(+{len(cols) - max_cols})"
        lines.append(f"- {name} | 行数 {nrows} | 列: {shown}")
    return "\n".join(lines)


def _minimal_blurb(name: str, table: dict) -> str:
    """再瘦也不能丢的表卡：表名+列+行数，保证分批后仍能点名关联。"""
    cols = table.get("columns") or []
    nrows = len(table.get("rows") or [])
    return (
        f"### 表名: {name}\n列: {', '.join(str(c) for c in cols)}\n"
        f"行数: {nrows}\n（行样例已省略，本地仍按全表执行）"
    )


def _shrink_blurb(name: str, table: dict, room: int) -> str:
    """单表摘要塞不进剩余预算时，逐级减少样例，最后只留列名与行数。"""
    room = max(0, int(room))
    for sample_n, max_dist in ((2, 8), (1, 3), (0, 0)):
        text = build_schema_blurb({name: table}, sample_n=sample_n, max_dist=max_dist)
        if len(text) <= room:
            return text
    head = _minimal_blurb(name, table)
    if len(head) <= room:
        return head
    # 列太多时再压列名列表，禁止截断半截导致表名丢失
    cols = table.get("columns") or []
    nrows = len(table.get("rows") or [])
    for n in (min(len(cols), 24), 12, 6, 3, 1, 0):
        shown = ", ".join(str(c) for c in cols[:n])
        if len(cols) > n:
            shown += f"…(+{len(cols) - n})"
        cand = f"### 表名: {name}\n列: {shown}\n行数: {nrows}"
        if len(cand) <= room:
            return cand
    stub = f"### 表名: {name}\n行数: {nrows}"
    return stub if len(stub) <= room else f"### 表名: {name}"[:room]


def pack_schema_batches(tables: dict, budget_chars: int = None) -> list:
    """
    按字符预算装包，而不是按固定张数。

    - 全部摘要加起来没到预算：一次送完，不切。
    - 小表整包钉在每一批（联系人册、值班表这类几 KB 的表不会和几 MB 主表拆散）。
    - 大表只占剩余预算；仍放不下就收缩样例。
    - 每一批都带全局表目录，被分到别批的表仍能被点名关联。
    - 每张大表至少在某一批里以完整表卡（列名级）出现，禁止预算挤爆后只剩目录半截。
    """
    if not tables:
        return ["（无表格）"]
    budget = int(budget_chars if budget_chars is not None else _plan_budget_chars())
    budget = max(budget, 1)
    items = []
    for name, t in tables.items():
        blurb = build_schema_blurb({name: t})
        items.append({"name": name, "table": t, "blurb": blurb, "n": len(blurb)})
    index = _schema_index(tables)
    # 目录本身超预算时压列数，避免目录吃光整窗
    if len(index) >= budget:
        for mc in (8, 4, 2, 1):
            index = _schema_index(tables, max_cols=mc)
            if len(index) < budget:
                break
    if len(index) + 2 + sum(i["n"] for i in items) <= budget:
        return [index + "\n\n" + "\n\n".join(i["blurb"] for i in items)]

    anchor_cap = max(0, int(budget * float(config.get_param("synth.anchor_budget_ratio"))))
    small_cut = max(1, int(budget * float(config.get_param("synth.small_payload_ratio"))))
    anchors, rest = [], []
    used = 0
    for it in sorted(items, key=lambda i: (i["n"], it_rows(i))):
        if it["n"] <= small_cut and used + it["n"] <= anchor_cap and used + it["n"] + len(index) < budget:
            anchors.append(it)
            used += it["n"]
        else:
            rest.append(it)
    anchor_text = "\n\n".join(a["blurb"] for a in anchors)

    def _prefix(with_anchors: bool) -> str:
        if with_anchors and anchor_text:
            return index + "\n\n" + anchor_text
        return index

    def _fit_piece(it: dict, room: int) -> str:
        if room <= 0:
            return f"### 表名: {it['name']}"
        if len(it["blurb"]) <= room:
            return it["blurb"]
        return _shrink_blurb(it["name"], it["table"], room)

    batches, cur, cur_len = [], [], 0
    prefix = _prefix(True)
    # 前缀过肥时先丢掉锚点全文，只留目录
    if len(prefix) + 64 >= budget:
        prefix = _prefix(False)
    cur, cur_len = [prefix], len(prefix)

    for it in rest:
        min_need = len(_minimal_blurb(it["name"], it["table"])) + 2
        room = budget - len(prefix) - 2
        # 当前前缀几乎不留空：换更瘦前缀或单独开批
        if room < min(min_need, 80):
            if len(cur) > 1:
                batches.append("\n\n".join(cur))
            prefix = _prefix(False)
            if len(prefix) + min_need > budget:
                # 单表独占一批：目录压到最小 + 该表
                thin = _schema_index(tables, max_cols=2)
                alone = _fit_piece(it, max(budget - len(thin) - 2, 32))
                batches.append(thin + "\n\n" + alone)
                prefix = _prefix(True) if len(_prefix(True)) + 64 < budget else _prefix(False)
                cur, cur_len = [prefix], len(prefix)
                continue
            cur, cur_len = [prefix], len(prefix)
            room = budget - len(prefix) - 2
        blurb = _fit_piece(it, max(room, 32))
        piece = len(blurb) + 2
        if len(cur) > 1 and cur_len + piece > budget:
            batches.append("\n\n".join(cur))
            cur, cur_len = [prefix], len(prefix)
            room = budget - len(prefix) - 2
            blurb = _fit_piece(it, max(room, 32))
            piece = len(blurb) + 2
        cur.append(blurb)
        cur_len += piece
        if config.param_is_user_set("synth.schema_chunk_tables"):
            cap_n = int(config.get_param("synth.schema_chunk_tables"))
            if cap_n > 0 and (len(cur) - 1) >= cap_n:
                batches.append("\n\n".join(cur))
                cur, cur_len = [prefix], len(prefix)
    if len(cur) > 1 or not batches:
        batches.append("\n\n".join(cur))
    return batches


def it_rows(item: dict) -> int:
    return len((item.get("table") or {}).get("rows") or [])


def plan_synthesis(user_input: str, tables: dict, api_key: str) -> Optional[dict]:
    """调用 LLM 生成合成方案。装得进窗口就一次规划；装不下才分批，且每批带小表与总目录。"""
    if not api_key or not tables:
        return None
    batches = pack_schema_batches(tables)
    if len(batches) == 1:
        return _plan_once(user_input, batches[0], api_key)
    plans = []
    for schema in batches:
        p = _plan_once(user_input, schema, api_key)
        if p:
            plans.append(p)
    if not plans:
        return None
    return _merge_plans(plans, tables)


def _contract_prompt_fields() -> dict:
    """Inject current contract version/hash into planner prompt. No invented enums."""
    from pipeline.synth_contract.registry import load_contract
    cv = load_contract()
    return {"contract_version": cv.version, "contract_hash": cv.file_hash}


def _plan_once(user_input: str, schema: str, api_key: str) -> Optional[dict]:
    fields = _contract_prompt_fields()
    prompt = _SYNTH_PROMPT.format(
        user_input=user_input, schema=schema, **fields)
    waits = list(config.get_param("synth.retry_waits") or [2, 4])
    max_retries = int(config.get_param("synth.max_retries") or 2)
    for attempt in range(max_retries):
        data = chat_json(
            prompt, api_key,
            max_tokens=int(config.get_param("synth.max_tokens")),
            temperature=float(config.get_param("synth.temperature")),
            timeout=int(config.get_param("synth.timeout")),
        )
        if isinstance(data, dict) and data.get("output_columns"):
            # 禁止本地补 stamp：缺 contract_version 原样返回，由 gate 拒绝
            return data
        if attempt < max_retries - 1:
            time.sleep(waits[min(attempt, len(waits) - 1)])
    return None


def _merge_plans(plans: list, tables: dict) -> dict:
    """合并分块方案：列/映射/筛选取并集；joins 去重。
    主表不按行数决胜——留给 execute 里的对齐规划一次裁定。"""
    base = dict(plans[0])
    cols, seen_c = [], set()
    for p in plans:
        for c in p.get("output_columns") or []:
            if c not in seen_c:
                seen_c.add(c)
                cols.append(c)
    base["output_columns"] = cols
    joins, seen_j = [], set()
    for p in plans:
        for j in p.get("joins") or []:
            if not isinstance(j, dict):
                continue
            key = (j.get("right_table"), j.get("left_column"), j.get("right_column"))
            if key not in seen_j:
                seen_j.add(key)
                joins.append(j)
    base["joins"] = joins
    bindings = {}
    for p in plans:
        for k, v in (p.get("column_bindings") or {}).items():
            bindings.setdefault(k, v)
    base["column_bindings"] = bindings
    maps, keeps = [], []
    for p in plans:
        maps.extend(p.get("value_mappings") or [])
        keeps.extend(p.get("keep_row_when") or [])
    base["value_mappings"] = maps
    base["keep_row_when"] = keeps
    # 主表：候选多数票；不一致或缺失则留空，由计划声明 / 后续门禁裁定
    candidates = [
        p.get("primary_table") for p in plans
        if p.get("primary_table") in tables
        and (tables.get(p.get("primary_table")) or {}).get("role") != "lookup"
        and (tables.get(p.get("primary_table")) or {}).get("matchable") is not False
    ]
    if candidates:
        from collections import Counter
        base["primary_table"] = Counter(candidates).most_common(1)[0][0]
    else:
        base["primary_table"] = ""
    base["notes"] = "；".join(str(p.get("notes") or "") for p in plans if p.get("notes"))[:500]
    return base
















def execute_plan(tables: dict, plan: dict, notes: list = None,
                 user_input: str = "", api_key: str = "", control=None,
                 on_progress=None, ai_confirm=None, ai_summary=None) -> dict:
    """机械执行合成方案 → {name, columns, rows, plan_notes}。
    契约门禁：缺 contract_version / 校验失败则整批拒绝，不执行、不补丁。
    无环境变量逃生门；契约库须已由 ensure_contract_library 加载。"""
    notes = notes if notes is not None else []

    def _tick(msg):
        if not on_progress:
            return
        try:
            on_progress(msg)
        except Exception:
            pass

    if not tables:
        return {
            "name": "合成表", "columns": [], "rows": [], "plan_notes": notes,
            "contract_rejected": False, "contract_status": "",
            "traces": [], "trace_ok": True, "_mechanical": False,
        }

    from pipeline.synth_contract.boot import assert_contract_library_ready, ContractLibraryError
    from pipeline.synth_contract.gate import (
        attach_contract_meta,
        gate_plan,
        reject_payload,
    )
    try:
        assert_contract_library_ready()
    except ContractLibraryError as e:
        notes.append("契约库未加载：%s" % e)
        from pipeline.synth_contract.validate import ValidationResult
        r = ValidationResult(ok=False)
        r.fail("boot_required", str(e), "boot", state_id="contract_incomplete")
        return reject_payload(notes, r, plan if isinstance(plan, dict) else None)

    _tick("正在按契约版本校验合成方案…")
    gate_result, cv = gate_plan(plan if isinstance(plan, dict) else {}, tables)
    if not gate_result.ok:
        notes.append("契约不完整或版本拒绝：已整批退回，未执行合成")
        return reject_payload(notes, gate_result, plan if isinstance(plan, dict) else None)
    if cv is not None and isinstance(plan, dict):
        attach_contract_meta(plan, cv)
        notes.append(
            "契约门禁通过：%s hash=%s" % (cv.version, cv.file_hash)
        )
    _tick("正在机械执行契约方案（无本地语义启发式）…")
    from pipeline.synth_contract.executor import execute_mechanical
    from pipeline.synth_contract.triage_flow import run_triaged
    return run_triaged(
        tables, plan, notes, cv,
        ai_confirm=ai_confirm, ai_summary=ai_summary,
        execute_mechanical=execute_mechanical,
    )


def synthesize(user_input: str, tables: dict, api_key: str, control=None,
               on_progress=None) -> dict:
    """规划 + 执行。失败时返回空表并带错误说明。附完整性指纹防失真。"""
    from agent_core.validator import table_fingerprint
    from pipeline.synth_contract.boot import ensure_contract_library, ContractLibraryError

    notes = []
    try:
        ensure_contract_library()
    except ContractLibraryError as e:
        return {
            "name": "动态合成表",
            "columns": [],
            "rows": [],
            "plan_notes": ["契约库不可用：%s" % e],
            "synth_plan": None,
            "contract_rejected": True,
            "contract_status": "contract_incomplete",
            "traces": [],
            "trace_ok": False,
            "_mechanical": False,
        }

    src_rows = sum(len(t.get("rows") or []) for t in tables.values())
    src_fp = table_fingerprint({
        "columns": ["__src__", "n"],
        "rows": [{"__src__": n, "n": len(t.get("rows") or [])} for n, t in tables.items()],
    })
    plan = plan_synthesis(user_input, tables, api_key)
    if not plan:
        return {
            "name": "动态合成表",
            "columns": [],
            "rows": [],
            "plan_notes": ["无法从模型获得有效的表格合成方案（请检查 API Key / 网络，或把需求说得更具体）"],
            "synth_plan": None,
            "contract_rejected": True,
            "contract_status": "contract_incomplete",
            "traces": [],
            "trace_ok": False,
            "_mechanical": False,
            "integrity": {"source_rows": src_rows, "lost_rows": src_rows,
                          "checksum_mismatch": False, "source_fingerprint": src_fp},
        }
    result = execute_plan(tables, plan, notes, user_input=user_input, api_key=api_key,
                          control=control, on_progress=on_progress)
    out_fp = table_fingerprint(result)
    lost = 0
    if src_rows > 0 and not result.get("rows") and not (
            plan.get("keep_row_when") or plan.get("value_mappings")):
        lost = src_rows
    result["integrity"] = {
        "source_rows": src_rows,
        "output_rows": len(result.get("rows") or []),
        "lost_rows": lost,
        "checksum_mismatch": False,
        "source_fingerprint": src_fp,
        "output_fingerprint": out_fp,
    }
    result["row_fingerprint"] = out_fp
    result["source_fingerprint"] = src_fp
    return result

