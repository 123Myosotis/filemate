"""analyze_blocks：调用封装、解析、超预算、速率限制重试（与超预算路径独立）。"""

import time

import requests
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import RequestException, Timeout

import config
from ai.client import CallCancelled, chat_completion, parse_json_object
from ai.retry import ChatRetryError, ERR_OUTPUT_EMPTY, ERR_RETRY_TIMEOUT, get_last_retry_log
from ai.context_budget import estimate_tokens
from pipeline.block_analyze.prompt import build_prompt
from pipeline.block_analyze.matrix_geom import draft_matrix_block, dimension_candidates


class AnalyzeError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


ERR_PROMPT_EXCEEDS = "analysis_prompt_exceeds_budget"
ERR_NOT_OBJECT = "analysis_not_object"
ERR_MISSING_MAPPINGS = "analysis_missing_mappings"
ERR_QUESTIONS_TYPE = "analysis_questions_not_array"
ERR_ALIGNMENTS_TYPE = "analysis_alignments_not_array"
ERR_MAPPING_ENVELOPE = "analysis_mapping_envelope"
ERR_RETRY_EXCEEDED = "analysis_retry_exceeded"
ERR_DRAFT_MISSING = "draft_fields_missing"
ERR_DIM_MISSING = "dimensions_missing"
ERR_DIM_EMPTY = "dimensions_empty"
ERR_DIM_NOT_CAND = "dimensions_not_candidate"
ERR_DIM_DUP = "dimensions_duplicate"
ERR_DIM_UNKNOWN = "dimensions_unknown_field"
ERR_NO_DIM_CAND = "no_dimension_candidates"
ERR_DIM_RESERVED = "dimension_name_conflicts_reserved"
ERR_DIM_COVER_ALL = "dimensions_cover_all_fields"
RESERVED_DIM_NAMES = frozenset({"attribute_name", "attribute_value"})


def _budget_B(runtime=None):
    runtime = runtime or {}
    window = int(
        runtime.get("context_tokens")
        if runtime.get("context_tokens") is not None
        else config.get_param("llm.context_tokens")
    )
    fill = float(
        runtime.get("fill_ratio")
        if runtime.get("fill_ratio") is not None
        else config.get_param("llm.context_fill_ratio")
    )
    reserved = int(
        runtime.get("reserved_output_tokens")
        if runtime.get("reserved_output_tokens") is not None
        else config.get_param("llm.reserved_output_tokens")
    )
    overhead = int(
        runtime.get("prompt_overhead_tokens")
        if runtime.get("prompt_overhead_tokens") is not None
        else config.get_param("llm.prompt_overhead_tokens")
    )
    user_tokens = int(runtime.get("user_instruction_tokens") or 0)
    usable = int(window * fill)
    b_raw = usable - reserved - overhead - user_tokens
    if b_raw <= 0:
        raise AnalyzeError(
            "budget_non_positive",
            "B_raw<=0",
            {
                "window": window,
                "usable": usable,
                "reserved_output_tokens": reserved,
                "prompt_overhead_tokens": overhead,
                "user_instruction_tokens": user_tokens,
                "B_raw": b_raw,
            },
        )
    return b_raw, {
        "window": window,
        "usable": usable,
        "reserved_output_tokens": reserved,
        "prompt_overhead_tokens": overhead,
        "user_instruction_tokens": user_tokens,
        "B": b_raw,
        "fill_ratio": fill,
    }


def parse_analysis(raw, block_plan):
    """按 2b 伪代码解析。缺 questions/alignments → 零条；畸形标 unexecutable。"""
    if not isinstance(raw, dict):
        raise AnalyzeError(ERR_NOT_OBJECT, "analysis is not an object")
    if "mappings" not in raw or not isinstance(raw.get("mappings"), list):
        raise AnalyzeError(ERR_MISSING_MAPPINGS, "mappings missing or not array")
    if "questions" in raw and not isinstance(raw.get("questions"), list):
        raise AnalyzeError(ERR_QUESTIONS_TYPE, "questions present but not array")
    if "alignments" in raw and not isinstance(raw.get("alignments"), list):
        raise AnalyzeError(ERR_ALIGNMENTS_TYPE, "alignments present but not array")
    questions_raw = raw["questions"] if isinstance(raw.get("questions"), list) else []
    alignments_raw = raw["alignments"] if isinstance(raw.get("alignments"), list) else []

    known_ids = {
        bl.get("block_id") for bl in (block_plan or {}).get("blocks") or []
    }
    mappings = []
    for item in raw["mappings"]:
        if not isinstance(item, dict):
            raise AnalyzeError(ERR_MAPPING_ENVELOPE, "mapping item not object")
        sources = item.get("sources")
        target = item.get("target")
        if not isinstance(sources, list) or not sources:
            raise AnalyzeError(ERR_MAPPING_ENVELOPE, "mapping missing sources")
        if not isinstance(target, dict) or not str(target.get("name") or "").strip():
            raise AnalyzeError(ERR_MAPPING_ENVELOPE, "mapping missing target.name")
        for src in sources:
            if not isinstance(src, dict) or "block_id" not in src or "locator" not in src:
                raise AnalyzeError(ERR_MAPPING_ENVELOPE, "source missing block_id/locator")
        mappings.append(dict(item))

    # 每块 mappings 条数：按 sources 首次 block_id 归属
    per_block_count = {}
    for m in mappings:
        seen = set()
        for src in m["sources"]:
            bid = src["block_id"]
            if bid in seen:
                continue
            seen.add(bid)
            per_block_count[bid] = per_block_count.get(bid, 0) + 1

    parsed_questions = []
    for q in questions_raw:
        executable = True
        reason = None
        if not isinstance(q, dict):
            executable, reason = False, "not_object"
        else:
            for key in ("hypothesis", "evidence", "if_holds", "ask_user"):
                if not str(q.get(key) or "").strip():
                    executable, reason = False, "missing_" + key
                    break
            bids = q.get("block_ids")
            if executable and (not isinstance(bids, list) or not bids):
                executable, reason = False, "missing_block_ids"
            if executable:
                for bid in bids:
                    if bid not in known_ids:
                        executable, reason = False, "unknown_block_id"
                        break
            scope = str(q.get("scope") or "")
            if executable and scope not in ("block", "mapping", "value_domain"):
                executable, reason = False, "bad_scope"
            if executable and scope in ("mapping", "value_domain"):
                indexes = q.get("mapping_indexes")
                if not isinstance(indexes, list) or not indexes:
                    executable, reason = False, "missing_mapping_indexes"
                elif executable:
                    for bid in bids:
                        limit = per_block_count.get(bid, 0)
                        for idx in indexes:
                            if not isinstance(idx, int) or idx < 0 or idx >= limit:
                                executable, reason = False, "mapping_index_oob"
                                break
                        if not executable:
                            break
            if executable and scope == "value_domain" and "value_domain" not in q:
                executable, reason = False, "missing_value_domain"
        parsed_questions.append(
            {"raw": q, "executable": executable, "reason": reason}
        )

    parsed_alignments = []
    for a in alignments_raw:
        executable = True
        reason = None
        if not isinstance(a, dict):
            executable, reason = False, "not_object"
        else:
            slots = a.get("slots")
            if not isinstance(slots, list) or len(slots) < 2:
                executable, reason = False, "align_too_few_slots"
            else:
                for slot in slots:
                    if not isinstance(slot, dict):
                        executable, reason = False, "align_slot_not_object"
                        break
                    if "block_id" not in slot or slot.get("locator") is None or str(slot.get("locator") or "") == "":
                        executable, reason = False, "align_slot_missing_fields"
                        break
                    if slot.get("block_id") not in known_ids:
                        executable, reason = False, "unknown_block_id"
                        break
        parsed_alignments.append(
            {"raw": a, "executable": executable, "reason": reason}
        )

    return {
        "mappings": mappings,
        "questions": parsed_questions,
        "alignments": parsed_alignments,
        "ai_raw": raw,
    }


def _is_rate_or_network(exc):
    if isinstance(exc, (Timeout, ReqConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError):
        resp = getattr(exc, "response", None)
        if resp is not None and resp.status_code in (408, 429, 500, 502, 503, 504):
            return True
    if isinstance(exc, RequestException):
        return True
    return False


def _chat_json_with_retry(prompt, api_key, *, max_tokens=None, temperature=None,
                         timeout=None, system=None, control=None,
                         transport=None):
    """JSON 对话。429/网络/空正文由 ai.client.chat_completion 统一重试，此处不再叠加 sleep。"""
    try:
        if transport is not None:
            raw = transport(prompt, api_key)
        else:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            data = chat_completion(
                messages,
                api_key,
                max_tokens=max_tokens,
                temperature=temperature if temperature is not None else 0.1,
                response_format={"type": "json_object"},
                timeout=timeout,
                control=control,
            )
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            raw = parse_json_object(content)
        if not isinstance(raw, dict):
            raise AnalyzeError(ERR_NOT_OBJECT, "model returned non-object")
        return raw
    except CallCancelled:
        raise
    except AnalyzeError:
        raise
    except ChatRetryError as exc:
        code = exc.code
        if code == ERR_RETRY_TIMEOUT:
            raise AnalyzeError(
                ERR_RETRY_TIMEOUT,
                exc.message,
                dict(exc.details or {}),
            )
        if code == ERR_OUTPUT_EMPTY:
            raise AnalyzeError(
                ERR_OUTPUT_EMPTY,
                exc.message,
                {
                    **(exc.details or {}),
                    "prompt_chars": len(prompt or ""),
                },
            )
        raise AnalyzeError(code or "analysis_transport_error", exc.message, exc.details)
    except Exception as exc:
        if _is_rate_or_network(exc):
            # 统一层未接住时的兜底（不应常走）
            raise AnalyzeError(
                ERR_RETRY_EXCEEDED,
                str(exc),
                {"last_error": str(exc), "retry_log": get_last_retry_log()},
            )
        raise AnalyzeError("analysis_transport_error", str(exc), {"retry_log": get_last_retry_log()})


def _slice_block_rows(block, row_start, row_end):
    """沿已有 row_span 切出子块视图（不改块计划）。"""
    clone = dict(block)
    spans = []
    for span in block.get("parent_spans") or [
        {
            "table_id": (block.get("parent_table_ids") or [None])[0],
            "row_span": list(block["row_span"]),
            "col_span": list(block["col_span"]),
            "repeat_header": False,
        }
    ]:
        s = dict(span)
        r0, r1 = span["row_span"]
        lo = max(r0, row_start)
        hi = min(r1, row_end)
        if lo >= hi:
            continue
        s["row_span"] = [lo, hi]
        spans.append(s)
    if not spans:
        return None
    clone["parent_spans"] = spans
    clone["row_span"] = list(spans[0]["row_span"])
    return clone


def _split_blocks_to_fit(blocks, tables_by_id, user_input, reply_text, prior_mappings,
                         bridge_readonly, open_questions, b_tokens):
    """再分析超 B：按块已有行区间切包，使每包 need ≤ B。"""
    packages = []
    for block in blocks:
        span0 = (block.get("parent_spans") or [{"row_span": block["row_span"]}])[0]
        r0, r1 = span0["row_span"]
        cursor = r0
        while cursor < r1:
            lo, hi = cursor, r1
            best = None
            # 二分找最大可装子区间
            left, right = cursor + 1, r1
            while left <= right:
                mid = (left + right) // 2
                sub = _slice_block_rows(block, cursor, mid)
                if sub is None:
                    break
                prompt, parts = build_prompt(
                    user_input,
                    [sub],
                    tables_by_id,
                    reply_text=reply_text,
                    prior_mappings=prior_mappings,
                    bridge_readonly=bridge_readonly,
                    open_questions=open_questions,
                )
                if parts["prompt_tokens"] <= b_tokens:
                    best = (mid, sub, prompt, parts)
                    left = mid + 1
                else:
                    right = mid - 1
            if best is None:
                # 单行仍超：再分析路径仍须切；若连一行都装不下则报错（不静默截断）
                sub = _slice_block_rows(block, cursor, min(cursor + 1, r1))
                prompt, parts = build_prompt(
                    user_input,
                    [sub],
                    tables_by_id,
                    reply_text=reply_text,
                    prior_mappings=prior_mappings,
                    bridge_readonly=bridge_readonly,
                    open_questions=open_questions,
                )
                if parts["prompt_tokens"] > b_tokens:
                    raise AnalyzeError(
                        ERR_PROMPT_EXCEEDS,
                        "reanalyze slice still exceeds B",
                        {"need": parts["prompt_tokens"], "B": b_tokens, **parts},
                    )
                packages.append([sub])
                cursor = min(cursor + 1, r1)
            else:
                packages.append([best[1]])
                cursor = best[0]
    return packages


def _build_matrix_drafts(blocks, tables_by_id):
    drafts = {}
    candidates = {}
    for b in blocks or []:
        if b.get("layout") != "matrix":
            continue
        d = draft_matrix_block(b, tables_by_id)
        if not d:
            continue
        drafts[b.get("block_id")] = d
        candidates[b.get("block_id")] = dimension_candidates(d, tables_by_id, b)
    return drafts, candidates


def _matrix_section_text(drafts, candidates):
    import json

    payload = []
    for bid, d in drafts.items():
        fields = []
        for f in d.get("fields") or []:
            fields.append(
                {
                    "field_id": f["field_id"],
                    "role": f["role"],
                    "subtable_id": f["subtable_id"],
                    "attribute_slot": f.get("attribute_slot"),
                    "value_samples": f.get("value_samples") or [],
                    "segments": f.get("segments"),
                }
            )
        payload.append(
            {
                "block_id": bid,
                "subtable_ids": d.get("subtable_ids"),
                "fields": fields,
                "dimension_candidates": candidates.get(bid) or {},
            }
        )
    return "【matrix 字段草案（只读；只为每个 field_id 写 target.name）】\n" + json.dumps(
        payload, ensure_ascii=False
    )


def _validate_draft_coverage(mappings, drafts):
    required = {}
    for bid, d in (drafts or {}).items():
        required[bid] = {f["field_id"] for f in d.get("fields") or []}
    seen = {bid: set() for bid in required}
    for m in mappings or []:
        for src in m.get("sources") or []:
            if not isinstance(src, dict):
                continue
            bid = src.get("block_id")
            fid = src.get("field_id")
            if fid is None and str(src.get("locator") or "").startswith("field:"):
                fid = str(src.get("locator")).split(":", 1)[1]
            if bid in seen and fid:
                seen[bid].add(str(fid))
    missing = []
    for bid, need in required.items():
        miss = sorted(need - seen.get(bid, set()))
        if miss:
            missing.append({"block_id": bid, "field_ids": miss})
    return missing


def _parse_dimensions(raw, candidates, drafts, name_by_fid):
    """校验 Z 输出。成功返回 {block_id: {subtable_id: [field_id...]}}。"""
    if not isinstance(raw, dict):
        raise AnalyzeError(ERR_DIM_MISSING, "dimensions response not object")
    out = {}
    for bid, cands in (candidates or {}).items():
        draft = (drafts or {}).get(bid) or {}
        block_out = {}
        for sid in draft.get("subtable_ids") or list(cands.keys()):
            key = "subtable_%s_dimensions" % sid
            # allow block-scoped key
            vals = raw.get(key)
            if vals is None and isinstance(raw.get(str(bid)), dict):
                vals = raw[str(bid)].get(key)
            if vals is None and isinstance(raw.get(bid), dict):
                vals = raw[bid].get(key)
            if vals is None and isinstance(raw.get("dimensions"), dict):
                vals = raw["dimensions"].get(str(sid)) or raw["dimensions"].get(sid)
            if vals is None:
                raise AnalyzeError(
                    ERR_DIM_MISSING,
                    "missing %s" % key,
                    {"block_id": bid, "key": key, "got_keys": list(raw.keys())[:30]},
                )
            if not isinstance(vals, list) or not vals:
                raise AnalyzeError(ERR_DIM_EMPTY, "empty dimensions", {"block_id": bid, "key": key})
            cand_set = set(cands.get(sid) or cands.get(str(sid)) or [])
            if not cand_set:
                raise AnalyzeError(ERR_NO_DIM_CAND, "no candidates", {"block_id": bid, "subtable_id": sid})
            seen = []
            for v in vals:
                if isinstance(v, dict):
                    v = v.get("field_id") or v.get("id")
                fid = str(v)
                if fid in seen:
                    raise AnalyzeError(ERR_DIM_DUP, "duplicate dim", {"field_id": fid})
                if fid not in cand_set:
                    # also reject unknown
                    all_f = {f["field_id"] for f in draft.get("fields") or []}
                    if fid not in all_f:
                        raise AnalyzeError(ERR_DIM_UNKNOWN, "unknown field", {"field_id": fid})
                    raise AnalyzeError(ERR_DIM_NOT_CAND, "not candidate", {"field_id": fid})
                tname = str((name_by_fid.get((bid, fid)) or "")).strip()
                if tname in RESERVED_DIM_NAMES:
                    raise AnalyzeError(
                        ERR_DIM_RESERVED,
                        "dimension name reserved",
                        {"field_id": fid, "target_name": tname},
                    )
                seen.append(fid)
            sub_fields = {
                f["field_id"]
                for f in draft.get("fields") or []
                if int(f.get("subtable_id") or 0) == int(sid)
            }
            if sub_fields and set(seen) >= sub_fields:
                raise AnalyzeError(
                    ERR_DIM_COVER_ALL,
                    "dimensions cover all fields",
                    {"block_id": bid, "subtable_id": sid, "dimensions": seen},
                )
            # 同构小表上限 2；非同构（多 list）允许更多，但不得覆盖全部字段
            iso = bool(draft.get("isomorphic", True))
            if iso and len(seen) > 2:
                raise AnalyzeError(
                    ERR_DIM_COVER_ALL,
                    "too many dimensions",
                    {"block_id": bid, "subtable_id": sid, "dimensions": seen},
                )
            list_ids = [
                f["field_id"]
                for f in draft.get("fields") or []
                if int(f.get("subtable_id") or 0) == int(sid) and f.get("role") == "list"
            ]
            # 同构：有 list 则维度必须含 list。非同构多 list：至少含其中一个（上限 2，不能要求全含）
            if list_ids and not (set(seen) & set(list_ids)):
                raise AnalyzeError(
                    ERR_DIM_NOT_CAND,
                    "dimensions must include a list",
                    {"block_id": bid, "subtable_id": sid, "list_ids": list_ids},
                )
            block_out[int(sid) if not isinstance(sid, int) else sid] = seen
        out[bid] = block_out
    return out


def analyze_blocks(
    user_input,
    blocks,
    tables_by_id,
    block_plan,
    *,
    api_key=None,
    reply_text=None,
    prior_mappings=None,
    bridge_readonly=None,
    open_questions=None,
    runtime=None,
    control=None,
    transport=None,
):
    """
    新链路块分析。
    - 首次（reply_text is None）超 B → analysis_prompt_exceeds_budget，不内部重切。
    - 再分析超 B → 按块区间切包分次调用并合并。
    - 速率限制重试读 api.max_retries_per_request，与超预算路径独立。
    - matrix：几何草案约束 + 命名调用 + Z 维度调用。
    """
    api_key = api_key if api_key is not None else config.get_api_key()
    if not api_key and transport is None:
        raise AnalyzeError("missing_api_key", "未配置 API Key")
    b_tokens, b_details = _budget_B(runtime)
    is_reanalyze = reply_text is not None

    drafts, candidates = _build_matrix_drafts(blocks, tables_by_id)
    matrix_section = _matrix_section_text(drafts, candidates) if drafts else None
    for bid, cands in candidates.items():
        for sid, lst in (cands or {}).items():
            if not lst:
                raise AnalyzeError(
                    ERR_NO_DIM_CAND,
                    "no_dimension_candidates",
                    {"block_id": bid, "subtable_id": sid},
                )

    prompt, parts = build_prompt(
        user_input,
        blocks,
        tables_by_id,
        reply_text=reply_text,
        prior_mappings=prior_mappings,
        bridge_readonly=bridge_readonly,
        open_questions=open_questions,
        matrix_section=matrix_section,
    )
    need = parts["prompt_tokens"]

    if need > b_tokens and not is_reanalyze:
        raise AnalyzeError(
            ERR_PROMPT_EXCEEDS,
            "请调大 prompt_overhead_tokens 或 reserved_output_tokens 后重跑",
            {
                "need": need,
                "B": b_tokens,
                "prompt_overhead_tokens": b_details["prompt_overhead_tokens"],
                "reserved_output_tokens": b_details["reserved_output_tokens"],
                "fill_ratio": b_details["fill_ratio"],
                "template_tokens": parts["template_tokens"],
                "blocks_tokens": parts["blocks_tokens"],
                "user_tokens": parts["user_tokens"],
            },
        )

    call_packages = [blocks]
    if need > b_tokens and is_reanalyze:
        call_packages = _split_blocks_to_fit(
            blocks,
            tables_by_id,
            user_input,
            reply_text,
            prior_mappings,
            bridge_readonly,
            open_questions,
            b_tokens,
        )

    merged_mappings = []
    merged_questions = []
    merged_alignments = []
    raws = []
    retry_logs = []
    for pack in call_packages:
        pack_drafts, pack_cands = _build_matrix_drafts(pack, tables_by_id)
        pack_section = _matrix_section_text(pack_drafts, pack_cands) if pack_drafts else None
        prompt_i, _parts_i = build_prompt(
            user_input,
            pack,
            tables_by_id,
            reply_text=reply_text,
            prior_mappings=prior_mappings,
            bridge_readonly=bridge_readonly,
            open_questions=open_questions,
            matrix_section=pack_section,
        )
        if estimate_tokens(prompt_i) > b_tokens and not is_reanalyze:
            raise AnalyzeError(
                ERR_PROMPT_EXCEEDS,
                "请调大 prompt_overhead_tokens 或 reserved_output_tokens 后重跑",
                {"need": estimate_tokens(prompt_i), "B": b_tokens},
            )
        try:
            raw = _chat_json_with_retry(
                prompt_i,
                api_key,
                max_tokens=int(config.get_param("llm.reserved_output_tokens")),
                temperature=0.1,
                timeout=int(config.get_param("api.timeout")),
                system="只输出合法 JSON 对象。",
                control=control,
                transport=transport,
            )
        except AnalyzeError as exc:
            retry_logs.append(list((exc.details or {}).get("retry_log") or get_last_retry_log()))
            # 空正文：调用方再切一次已有行区间后重试一包
            if (
                exc.code == ERR_OUTPUT_EMPTY
                and len(pack) == 1
                and transport is None
            ):
                sub_packs = _split_blocks_to_fit(
                    pack,
                    tables_by_id,
                    user_input,
                    reply_text,
                    prior_mappings,
                    bridge_readonly,
                    open_questions,
                    b_tokens,
                )
                if len(sub_packs) <= 1:
                    raise
                # 只再试第一刀切出的首包，避免无限切
                pack = sub_packs[0]
                prompt_i, _parts_i = build_prompt(
                    user_input,
                    pack,
                    tables_by_id,
                    reply_text=reply_text,
                    prior_mappings=prior_mappings,
                    bridge_readonly=bridge_readonly,
                    open_questions=open_questions,
                    matrix_section=pack_section,
                )
                raw = _chat_json_with_retry(
                    prompt_i,
                    api_key,
                    max_tokens=int(config.get_param("llm.reserved_output_tokens")),
                    temperature=0.1,
                    timeout=int(config.get_param("api.timeout")),
                    system="只输出合法 JSON 对象。",
                    control=control,
                    transport=transport,
                )
            else:
                raise
        retry_logs.append(get_last_retry_log())
        parsed = parse_analysis(raw, block_plan)
        # 合并时重编 mapping 下标：后包的 mapping 追加；questions 的索引若指向本包则偏移
        offset = len(merged_mappings)
        for m in parsed["mappings"]:
            # normalize field_id onto sources from locator
            for src in m.get("sources") or []:
                if isinstance(src, dict) and not src.get("field_id"):
                    loc = str(src.get("locator") or "")
                    if loc.startswith("field:"):
                        src["field_id"] = loc.split(":", 1)[1].strip()
            merged_mappings.append(m)
        for q in parsed["questions"]:
            item = dict(q)
            if item.get("executable") and isinstance(item.get("raw"), dict):
                raw_q = dict(item["raw"])
                indexes = raw_q.get("mapping_indexes")
                if isinstance(indexes, list) and offset:
                    raw_q["mapping_indexes"] = [
                        i + offset if isinstance(i, int) else i for i in indexes
                    ]
                item["raw"] = raw_q
            merged_questions.append(item)
        for a in parsed.get("alignments") or []:
            merged_alignments.append(dict(a))
        raws.append(raw)

    name_repairs = []
    if drafts:
        missing = _validate_draft_coverage(merged_mappings, drafts)
        if missing:
            raise AnalyzeError(ERR_DRAFT_MISSING, "draft fields missing", {"missing": missing})
        # 同子表重名会让桥丢掉后面的 field_id。这里改成 name_fieldid，字段仍全保留。
        sub_of = {}
        for bid, d in drafts.items():
            for f in d.get("fields") or []:
                sub_of[(bid, f["field_id"])] = f.get("subtable_id")
        seen_names = set()
        for m in merged_mappings:
            target = m.get("target") if isinstance(m.get("target"), dict) else None
            if not target:
                continue
            srcs = [s for s in (m.get("sources") or []) if isinstance(s, dict) and s.get("field_id")]
            if not srcs:
                continue
            src = srcs[0]
            fid = str(src.get("field_id"))
            sid = sub_of.get((src.get("block_id"), fid))
            name = str(target.get("name") or "").strip()
            key = (src.get("block_id"), sid, name)
            if key in seen_names:
                renamed = "%s_%s" % (name, fid)
                target["name"] = renamed
                name_repairs.append(
                    {"block_id": src.get("block_id"), "subtable_id": sid, "field_id": fid, "from": name, "to": renamed}
                )
            else:
                seen_names.add(key)

    dimensions_by_block = {}
    if drafts:
        name_by_fid = {}
        for m in merged_mappings:
            tname = str((m.get("target") or {}).get("name") or "").strip()
            for src in m.get("sources") or []:
                if isinstance(src, dict) and src.get("field_id") is not None:
                    name_by_fid[(src.get("block_id"), str(src.get("field_id")))] = tname
        # Z：优先从命名同次 JSON 取维度。不合法则再调一次（至多两调用），仍失败则整次失败。
        import json

        def _dim_prompt(prior_error):
            body = {
                str(bid): {
                    "fields": [
                        {
                            "field_id": f["field_id"],
                            "role": f["role"],
                            "target_name": name_by_fid.get((bid, f["field_id"]), ""),
                            "value_samples": f.get("value_samples") or [],
                            "subtable_id": f["subtable_id"],
                            "is_candidate": f["field_id"]
                            in (candidates.get(bid, {}).get(f["subtable_id"]) or []),
                        }
                        for f in d.get("fields") or []
                    ],
                    "dimension_candidates": candidates.get(bid),
                    "isomorphic": d.get("isomorphic"),
                }
                for bid, d in drafts.items()
            }
            required_keys = []
            skeleton = {}
            for _bid, d in drafts.items():
                cand = candidates.get(_bid) or {}
                for sid in d.get("subtable_ids") or []:
                    key = "subtable_%s_dimensions" % sid
                    required_keys.append(key)
                    allowed = list(cand.get(sid) or cand.get(str(sid)) or [])
                    fields = [
                        f
                        for f in d.get("fields") or []
                        if int(f.get("subtable_id") or 0) == int(sid)
                    ]
                    lists = [f["field_id"] for f in fields if f.get("role") == "list" and f["field_id"] in allowed]
                    if len(lists) == 1:
                        skeleton[key] = lists
                    elif len(lists) > 1:
                        skeleton[key] = lists
                    elif allowed:
                        skeleton[key] = [allowed[0]]
                    else:
                        skeleton[key] = []
            lines = [
                "【用户原话】\n" + (user_input or ""),
                "【任务】只从 dimension_candidates 里选 field_id。禁止选 is_candidate=false 的字段。",
                "每个子表至少留一个非维度字段，禁止把该子表全部 field_id 都放进维度。",
                "只有 column、没有 list 的子表：维度数组长度必须是 1。",
                "同构且只有一个 list：维度必须含该 list，长度 ≤2。",
                "有多个 list：维度就是这些 list 的 field_id，不要再加 attribute/column。",
                "必须同时输出这些键：%s。" % "、".join(required_keys),
                "可直接采用这个骨架（值已合法）：\n" + json.dumps(skeleton, ensure_ascii=False),
                "值是 field_id 字符串数组，不要输出对象。",
                "【草案与候选】\n" + json.dumps(body, ensure_ascii=False),
            ]
            if prior_error:
                lines.append("【上次非法】\n" + prior_error)
            return "\n\n".join(lines)

        dim_raw = None
        for raw in raws:
            if isinstance(raw, dict) and any(
                str(k).startswith("subtable_") and str(k).endswith("_dimensions") for k in raw
            ):
                dim_raw = raw
                break
        dimensions_by_block = None
        first_err = None
        if dim_raw is not None:
            try:
                dimensions_by_block = _parse_dimensions(dim_raw, candidates, drafts, name_by_fid)
            except AnalyzeError as exc:
                first_err = "%s %s" % (exc.code, exc.details)
        if dimensions_by_block is None:
            dim_raw = _chat_json_with_retry(
                _dim_prompt(first_err),
                api_key,
                max_tokens=int(config.get_param("llm.reserved_output_tokens")),
                temperature=0.1,
                timeout=int(config.get_param("api.timeout")),
                system="只输出合法 JSON 对象。",
                control=control,
                transport=transport,
            )
            retry_logs.append(get_last_retry_log())
            raws.append(dim_raw)
            dimensions_by_block = _parse_dimensions(dim_raw, candidates, drafts, name_by_fid)

    return {
        "ok": True,
        "mappings": merged_mappings,
        "questions": merged_questions,
        "alignments": merged_alignments,
        "ai_raws": raws,
        "budget": b_details,
        "packages": len(call_packages),
        "retry_log": retry_logs,
        "matrix_draft": drafts,
        "dimension_candidates": candidates,
        "dimensions_by_block": dimensions_by_block,
        "name_repairs": name_repairs,
    }
