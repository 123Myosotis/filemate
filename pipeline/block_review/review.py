"""复核与回滚。本地不判断列名像不像；只执行复核返回。"""

import hashlib
import json
import time
from datetime import datetime, timezone

import config
from ai.client import chat_completion, parse_json_object
from ai.retry import ChatRetryError, ERR_OUTPUT_EMPTY, ERR_RETRY_TIMEOUT, get_last_retry_log
from ai.context_budget import estimate_tokens, resolve_context_tokens
from pipeline.block_bridge.bridge import run_bridge
from pipeline.block_review.snapshot import (
    SnapshotError,
    alignments_match,
    confirm_mapping,
    load_snapshot,
    persist_bridge_sidecars,
    reject_block,
    reject_mapping,
    restore_from_snapshots,
    save_snapshot,
)
from pipeline.block_stitch.stitch import stitch

ERR_BUDGET_NON_POSITIVE = "review_budget_non_positive"
ERR_TOO_MANY_PACKAGES = "analysis_too_many_packages"
# 检查维度（5a §3.2 细化）。维度描述，不是固定判定规则。
_REVIEW_DIMENSIONS = """检查维度（只按看见的材料判断；不要套固定句式；不要用字数阈值）：
列名形态（都是维度，不是固定规则）：
1. 列名功能定位：输出列名描述的是「这一列装的是什么数据」（数据字段名），
   不是「用户要求做什么」（任务描述）。任务描述属于用户原话，不属于表头。
2. 列名 vs 实际值匹配：对照本包 rows / cell_origin，这一列实际装的值是什么？
   列名是否在描述这些值？还是只在复述用户指令？
3. 列名 vs 用户原话对照：列名是否看起来只是用户原话的改写或缩写？
   特别地：如果列名描述了「做什么」而非「装什么」，倾向于 acceptable=false。
   （由你判断；不是套「逐字相同」的固定匹配。）
4. 列名 vs 源列头对照：对照【源列头对照】。输出列名是否与对应源列头语义一致？
   源列头是原表这一列的字段名；输出列名应描述同一种数据，不是用户原话的改写。
   （不是「与源列头逐字相同」的固定规则。）
其它维度：
5. 每个列的值是否能从 cell_origin 对上源位置。
6. cell_origin 是否与给出的源坐标一致。
7. unresolved_conflicts 与 stitch_implied_conflicts 是否在结果列上被体现。
8. 是否有缺行、重复行、值域异常。
只输出一个 JSON 对象，键为 findings。
每一项含 block_id、mapping_index（可空）、acceptable、note、suggestion（可空）。
没有发现时 findings 必须是空数组。
acceptable 为 false 时带上 block_id；能定位到映射时带上 mapping_index。
"""


class ReviewError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def source_fingerprint(tables_by_id, block_plan):
    """与 2b 定案一致：网格哈希 + 块计划哈希。"""
    parts = []
    for tid in sorted((tables_by_id or {}).keys(), key=lambda x: str(x)):
        grid = (tables_by_id[tid] or {}).get("grid") or []
        blob = "\n".join(
            "\t".join("" if c is None else str(c) for c in (row or [])) for row in grid
        )
        parts.append(str(tid) + "\n" + blob)
    grid_hash = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    plan_body = []
    for block in (block_plan or {}).get("blocks") or []:
        plan_body.append(
            {
                "block_id": block.get("block_id"),
                "parent_table_ids": block.get("parent_table_ids"),
                "axis": block.get("axis"),
                "layout": block.get("layout"),
                "row_span": block.get("row_span"),
                "col_span": block.get("col_span"),
                "payload_tokens": block.get("payload_tokens"),
            }
        )
    plan_hash = hashlib.sha256(
        json.dumps(plan_body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {"grid_hash": grid_hash, "plan_hash": plan_hash}


def _now():
    return datetime.now(timezone.utc).isoformat()


def review_dimensions_text():
    return _REVIEW_DIMENSIONS


def is_effective(finding, block_plan):
    if not isinstance(finding, dict):
        return False
    known = {b.get("block_id") for b in (block_plan or {}).get("blocks") or []}
    if finding.get("block_id") not in known:
        return False
    if not isinstance(finding.get("acceptable"), bool):
        return False
    mid = finding.get("mapping_index", None)
    if mid is not None and not isinstance(mid, int):
        return False
    return True


def _rows_for_block(stitched, block_id):
    origins = [
        o
        for o in (stitched or {}).get("cell_origin") or []
        if o.get("block_id") == block_id
    ]
    indexes = sorted({o.get("row_index") for o in origins if o.get("row_index") is not None})
    rows = (stitched or {}).get("rows") or []
    return [rows[i] for i in indexes if isinstance(i, int) and 0 <= i < len(rows)], origins


def _abs_cell(grid, r, c):
    if r < 0 or c < 0 or r >= len(grid):
        return ""
    row = grid[r] if isinstance(grid[r], (list, tuple)) else []
    if c >= len(row):
        return ""
    val = row[c]
    return "" if val is None else str(val)


def _spans(block):
    spans = block.get("parent_spans")
    if spans:
        return spans
    tids = block.get("parent_table_ids") or [None]
    return [
        {
            "table_id": tids[0],
            "row_span": list(block.get("row_span") or [0, 0]),
            "col_span": list(block.get("col_span") or [0, 0]),
            "repeat_header": False,
        }
    ]


def _source_header_for(block, source, tables_by_id):
    """5a §3.1 3.5：测量事实取原表头。本地不解释语义。"""
    col = source.get("col")
    if col is None:
        return ""
    layout = block.get("layout")
    if layout == "ordered":
        for span in _spans(block):
            c0, c1 = span.get("col_span") or [0, 0]
            if not (c0 <= col < c1):
                continue
            grid = ((tables_by_id or {}).get(span.get("table_id")) or {}).get("grid") or []
            return _abs_cell(grid, 0, col)
        return ""
    # matrix：先找与 source 行/列相交的合并标签；否则块内该列从上往下第一个非空
    row = source.get("row")
    ctx = block.get("context") or {}
    for merge in ctx.get("merges") or []:
        if not isinstance(merge, dict):
            continue
        r0, c0 = int(merge.get("r0", -1)), int(merge.get("c0", -1))
        r1, c1 = int(merge.get("r1", -1)), int(merge.get("c1", -1))
        if row is not None and not (r0 <= row < r1):
            continue
        if not (c0 <= col < c1):
            continue
        text = merge.get("text")
        if text is None:
            for span in _spans(block):
                grid = ((tables_by_id or {}).get(span.get("table_id")) or {}).get("grid") or []
                text = _abs_cell(grid, r0, c0)
                break
        return "" if text is None else str(text)
    for span in _spans(block):
        c0, c1 = span.get("col_span") or [0, 0]
        if not (c0 <= col < c1):
            continue
        grid = ((tables_by_id or {}).get(span.get("table_id")) or {}).get("grid") or []
        r0, r1 = span.get("row_span") or [0, 0]
        for r in range(r0, r1):
            text = _abs_cell(grid, r, col)
            if str(text).strip():
                return text
    return ""


def _value_sample_for(block, source, tables_by_id):
    """测量事实：该列块内第一个非空数据格。
    ordered：跳过第 0 行（表头）；matrix：从块顶起第一个非空格。
    不取第 0 行列头，不用 sources_resolved.cell_text（常为表头原文）。"""
    col = source.get("col")
    if block is None or col is None:
        return ""
    for span in _spans(block):
        c0, c1 = span.get("col_span") or [0, 0]
        if not (c0 <= col < c1):
            continue
        grid = ((tables_by_id or {}).get(span.get("table_id")) or {}).get("grid") or []
        r0, r1 = span.get("row_span") or [0, 0]
        start = r0
        if block.get("layout") == "ordered":
            start = max(r0, 1)
        for r in range(start, r1):
            cell = _abs_cell(grid, r, col)
            if str(cell).strip():
                return cell
    return ""


def build_source_header_对照(executable, block_plan, tables_by_id):
    """对每个 target_name 列出 sources 的原表头与值样例。"""
    blocks = {
        b.get("block_id"): b for b in (block_plan or {}).get("blocks") or []
    }
    out = []
    for item in executable or []:
        name = item.get("target_name")
        for src in item.get("sources_resolved") or []:
            bid = src.get("block_id")
            block = blocks.get(bid)
            if block is None:
                header = ""
                sample = "" if src.get("cell_text") is None else str(src.get("cell_text"))
            else:
                header = _source_header_for(block, src, tables_by_id)
                sample = _value_sample_for(block, src, tables_by_id)
            out.append(
                {
                    "target_name": name,
                    "source_block_id": bid,
                    "source_col": src.get("col"),
                    "source_header": header,
                    "value_sample": sample,
                }
            )
    return out


def resolve_review_model():
    """复核专用模型；空则沿用 api.model。不静默改成别的默认。"""
    name = config.get_param("review.model")
    if name is None or str(name).strip() == "":
        return str(config.get_param("api.model") or "")
    return str(name).strip()


def _review_cfg_num(review_key, llm_key, runtime=None, runtime_key=None):
    """review.* 显式值优先，否则 runtime，再否则 llm.*。"""
    runtime = runtime or {}
    raw = config.get_param(review_key)
    if raw is not None and str(raw).strip() != "":
        return float(raw) if "ratio" in review_key or "delay" in review_key else int(raw)
    if runtime_key is not None and runtime.get(runtime_key) is not None:
        return (
            float(runtime[runtime_key])
            if "ratio" in (runtime_key or "") or "delay" in (runtime_key or "")
            else int(runtime[runtime_key])
        )
    return float(config.get_param(llm_key)) if "ratio" in llm_key else int(config.get_param(llm_key))


def resolve_review_budget(runtime=None):
    """复核输入预算 review_B，与 2b 公式对齐。不读 review_budget_tokens。"""
    runtime = runtime or {}
    model = resolve_review_model()
    if runtime.get("review_model"):
        model = str(runtime.get("review_model")).strip() or model

    raw_window = config.get_param("review.context_tokens")
    if raw_window is not None and str(raw_window).strip() != "":
        window = int(raw_window)
    elif runtime.get("review_context_tokens") is not None:
        window = int(runtime["review_context_tokens"])
    else:
        window = int(resolve_context_tokens(model))

    fill = _review_cfg_num(
        "review.fill_ratio", "llm.context_fill_ratio", runtime, "review_fill_ratio"
    )
    if runtime.get("fill_ratio") is not None and config.get_param("review.fill_ratio") is None:
        # 仅当未设 review.fill_ratio 时允许沿用 runtime.fill_ratio（与 2b 同场）
        fill = float(runtime["fill_ratio"])

    reserved = _review_cfg_num(
        "review.reserved_output_tokens",
        "llm.reserved_output_tokens",
        runtime,
        "review_reserved_output_tokens",
    )
    overhead = _review_cfg_num(
        "review.prompt_overhead_tokens",
        "llm.prompt_overhead_tokens",
        runtime,
        "review_prompt_overhead_tokens",
    )

    usable = int(window * float(fill))
    b_raw = usable - int(reserved) - int(overhead)
    details = {
        "window": window,
        "fill_ratio": float(fill),
        "usable": usable,
        "reserved_output_tokens": int(reserved),
        "prompt_overhead_tokens": int(overhead),
        "review_B": b_raw,
        "model": model,
    }
    if b_raw <= 0:
        raise ReviewError(
            ERR_BUDGET_NON_POSITIVE,
            "review_B<=0",
            details,
        )
    cap = max(1, int(window) - int(reserved))
    review_b = min(int(b_raw), cap)
    details["review_B"] = review_b
    return review_b, details


def _prompt_tokens(package):
    return estimate_tokens(build_review_prompt(package))


def review_packages(
    stitched,
    block_plan,
    tables_by_id,
    bridge_result,
    user_input,
    journal,
    budget_tokens,
    executable=None,
    small_table_ratio=None,
):
    """按块计划已有区间分包。用完整 prompt 的 estimate_tokens 相对 review_B 判切。"""
    packages = []
    budget = int(budget_tokens)
    if small_table_ratio is None:
        small_table_ratio = float(config.get_param("review.small_table_ratio") or 0.5)
    else:
        small_table_ratio = float(small_table_ratio)
    small_cap = budget * small_table_ratio

    blocks = list((block_plan or {}).get("blocks") or [])
    columns = list((stitched or {}).get("columns") or [])
    conflicts = list((stitched or {}).get("unresolved_conflicts") or [])
    implied = list((stitched or {}).get("stitch_implied_conflicts") or [])
    headers = build_source_header_对照(executable, block_plan, tables_by_id)
    if not blocks:
        packages.append(
            {
                "block_id": None,
                "columns": columns,
                "source_header_对照": headers,
                "rows_slice": [],
                "cell_origin_slice": [],
                "unresolved_conflicts": conflicts,
                "stitch_implied_conflicts": implied,
                "user_input": user_input or "",
                "journal": journal or [],
                "bridge": bridge_result or {},
            }
        )
        return packages

    for block in blocks:
        bid = block.get("block_id")
        rows, origins = _rows_for_block(stitched, bid)
        span = block.get("row_span") or [0, 0]
        groups = {}
        for origin in origins:
            groups.setdefault(origin.get("source_row"), []).append(origin)
        source_rows = sorted(k for k in groups if k is not None)
        if not source_rows:
            source_rows = [None]
            groups[None] = []

        def pack(sub_rows_idx, sub_origins):
            return {
                "block_id": bid,
                "row_span": span,
                "col_span": block.get("col_span"),
                "layout": block.get("layout"),
                "columns": columns,
                "source_header_对照": headers,
                "rows_slice": [rows[i] for i in sub_rows_idx if i < len(rows)],
                "cell_origin_slice": sub_origins,
                "unresolved_conflicts": conflicts,
                "stitch_implied_conflicts": implied,
                "user_input": user_input or "",
                "journal": journal or [],
                "bridge": {
                    "rejected": (bridge_result or {}).get("rejected") or [],
                    "alignments_resolved": (bridge_result or {}).get("alignments_resolved") or [],
                    "conflicts": (bridge_result or {}).get("conflicts") or [],
                },
            }

        all_idx = list(range(len(rows)))
        whole = pack(all_idx, origins)
        whole_need = _prompt_tokens(whole)
        # 小表阈值或未超 review_B → 整块一包
        if whole_need <= small_cap or whole_need <= budget:
            packages.append(whole)
            continue

        bucket_idx = []
        bucket_origins = []
        row_index_by_source = {}
        for origin in origins:
            row_index_by_source.setdefault(origin.get("source_row"), origin.get("row_index"))
        for src in source_rows:
            trial_idx = list(bucket_idx)
            ri = row_index_by_source.get(src)
            if isinstance(ri, int) and ri not in trial_idx:
                trial_idx.append(ri)
            trial_origins = bucket_origins + groups.get(src, [])
            trial = pack(trial_idx, trial_origins)
            if _prompt_tokens(trial) <= budget:
                bucket_idx = trial_idx
                bucket_origins = trial_origins
            else:
                if bucket_origins or bucket_idx:
                    packages.append(pack(bucket_idx, bucket_origins))
                bucket_idx = [ri] if isinstance(ri, int) else []
                bucket_origins = list(groups.get(src, []))
        if bucket_idx or bucket_origins:
            packages.append(pack(bucket_idx, bucket_origins))
    return packages


def build_review_prompt(package):
    sections = [
        "【用户原话（供判断列名是否复述用）】\n" + str(package.get("user_input") or ""),
        "【块】block_id=%s layout=%s row_span=%s col_span=%s"
        % (
            package.get("block_id"),
            package.get("layout"),
            package.get("row_span"),
            package.get("col_span"),
        ),
        "【columns（每个列名请对照本包 cell_origin 看这一列实际装的值是什么）】\n"
        + json.dumps(package.get("columns") or [], ensure_ascii=False),
        "【源列头对照（供判断输出列名是否与源列头语义一致）】\n"
        + json.dumps(package.get("source_header_对照") or [], ensure_ascii=False),
        "【rows】\n" + json.dumps(package.get("rows_slice") or [], ensure_ascii=False),
        "【cell_origin】\n" + json.dumps(package.get("cell_origin_slice") or [], ensure_ascii=False),
        "【unresolved_conflicts】\n"
        + json.dumps(package.get("unresolved_conflicts") or [], ensure_ascii=False),
        "【stitch_implied_conflicts】\n"
        + json.dumps(package.get("stitch_implied_conflicts") or [], ensure_ascii=False),
        "【bridge】\n" + json.dumps(package.get("bridge") or {}, ensure_ascii=False, default=str),
        "【journal】\n" + json.dumps(package.get("journal") or [], ensure_ascii=False, default=str),
        "【输出形状与检查维度】\n" + _REVIEW_DIMENSIONS,
    ]
    return "\n\n".join(sections)


def parse_review(raw):
    if not isinstance(raw, dict):
        raise ReviewError("review_not_object", "review is not an object")
    findings = raw.get("findings")
    if findings is None:
        findings = []
    if not isinstance(findings, list):
        raise ReviewError("review_findings_not_array", "findings is not an array")
    return findings


def _exc_detail(exc):
    """拼出可供「模型不存在」判定的详情（含 HTTP body）。"""
    parts = [str(exc)]
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            parts.append(str(resp.status_code))
            parts.append(resp.text or "")
        except Exception:
            pass
    return " ".join(parts)


def _model_unavailable(exc):
    msg = _exc_detail(exc).lower()
    keys = (
        "model not found",
        "model_not_found",
        "invalid model",
        "does not exist",
        "unknown model",
        "无可用模型",
        "模型不存在",
        "not support",
        "unsupported model",
        "model_not_exist",
        "1210",  # 智谱常见：模型不存在错误码
    )
    return any(k in msg for k in keys)


def _review_max_tokens(runtime=None):
    """复核输出 max_tokens：review.reserved_output_tokens 优先，否则 llm.*。"""
    raw = config.get_param("review.reserved_output_tokens")
    if raw is not None and str(raw).strip() != "":
        return int(raw)
    runtime = runtime or {}
    if runtime.get("review_reserved_output_tokens") is not None:
        return int(runtime["review_reserved_output_tokens"])
    return int(config.get_param("llm.reserved_output_tokens"))


def _call_review_model(prompt, state):
    transport = (state or {}).get("review_transport")
    if transport is not None:
        return transport(prompt)
    api_key = config.get_api_key()
    if not api_key:
        raise ReviewError("missing_api_key", "未配置 API Key")
    runtime = (state or {}).get("runtime") or {}
    timeout = runtime.get("review_timeout")
    if timeout is None:
        timeout = int(config.get_param("api.timeout"))
    model = runtime.get("review_model")
    if not model:
        model = resolve_review_model()
    try:
        data = chat_completion(
            [{"role": "user", "content": prompt}],
            api_key,
            max_tokens=_review_max_tokens(runtime),
            temperature=0,
            response_format={"type": "json_object"},
            timeout=int(timeout),
            model=model,
        )
    except ChatRetryError as exc:
        raise ReviewError(
            exc.code,
            "复核模型调用失败 model=%s detail=%s" % (model, exc.message),
            {
                "model": model,
                "unavailable": False,
                "retry_log": list((exc.details or {}).get("retry_log") or get_last_retry_log()),
                "prompt_chars": len(prompt or ""),
                **{k: v for k, v in (exc.details or {}).items() if k != "retry_log"},
            },
        )
    except Exception as exc:
        detail = _exc_detail(exc)
        raise ReviewError(
            "review_model_error",
            "复核模型调用失败 model=%s detail=%s" % (model, detail[:500]),
            {
                "model": model,
                "unavailable": _model_unavailable(exc),
                "retry_log": get_last_retry_log(),
            },
        )
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    return parse_json_object(content)


def run_review(state, max_packages=None):
    runtime = state.get("runtime") or {}
    budget, budget_details = resolve_review_budget(runtime)
    state["review_budget"] = budget_details
    packages = review_packages(
        state.get("stitched") or {},
        state.get("block_plan") or {},
        state.get("tables_by_id") or {},
        state.get("bridge_result") or {},
        state.get("user_input") or "",
        state.get("journal") or [],
        budget,
        executable=state.get("working_executable") or [],
    )
    hard_cap = int(config.get_param("review.max_packages") or 30)
    if len(packages) > hard_cap:
        raise ReviewError(
            ERR_TOO_MANY_PACKAGES,
            "review packages %s exceeds review.max_packages=%s" % (len(packages), hard_cap),
            {
                "packages": len(packages),
                "max_packages": hard_cap,
                "review_B": budget,
                **budget_details,
            },
        )
    state["review_packages"] = packages
    findings = []
    raws = []
    retry_logs = []
    limit = len(packages) if max_packages is None else min(len(packages), int(max_packages))
    delay = float(config.get_param("review.inter_package_delay_sec") or 0)
    for i, pack in enumerate(packages[:limit]):
        if i > 0 and delay > 0:
            time.sleep(delay)
        prompt = build_review_prompt(pack)
        try:
            raw = _call_review_model(prompt, state)
        except ReviewError as exc:
            retry_logs.append(list((exc.details or {}).get("retry_log") or get_last_retry_log()))
            # 空正文：沿已有块/行切片再试一次（预算切不动时强制行对半）
            if exc.code == ERR_OUTPUT_EMPTY:
                smaller = max(500, int(budget) // 2)
                alt_packs = review_packages(
                    state.get("stitched") or {},
                    state.get("block_plan") or {},
                    state.get("tables_by_id") or {},
                    state.get("bridge_result") or {},
                    state.get("user_input") or "",
                    state.get("journal") or [],
                    smaller,
                    executable=state.get("working_executable") or [],
                )
                trial = None
                if alt_packs and (
                    len(alt_packs) > len(packages)
                    or (alt_packs[0].get("rows_slice") or []) != (pack.get("rows_slice") or [])
                ):
                    trial = alt_packs[0]
                else:
                    rows = list(pack.get("rows_slice") or [])
                    origins = list(pack.get("cell_origin_slice") or [])
                    if len(rows) > 1:
                        mid = max(1, len(rows) // 2)
                        trial = dict(pack)
                        trial["rows_slice"] = rows[:mid]
                        seen = []
                        for o in origins:
                            ri = o.get("row_index")
                            if ri not in seen:
                                seen.append(ri)
                        keep_rows = set(seen[:mid])
                        trial["cell_origin_slice"] = [
                            o for o in origins if o.get("row_index") in keep_rows
                        ]
                if trial is None:
                    raise
                prompt2 = build_review_prompt(trial)
                raw = _call_review_model(prompt2, state)
                retry_logs.append(get_last_retry_log())
            else:
                raise
        else:
            retry_logs.append(get_last_retry_log())
        raws.append(raw)
        findings.extend(parse_review(raw))
    state["last_findings"] = findings
    state["review_raws"] = raws
    state["retry_log"] = retry_logs
    return {
        "findings": findings,
        "ai_raw_review": raws,
        "retry_log": retry_logs,
        "review_budget": budget_details,
    }


def _primary_block(item):
    sources = item.get("sources_resolved") or []
    if not sources:
        return None
    return sources[0].get("block_id")


def _exec_key(item):
    return (_primary_block(item), item.get("mapping_index"))


def on_review(review_result, state):
    findings = (review_result or {}).get("findings")
    if not isinstance(findings, list):
        return handle_invalid_review(state)
    plan = state.get("block_plan") or {}
    effective = [f for f in findings if is_effective(f, plan)]
    if findings and not effective:
        return handle_invalid_review(state)
    bad = [f for f in effective if f.get("acceptable") is False]
    if not findings or (effective and not bad):
        state["invalid_streak"] = 0
        _confirm_scope(state, effective)
        state["phase"] = "confirmed"
        return state
    state["invalid_streak"] = 0
    for finding in bad:
        apply_rollback(finding, state)
        handle_reject_path(finding, state)
    return state


def _confirm_scope(state, effective):
    items = list(state.get("working_executable") or [])
    if effective:
        wanted = set()
        for f in effective:
            if f.get("acceptable") is True and f.get("mapping_index") is not None:
                wanted.add((f.get("block_id"), f.get("mapping_index")))
        if wanted:
            items = [e for e in items if _exec_key(e) in wanted or _primary_block(e) in {b for b, m in wanted}]
    by_block = {}
    for item in items:
        by_block.setdefault(_primary_block(item), []).append(item)
    fp = state.get("fingerprint") or source_fingerprint(state.get("tables_by_id"), state.get("block_plan"))
    stitched = state.get("stitched") or {}
    for bid, group in by_block.items():
        if bid is None:
            continue
        snap = load_snapshot(state["session_dir"], bid, fp)
        snap["source_fingerprint"] = fp
        origins = [
            o
            for o in stitched.get("cell_origin") or []
            if o.get("block_id") == bid and o.get("mapping_index") in {g.get("mapping_index") for g in group}
        ]
        rows, _ = _rows_for_block(stitched, bid)
        for item in group:
            confirm_mapping(snap, item, stitched.get("columns"), rows, origins)
        save_snapshot(state["session_dir"], snap, _now())
    persist_bridge_sidecars(
        state["session_dir"],
        (state.get("bridge_result") or {}).get("alignments_resolved") or [],
        (state.get("bridge_result") or {}).get("conflicts") or [],
    )


def handle_reject_path(finding, state):
    if finding.get("mapping_index") is None:
        bid = finding.get("block_id")
        mids = []
        for item in state.get("working_executable") or []:
            if _primary_block(item) == bid:
                mids.append(item.get("mapping_index"))
        for mid in mids:
            child = dict(finding)
            child["mapping_index"] = mid
            handle_reject_path(child, state)
        return state

    key = (finding.get("block_id"), finding.get("mapping_index"))
    counts = state.setdefault("reanalyze_count", {})
    max_n = int(config.get_param("review.reanalyze_max"))
    if int(counts.get(key, 0)) >= max_n:
        return escalate_to_interrupt(finding, state)

    counts[key] = int(counts.get(key, 0)) + 1
    state["reanalyze_count"] = counts
    new_analysis = _reanalyze(state, finding)
    replace_mapping(state, finding.get("block_id"), finding.get("mapping_index"), new_analysis)
    return rebridge_restitch_rereview(state, [key])


def _reanalyze(state, finding):
    transport = state.get("analyze_transport")
    if transport is not None:
        return transport(finding)
    from pipeline.block_analyze.analyze import analyze_blocks

    bid = finding.get("block_id")
    blocks = [b for b in (state.get("block_plan") or {}).get("blocks") or [] if b.get("block_id") == bid]
    prior = None
    for item in state.get("working_executable") or []:
        if _exec_key(item) == (bid, finding.get("mapping_index")):
            prior = item
            break
    user = (state.get("user_input") or "") + "\n" + str(finding.get("note") or "")
    return analyze_blocks(
        user,
        blocks,
        state.get("tables_by_id") or {},
        state.get("block_plan") or {},
        reply_text=str(finding.get("note") or ""),
        prior_mappings=[prior] if prior else [],
        runtime=state.get("runtime"),
    )


def replace_mapping(state, block_id, mapping_index, new_analysis):
    mappings = (new_analysis or {}).get("mappings") or []
    if not mappings:
        return
    fresh = dict(mappings[0])
    fresh.setdefault("sources", [])
    state.setdefault("replaced_mappings", {})[(block_id, mapping_index)] = fresh
    # 保留旧坐标，只换 target_name（及同次信封里的 target）
    target = (fresh.get("target") or {}).get("name")
    updated = []
    for item in state.get("working_executable") or []:
        if _exec_key(item) == (block_id, mapping_index) and target:
            cloned = dict(item)
            cloned["target_name"] = target
            if "rule" in fresh:
                cloned["rule"] = fresh.get("rule")
            updated.append(cloned)
        else:
            updated.append(item)
    state["working_executable"] = updated


def rebridge_restitch_rereview(state, replaced_keys):
    """选 A：confirmed 从快照读；替换条再走桥；全量 stitch。"""
    fp = state.get("fingerprint") or source_fingerprint(state.get("tables_by_id"), state.get("block_plan"))
    confirmed = []
    replaced = set(replaced_keys)
    for block in (state.get("block_plan") or {}).get("blocks") or []:
        snap = load_snapshot(state["session_dir"], block.get("block_id"), fp)
        if snap.get("status") == "rejected":
            continue
        for item in snap.get("executable_confirmed") or []:
            key = (block.get("block_id"), item.get("mapping_index"))
            if key in replaced:
                continue
            confirmed.append(item)

    replaced_exec = []
    mappings = []
    for key in replaced_keys:
        raw = (state.get("replaced_mappings") or {}).get(key)
        if raw:
            mappings.append(raw)
        else:
            for item in state.get("working_executable") or []:
                if _exec_key(item) == key:
                    replaced_exec.append(item)
    bridge_out = {"executable": [], "conflicts": (state.get("bridge_result") or {}).get("conflicts") or [], "alignments_resolved": (state.get("bridge_result") or {}).get("alignments_resolved") or []}
    if mappings:
        released = [{"global_index": i} for i in range(len(mappings))]
        bridge_out = run_bridge(
            {
                "block_plan": state.get("block_plan"),
                "analysis": {
                    "mappings": mappings,
                    "alignments": [],
                    "ai_raws": [mappings],
                    "matrix_draft": state.get("matrix_draft"),
                },
                "tables_by_id": state.get("tables_by_id"),
                "released": released,
                "runtime": {"remaining_tokens": int((state.get("runtime") or {}).get("remaining_tokens") or 10**6)},
                "matrix_draft": state.get("matrix_draft"),
            }
        )
        for item in bridge_out.get("executable") or []:
            # 桥的 mapping_index 是本次子集下标；改回原点名
            src_key = replaced_keys[item.get("mapping_index")] if item.get("mapping_index") < len(replaced_keys) else None
            if src_key:
                item = dict(item)
                item["mapping_index"] = src_key[1]
                sources = []
                for s in item.get("sources_resolved") or []:
                    s = dict(s)
                    s["block_id"] = src_key[0]
                    sources.append(s)
                item["sources_resolved"] = sources
            replaced_exec.append(item)

    alignments = (state.get("bridge_result") or {}).get("alignments_resolved") or []
    if not alignments_match(alignments, state.get("block_plan") or {"blocks": []}) and alignments:
        raise ReviewError("alignments_missing_or_mismatch", "重拼时对齐与块计划不匹配")
    conflicts = bridge_out.get("conflicts") or []
    merged = confirmed + replaced_exec
    # 工作集同步
    by_key = {}
    for item in confirmed + replaced_exec:
        by_key[_exec_key(item)] = item
    for item in state.get("working_executable") or []:
        by_key.setdefault(_exec_key(item), item)
    state["working_executable"] = list(by_key.values())
    stitched = stitch(
        {
            "executable": state["working_executable"],
            "conflicts": conflicts,
            "alignments_resolved": alignments,
            "block_plan": state.get("block_plan"),
            "tables_by_id": state.get("tables_by_id"),
            "dimensions_by_block": state.get("dimensions_by_block") or {},
        }
    )
    state["stitched"] = stitched
    state["bridge_result"] = dict(state.get("bridge_result") or {})
    state["bridge_result"]["conflicts"] = conflicts
    state["bridge_result"]["alignments_resolved"] = alignments
    persist_bridge_sidecars(state["session_dir"], alignments, conflicts)
    reviewed = run_review(state, max_packages=state.get("review_call_limit"))
    return after_rereview_same_mapping(reviewed, state, replaced_keys)


def after_rereview_same_mapping(reviewed, state, replaced_keys):
    findings = reviewed.get("findings") or []
    plan = state.get("block_plan") or {}
    effective = [f for f in findings if is_effective(f, plan)]
    max_n = int(config.get_param("review.reanalyze_max"))
    for key in replaced_keys:
        f_new = None
        for f in effective:
            if (f.get("block_id"), f.get("mapping_index")) == key and f.get("acceptable") is False:
                f_new = f
                break
        if f_new is None:
            # 未再打回：确认
            for item in state.get("working_executable") or []:
                if _exec_key(item) == key:
                    fp = state.get("fingerprint") or source_fingerprint(state.get("tables_by_id"), state.get("block_plan"))
                    snap = load_snapshot(state["session_dir"], key[0], fp)
                    snap["source_fingerprint"] = fp
                    confirm_mapping(snap, item, (state.get("stitched") or {}).get("columns"), [], [])
                    save_snapshot(state["session_dir"], snap, _now())
                    state.setdefault("reanalyze_count", {}).pop(key, None)
            continue
        # 写死：key 来自本次 f_new
        key = (f_new.get("block_id"), f_new.get("mapping_index"))
        if int(state.setdefault("reanalyze_count", {}).get(key, 0)) >= max_n:
            escalate_to_interrupt(f_new, state)
        else:
            handle_reject_path(f_new, state)
    return state


def escalate_to_interrupt(finding, state):
    target = ""
    for item in state.get("working_executable") or []:
        if _exec_key(item) == (finding.get("block_id"), finding.get("mapping_index")):
            target = str(item.get("target_name") or "")
            break
    question = {
        "hypothesis": "复核认为当前 target_name 不合理输出列名",
        "evidence": target + "\n" + str(finding.get("note") or ""),
        "if_holds": str(finding.get("suggestion") or ""),
        "ask_user": "请给出应使用的输出列名，或说明如何处理；也可取消任务",
        "block_ids": [finding.get("block_id")],
        "scope": "mapping",
        "mapping_indexes": [finding.get("mapping_index")],
    }
    state["waiting"] = True
    state["phase"] = "interrupt"
    state.setdefault("open_questions", []).append(question)
    state["confirmed_by_user"] = False
    return state


def apply_rollback(finding, state):
    if finding.get("mapping_index") is None:
        return rollback_block(finding.get("block_id"), state)
    return rollback_mapping(finding.get("block_id"), finding.get("mapping_index"), state)


def rollback_mapping(block_id, mapping_index, state):
    fp = state.get("fingerprint") or source_fingerprint(state.get("tables_by_id"), state.get("block_plan"))
    snap = load_snapshot(state["session_dir"], block_id, fp)
    snap["source_fingerprint"] = fp
    reject_mapping(snap, mapping_index)
    save_snapshot(state["session_dir"], snap, _now())
    stitched = state.get("stitched") or {}
    origins = [
        o
        for o in stitched.get("cell_origin") or []
        if not (o.get("block_id") == block_id and o.get("mapping_index") == mapping_index)
    ]
    stitched["cell_origin"] = origins
    state["stitched"] = stitched
    state.setdefault("pending", set()).add((block_id, mapping_index))
    return state


def rollback_block(block_id, state):
    fp = state.get("fingerprint") or source_fingerprint(state.get("tables_by_id"), state.get("block_plan"))
    snap = load_snapshot(state["session_dir"], block_id, fp)
    snap["source_fingerprint"] = fp
    reject_block(snap)
    save_snapshot(state["session_dir"], snap, _now())
    stitched = state.get("stitched") or {}
    stitched["cell_origin"] = [
        o for o in stitched.get("cell_origin") or [] if o.get("block_id") != block_id
    ]
    state["stitched"] = stitched
    return state


def handle_invalid_review(state):
    state["invalid_streak"] = int(state.get("invalid_streak") or 0) + 1
    state["confirmed_by_user"] = False
    state["phase"] = "unconfirmed"
    n = int(config.get_param("review.invalid_threshold"))
    if state["invalid_streak"] < n:
        return state
    bids = [b.get("block_id") for b in (state.get("block_plan") or {}).get("blocks") or []]
    state["waiting"] = True
    state["phase"] = "interrupt"
    state.setdefault("open_questions", []).append(
        {
            "hypothesis": "复核连续未能给出带块定位的有效意见",
            "evidence": "连续无效次数 = %s" % n,
            "if_holds": "若接受当前拼接结果可直接出表",
            "ask_user": "复核连续 %s 次没给有效意见，你可以接受当前结果或取消任务；也可要求继续复核" % n,
            "block_ids": bids,
            "scope": "block",
        }
    )
    return state


def apply_invalid_user_choice(state, choice):
    """接受 / 取消 / 继续。不扫描用户散文。"""
    if choice == "accept":
        state["confirmed_by_user"] = True
        state["invalid_streak"] = 0
        state["waiting"] = False
        state["phase"] = "confirmed_by_user"
        state["cancelled"] = False
        return state
    if choice == "cancel":
        state["cancelled"] = True
        state["waiting"] = False
        state["phase"] = "cancelled"
        state["confirmed_by_user"] = False
        return state
    if choice == "continue":
        state["invalid_streak"] = 0
        state["waiting"] = False
        state["phase"] = "rereview"
        state["cancelled"] = False
        return state
    raise ReviewError("unknown_choice", "choice must be accept, cancel, or continue")
