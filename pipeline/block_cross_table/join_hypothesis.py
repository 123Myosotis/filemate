"""关联假设：模型提案、集合排序、结构校验、问题与落盘。提案函数不读配置。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from pipeline.block_cross_table.errors import CrossTableError

_SEQ = 0
LAST_EXCHANGE: dict[str, Any] = {}


def _contract_cross() -> dict:
    from pipeline.synth_contract.registry import load_contract

    return (load_contract().data or {}).get("cross_table") or {}


def _reason(name: str) -> str:
    catalog = _contract_cross().get("join_reason_codes") or []
    if name not in catalog:
        raise CrossTableError("cross_table_contract_invalid", "join reason missing")
    return name


def _join_types() -> set[str]:
    enums = (_contract_cross().get("enums") or {}).get("join_type") or []
    return {str(item) for item in enums}


def _templates() -> dict:
    templates = _contract_cross().get("join_question_templates") or {}
    if "ask_columns" not in templates or "pair" not in templates:
        raise CrossTableError("cross_table_contract_invalid", "join templates missing")
    return templates


def _endpoint(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    parent = raw.get("parent_table_id")
    field_id = raw.get("field_id")
    if parent in (None, "") or field_id in (None, ""):
        return None
    return {
        "parent_table_id": str(parent),
        "block_id": raw.get("block_id"),
        "field_id": str(field_id),
    }


def _similar_name_groups(field_refs):
    """用 semantic_match 的 NFKC/包含匹配，找出「名字相近但非精确」的字段对。

    只作种子线索（模型最终裁决），不直接建 join，避免本地误配。
    """
    try:
        from pipeline.stage3_processor import semantic_match
    except Exception:
        return []
    items = []
    for ref in field_refs or []:
        nm = str(ref.get("target_name") or ref.get("source_label") or "").strip()
        if nm:
            items.append((ref, nm))
    groups = []
    for i in range(len(items)):
        ra, na = items[i]
        for j in range(i + 1, len(items)):
            rb, nb = items[j]
            col, score = semantic_match.match_column([nb], na)
            if col and score >= 0.5:
                groups.append({
                    "left": [ra.get("parent_table_id"), ra.get("field_id")],
                    "right": [rb.get("parent_table_id"), rb.get("field_id")],
                    "score": round(score, 3),
                })
    return groups


def propose_join_hypotheses(user_utterance, field_refs, exact_groups, value_samples_by_field, prior_rejections=None):
    """每次只调模型一次。空数组合法。不读配置，不内部重试。"""
    from pipeline.block_cross_table.api import _one_model_call
    from pipeline.block_cross_table.b3c import _extract_rows

    payload = {
        "user_utterance": user_utterance,
        "field_refs": [
            {
                "parent_table_id": ref.get("parent_table_id"),
                "block_id": ref.get("block_id"),
                "field_id": ref.get("field_id"),
                "source_label": ref.get("source_label"),
                "target_name": ref.get("target_name"),
            }
            for ref in field_refs or []
        ],
        "exact_groups": [
            {
                "candidate_id": group.get("candidate_id"),
                "members": [
                    [ref.get("parent_table_id"), ref.get("field_id")]
                    for ref in group.get("member_refs") or []
                ],
            }
            for group in exact_groups or []
        ],
        "value_samples_by_field": value_samples_by_field or {},
        "similar_groups": _similar_name_groups(field_refs),
        "prior_rejections": prior_rejections or [],
    }
    prompt = "\n".join(
        [
            "Return one JSON object with key hypotheses (array).",
            "Each hypothesis has hypothesis_id (unique in this array),",
            "left {parent_table_id, block_id, field_id},",
            "right {parent_table_id, block_id, field_id},",
            "join_type one of left, inner, full,",
            "rationale a non-empty string.",
            "similar_groups are partial-name hints (contains/reverse-contains), not exact matches; use them only as candidates.",
            "An empty hypotheses array is valid.",
            "Do not invent field_id values.",
            json.dumps(payload, ensure_ascii=False),
        ]
    )
    raw = _one_model_call(prompt)
    LAST_EXCHANGE.clear()
    LAST_EXCHANGE["prompt"] = prompt
    LAST_EXCHANGE["raw"] = raw
    rows = _extract_rows(raw, "hypotheses")
    if rows is None and isinstance(raw, dict) and raw.get("hypotheses") is None:
        rows = []
    if not isinstance(rows, list):
        rows = []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(
                {
                    "hypothesis_id": "",
                    "left": {},
                    "right": {},
                    "join_type": "",
                    "rationale": "",
                    "ai_raw": {"body": row},
                }
            )
            continue
        hid = row.get("hypothesis_id")
        if isinstance(hid, str):
            hid_text = hid
        elif isinstance(hid, int) and not isinstance(hid, bool):
            hid_text = str(hid)
        else:
            hid_text = ""
        out.append(
            {
                "hypothesis_id": hid_text,
                "left": row.get("left") if isinstance(row.get("left"), dict) else {},
                "right": row.get("right") if isinstance(row.get("right"), dict) else {},
                "join_type": row.get("join_type") if isinstance(row.get("join_type"), str) else "",
                "rationale": row.get("rationale") if isinstance(row.get("rationale"), str) else "",
                "ai_raw": row,
            }
        )
    return out


def validate_join_hypothesis(hypotheses, field_refs):
    known = {str(ref.get("field_id")) for ref in field_refs or []}
    allowed = _join_types()
    reasons: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        code = _reason(name)
        if code not in reasons:
            reasons.append(code)

    for item in hypotheses or []:
        left = _endpoint(item.get("left"))
        right = _endpoint(item.get("right"))
        if left is None or left["field_id"] not in known or right is None or right["field_id"] not in known:
            add("unknown_field")
        elif left["parent_table_id"] == right["parent_table_id"]:
            add("same_table")
        if item.get("join_type") not in allowed:
            add("invalid_join_type")
        hid = item.get("hypothesis_id")
        if not isinstance(hid, str) or hid == "" or hid in seen:
            add("duplicate_hypothesis_id")
        elif isinstance(hid, str):
            seen.add(hid)
        rationale = item.get("rationale")
        if not isinstance(rationale, str) or rationale == "":
            add("empty_rationale")
    needs = bool(reasons)
    return {"ok": not needs, "needs_retry": needs, "reason_codes": reasons}


def rank_hypotheses(hypotheses, value_samples_by_field, top_n):
    samples = value_samples_by_field or {}
    ranked = []
    for item in hypotheses or []:
        left = _endpoint(item.get("left")) or {}
        right = _endpoint(item.get("right")) or {}
        left_vals = set(map(str, samples.get(left.get("field_id")) or []))
        right_vals = set(map(str, samples.get(right.get("field_id")) or []))
        common = len(left_vals & right_vals)
        copied = dict(item)
        copied["coverage_ratio"] = common / float(max(len(left_vals), 1))
        ranked.append(copied)
    ranked.sort(key=lambda row: row.get("coverage_ratio") or 0, reverse=True)
    limit = int(top_n)
    if limit < 0:
        limit = 0
    return ranked[:limit]


def build_join_questions(ranked, top_n):
    templates = _templates()
    texts = templates.get("join_type_text") or {}
    questions = []
    for item in (ranked or [])[: int(top_n)]:
        left = item.get("left") or {}
        right = item.get("right") or {}
        left_label = str(item.get("left_label") or left.get("field_id") or "")
        right_label = str(item.get("right_label") or right.get("field_id") or "")
        join_type = str(item.get("join_type") or "")
        pair = str(templates["pair"]).format(left_label=left_label, right_label=right_label)
        questions.append(
            {
                "question_id": "join_hypothesis_pick:" + str(item.get("hypothesis_id")),
                "interrupt_kind": "join_hypothesis_pick",
                "hypothesis_id": item.get("hypothesis_id"),
                "ask_user": pair,
                "join_type": join_type,
                "join_type_text": texts.get(join_type, join_type),
                "coverage_ratio": item.get("coverage_ratio"),
                "rationale": item.get("rationale"),
                "allowed_replies": ["confirm", "reject", "rethink", "specify"],
            }
        )
    questions.append(
        {
            "question_id": "join_hypothesis_pick:none",
            "interrupt_kind": "join_hypothesis_pick",
            "hypothesis_id": None,
            "ask_user": templates["none_match"],
            "join_type": None,
            "join_type_text": "",
            "coverage_ratio": None,
            "rationale": "",
            "allowed_replies": ["specify", "rethink"],
        }
    )
    return questions


def column_question(field_refs):
    templates = _templates()
    return {
        "question_id": "join_hypothesis_pick:ask",
        "interrupt_kind": "join_hypothesis_pick",
        "hypothesis_id": None,
        "ask_user": templates["ask_columns"],
        "field_refs": list(field_refs or []),
        "allowed_replies": ["specify"],
    }


def propose_output_spec(user_utterance, field_refs, chosen_hypothesis, sample_rows):
    from pipeline.block_cross_table.api import _one_model_call
    from pipeline.block_cross_table.b3c import _extract_rows

    payload = {
        "user_utterance": user_utterance,
        "field_refs": [
            {
                "field_id": ref.get("field_id"),
                "parent_table_id": ref.get("parent_table_id"),
                "source_label": ref.get("source_label"),
            }
            for ref in field_refs or []
        ],
        "chosen_hypothesis": chosen_hypothesis,
        "sample_rows": sample_rows or [],
    }
    prompt = "\n".join(
        [
            "Return one JSON object with key specs (array).",
            "Each spec has output_spec_id, columns [{field_id, role keep|melt_group|melt_attr}],",
            "row_shape wide|long, output_grain per_row|per_concept_group, rationale non-empty.",
            json.dumps(payload, ensure_ascii=False),
        ]
    )
    raw = _one_model_call(prompt)
    LAST_EXCHANGE.clear()
    LAST_EXCHANGE["prompt"] = prompt
    LAST_EXCHANGE["raw"] = raw
    rows = _extract_rows(raw, "specs")
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "output_spec_id": str(row.get("output_spec_id") or ""),
                "columns": row.get("columns") if isinstance(row.get("columns"), list) else [],
                "row_shape": row.get("row_shape") if row.get("row_shape") in ("wide", "long") else "",
                "output_grain": row.get("output_grain") if isinstance(row.get("output_grain"), str) else "",
                "rationale": row.get("rationale") if isinstance(row.get("rationale"), str) else "",
                "ai_raw": row,
            }
        )
    return out


def build_output_questions(specs):
    questions = []
    for spec in specs or []:
        questions.append(
            {
                "question_id": "output_spec_pick:" + str(spec.get("output_spec_id")),
                "interrupt_kind": "output_spec_pick",
                "output_spec_id": spec.get("output_spec_id"),
                "ask_user": str(spec.get("rationale") or spec.get("output_spec_id") or ""),
                "rationale": spec.get("rationale"),
                "allowed_replies": ["confirm", "reject"],
            }
        )
    return questions


def write_trace(kind: str, body: dict, clock: Callable[[], str]) -> str:
    import config

    directory = config.get_param("cross_table.trace_dir")
    pattern = config.get_param("cross_table.trace_filename_pattern")
    if not isinstance(directory, str) or directory == "" or not isinstance(pattern, str) or pattern == "":
        raise CrossTableError("trace_io_failed", "trace settings missing")
    global _SEQ
    _SEQ += 1
    ts = str(clock()).replace(":", "").replace("-", "")
    name = pattern.replace("{kind}", str(kind)).replace("{seq}", str(_SEQ)).replace("{ts}", ts)
    root = Path(directory)
    if not root.is_absolute():
        root = Path(__file__).resolve().parents[2] / directory
    target = root / name
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        raise CrossTableError("trace_io_failed", str(exc)) from exc
    if target.stat().st_size == 0:
        raise CrossTableError("trace_io_failed", "empty trace")
    return str(target.relative_to(Path(__file__).resolve().parents[2]))


def reset_trace_seq() -> None:
    global _SEQ
    _SEQ = 0


def materialize_join(chosen, tables_by_id, field_cols_by_parent):
    """按 join_type 做等值连接。行 0 视为表头，不进入结果。"""
    left = _endpoint(chosen.get("left"))
    right = _endpoint(chosen.get("right"))
    if left is None or right is None:
        raise CrossTableError("cross_table_join_hypothesis_invalid", "endpoint missing")
    join_type = chosen.get("join_type")
    if join_type not in _join_types():
        raise CrossTableError("cross_table_join_hypothesis_invalid", "join_type")
    left_table = (tables_by_id or {}).get(left["parent_table_id"]) or {}
    right_table = (tables_by_id or {}).get(right["parent_table_id"]) or {}
    left_grid = left_table.get("grid") or []
    right_grid = right_table.get("grid") or []
    left_cols = (field_cols_by_parent or {}).get(left["parent_table_id"]) or {}
    right_cols = (field_cols_by_parent or {}).get(right["parent_table_id"]) or {}
    left_key_col = left_cols.get(left["field_id"])
    right_key_col = right_cols.get(right["field_id"])

    def cell(grid, row, col):
        if col is None or row >= len(grid):
            return ""
        line = grid[row]
        if not isinstance(col, int) or col >= len(line):
            return ""
        value = line[col]
        return "" if value is None else str(value)

    right_index: dict[str, list[int]] = {}
    for row_i, _line in enumerate(right_grid):
        if row_i == 0:
            continue
        text = cell(right_grid, row_i, right_key_col)
        if text == "":
            continue
        right_index.setdefault(text, []).append(row_i)

    def pack(row_i, other_i):
        packed = {}
        for field_id, col in left_cols.items():
            packed[field_id] = cell(left_grid, row_i, col) if row_i is not None else ""
        for field_id, col in right_cols.items():
            packed[field_id] = cell(right_grid, other_i, col) if other_i is not None else ""
        return packed

    rows = []
    unmatched = []
    used_right: set[int] = set()
    for row_i, _line in enumerate(left_grid):
        if row_i == 0:
            continue
        key = cell(left_grid, row_i, left_key_col)
        hits = right_index.get(key, []) if key != "" else []
        if not hits:
            if join_type == "inner":
                continue
            rows.append(pack(row_i, None))
            unmatched.append(
                {
                    "parent_table_id": left["parent_table_id"],
                    "field_id": left["field_id"],
                    "value": key,
                    "disposition": "retain_unmatched",
                }
            )
            continue
        for other_i in hits:
            used_right.add(other_i)
            rows.append(pack(row_i, other_i))
    if join_type == "full":
        for row_i, _line in enumerate(right_grid):
            if row_i == 0 or row_i in used_right:
                continue
            rows.append(pack(None, row_i))
            unmatched.append(
                {
                    "parent_table_id": right["parent_table_id"],
                    "field_id": right["field_id"],
                    "value": cell(right_grid, row_i, right_key_col),
                    "disposition": "retain_unmatched",
                }
            )
    if join_type == "inner":
        unmatched = []
    return {"rows": rows, "unmatched_rows": unmatched}


def project_output(spec, expanded_rows):
    columns = []
    for column in (spec or {}).get("columns") or []:
        if isinstance(column, dict) and column.get("field_id"):
            columns.append(str(column.get("field_id")))
    if not columns and expanded_rows:
        columns = list((expanded_rows[0] or {}).keys())
    projected = []
    for raw in expanded_rows or []:
        projected.append({column: "" if raw.get(column) is None else str(raw.get(column) or "") for column in columns})
    return {
        "columns": columns,
        "rows": projected,
        "output_grain": (spec or {}).get("output_grain") or "per_row",
        "row_shape": (spec or {}).get("row_shape") or "wide",
        "unmatched_rows": [],
    }
