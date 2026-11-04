"""跨表合并 API。本地函数不读配置、不读契约；模型函数每次只调用一次绑定的 chat。"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Literal

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.types import (
    CrossTableQuestion,
    DecisionLog,
    ExactMatchGroup,
    FieldRef,
    RowLink,
    SynonymCandidate,
)

# 当前签名 Literal 的闭集；扩展只能改契约枚举并同步本集合（本函数不读契约）。
_MATCH_TEXT_OPS = frozenset({"trim", "casefold", "width_fold"})
_EQUALITY_BASIS = frozenset({"target_name", "source_label", "either"})
_PROPOSAL_STATUS = frozenset({"ok", "ambiguous", "invalid"})
_REPLIES = frozenset({"confirm", "reject", "defer"})
_CHAT: Callable[[str], Any] | None = None


def bind_chat_json(fn: Callable[[str], Any] | None) -> None:
    """编排或单测注入一次 JSON 调用。模型函数自身不读配置。"""
    global _CHAT
    _CHAT = fn


def _one_model_call(prompt: str) -> Any:
    """Invoke the bound chat once.

    JSON mode: bind a client that already forces JSON (e.g. ai.client.chat_json,
    which sends response_format={"type":"json_object"}). This helper does not
    add a second transport path; it only forwards the prompt string.
    """
    if _CHAT is None:
        raise CrossTableError(
            "cross_table_input_invalid",
            "chat client is not bound",
        )
    return _CHAT(prompt)


def _as_block_map(block_plan: dict[str, Any] | None) -> dict[Any, dict[str, Any]]:
    found: dict[Any, dict[str, Any]] = {}
    blocks = (block_plan or {}).get("blocks") or []
    if not isinstance(blocks, list):
        return found
    for block in blocks:
        if not isinstance(block, dict):
            continue
        bid = block.get("block_id")
        found[bid] = block
        found[str(bid)] = block
    return found


def _draft_field(
    matrix_draft: dict[Any, Any] | None, block_id: Any, field_id: Any
) -> dict[str, Any] | None:
    if not matrix_draft or field_id is None:
        return None
    draft = matrix_draft.get(block_id)
    if draft is None:
        draft = matrix_draft.get(str(block_id))
    if not isinstance(draft, dict):
        return None
    for field in draft.get("fields") or []:
        if isinstance(field, dict) and str(field.get("field_id")) == str(field_id):
            return field
    return None


def _ordered_source_labels(sources: list[dict[str, Any]]) -> list[str]:
    """缺口 B2-1：全部非空 cell_text（strip），去重，按 row、col 升序。不按表头行过滤。"""
    ranked: list[tuple[int, int, str]] = []
    for source in sources:
        raw = source.get("cell_text")
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if text == "":
            continue
        row = source.get("row")
        col = source.get("col")
        ranked.append(
            (
                row if isinstance(row, int) else 0,
                col if isinstance(col, int) else 0,
                text,
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1]))
    labels: list[str] = []
    seen: set[str] = set()
    for _row, _col, text in ranked:
        if text in seen:
            continue
        seen.add(text)
        labels.append(text)
    return labels


def _in_dimensions(
    dimensions_by_block: dict[Any, dict[Any, list[str]]] | None,
    block_id: Any,
    field_id: Any,
) -> bool:
    if not dimensions_by_block or field_id is None:
        return False
    by_sub = dimensions_by_block.get(block_id)
    if by_sub is None:
        by_sub = dimensions_by_block.get(str(block_id))
    if not isinstance(by_sub, dict):
        return False
    want = str(field_id)
    for members in by_sub.values():
        if not isinstance(members, list):
            continue
        for member in members:
            if str(member) == want:
                return True
    return False


def collect_cross_table_field_refs(
    executable: list[Any],
    matrix_draft: dict[Any, Any],
    dimensions_by_block: dict[Any, dict[Any, list[str]]],
    block_plan: dict[str, Any],
) -> list[FieldRef]:
    """本地。全集只来自 executable。不读 mappings，不读 rejected，不调用模型。

    缺口 B2-1：一条 executable 项投影一个 FieldRef。source_labels 收集该项全部
    sources_resolved[].cell_text（strip 后非空），去重，按 row 升序、col 升序。
    不做「只收表头行 / 只收第一行」过滤。source_label 仅当 source_labels 长度恰为 1
    时取该值，否则空串。

    ordered（sources 上无 field_id）：role=column，subtable_id=None，
    value_samples=[]，in_dimensions=False，field_id 用 col:{parent_table_id}:{col}。
    matrix：role / subtable_id / value_samples 从 matrix_draft[block_id].fields 读取；
    value_samples 原样拷贝，不按条数截断。FieldRef 类型没有 segments，草案上的
    segments 只用于定位字段，不写入 FieldRef。

    parent_table_id 经 resolve_parent_table_id。找不到块 → cross_table_input_invalid。
    executable 不是 list → cross_table_input_invalid。无 target_name 的项不进入。
    """
    if not isinstance(executable, list):
        raise CrossTableError(
            "cross_table_input_invalid",
            "executable must be a list",
        )
    blocks = _as_block_map(block_plan)
    refs: list[FieldRef] = []
    for item in executable:
        if not isinstance(item, dict):
            raise CrossTableError(
                "cross_table_input_invalid",
                "executable item must be an object",
            )
        target_name = item.get("target_name")
        if not isinstance(target_name, str) or target_name == "":
            continue
        sources = item.get("sources_resolved") or []
        if not isinstance(sources, list) or not sources:
            continue
        source0 = sources[0] if isinstance(sources[0], dict) else None
        if source0 is None:
            raise CrossTableError(
                "cross_table_input_invalid",
                "source must be an object",
            )
        block_id = source0.get("block_id")
        if block_id is None:
            raise CrossTableError(
                "cross_table_input_invalid",
                "source missing block_id",
            )
        block = blocks.get(block_id)
        if block is None:
            block = blocks.get(str(block_id))
        if block is None:
            raise CrossTableError(
                "cross_table_input_invalid",
                "block_id not in block_plan",
                {"block_id": block_id},
            )
        parent = resolve_parent_table_id(source0, block, None)
        field_id = None
        for source in sources:
            if isinstance(source, dict) and source.get("field_id") not in (None, ""):
                field_id = str(source.get("field_id"))
                break
        labels = _ordered_source_labels([s for s in sources if isinstance(s, dict)])
        source_label = labels[0] if len(labels) == 1 else ""
        draft_field = _draft_field(matrix_draft, block_id, field_id)
        if field_id is None:
            col = source0.get("col")
            identity = "col:%s:%s" % (parent, col)
            role = "column"
            subtable_id = None
            samples: list[str] = []
            in_dim = False
        else:
            identity = field_id
            role = "column"
            subtable_id = None
            samples = []
            if isinstance(draft_field, dict):
                if isinstance(draft_field.get("role"), str):
                    role = draft_field["role"]
                subtable_id = draft_field.get("subtable_id")
                raw_samples = draft_field.get("value_samples")
                if isinstance(raw_samples, list):
                    samples = list(raw_samples)
            in_dim = _in_dimensions(dimensions_by_block, block_id, field_id)
        refs.append(
            FieldRef(
                field_id=identity,
                block_id=block_id,
                parent_table_id=parent,
                subtable_id=subtable_id,
                role=role,
                target_name=target_name,
                source_labels=labels,
                source_label=source_label,
                in_dimensions=in_dim,
                value_samples=samples,
            )
        )
    return refs


def normalize_match_text(
    text: str,
    ops: list[Literal["trim", "casefold", "width_fold"]],
) -> str:
    """本地。按 ops 顺序规范化比较文本。不调用模型。纯函数：不读配置、不读契约。

    设计缺口（类型安全扩展，不改变设计「业务路径无抛错」语义）：
    - None → 返回 ""（与空串相同出口）。
    - str → 按 ops 顺序处理。
    - 其他类型（int / list / dict / …）→ 抛 cross_table_input_invalid。

    ops：
    - 只执行列表里出现的步骤，顺序即执行顺序；不重排、不去重。
    - 未知步骤（非 trim / casefold / width_fold）→ cross_table_contract_invalid。
    - 不在本函数加额外预处理（去标点、去数字、截断等）；扩展只能走契约新版本新增枚举值，
      再同步本函数允许集——禁止在本函数或调用方硬编码额外步骤。
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        raise CrossTableError(
            "cross_table_input_invalid",
            "normalize_match_text text must be str or None",
        )
    if ops is None:
        raise CrossTableError(
            "cross_table_input_invalid",
            "normalize_match_text ops must be a list",
        )
    try:
        op_list = list(ops)
    except TypeError as exc:
        raise CrossTableError(
            "cross_table_input_invalid",
            "normalize_match_text ops must be a list",
        ) from exc
    for op in op_list:
        if op not in _MATCH_TEXT_OPS:
            raise CrossTableError(
                "cross_table_contract_invalid",
                "unknown normalize op",
                {"op": op},
            )
    s = text
    for op in op_list:
        if op == "trim":
            s = s.strip()
        elif op == "casefold":
            s = s.casefold()
        else:
            s = unicodedata.normalize("NFKC", s)
    return s


def _exact_group_candidate_id(
    members: list[FieldRef], matched_on: str
) -> str:
    """稳定 id：matched_on + 成员 (parent_table_id, field_id) 升序拼接。"""
    keys = sorted(
        (str(m["parent_table_id"]), str(m["field_id"])) for m in members
    )
    body = "\x1e".join(f"{p}\x1f{f}" for p, f in keys)
    return f"{matched_on}\x1f{body}"


def _group_by_normalized_field(
    field_refs: list[FieldRef],
    field_key: Literal["target_name", "source_label"],
    ops: list[Literal["trim", "casefold", "width_fold"]],
) -> list[ExactMatchGroup]:
    buckets: dict[str, list[FieldRef]] = defaultdict(list)
    for ref in field_refs:
        raw = ref.get(field_key)
        if raw is None:
            raw = ""
        if not isinstance(raw, str):
            raise CrossTableError(
                "cross_table_input_invalid",
                "field text must be str",
                {"field": field_key},
            )
        norm = normalize_match_text(raw, ops)
        if norm == "":
            continue
        buckets[norm].append(ref)
    groups: list[ExactMatchGroup] = []
    for _norm, members in sorted(buckets.items(), key=lambda item: item[0]):
        parent_ids = {m["parent_table_id"] for m in members}
        if len(parent_ids) == 0 or len(parent_ids) == 1:
            continue
        groups.append(
            ExactMatchGroup(
                candidate_id=_exact_group_candidate_id(members, field_key),
                member_refs=list(members),
                matched_on=field_key,
                decision_status="proposed",
            )
        )
    return groups


def group_exact_name_matches(
    field_refs: list[FieldRef],
    equality_basis: Literal["target_name", "source_label", "either"],
    ops: list[Literal["trim", "casefold", "width_fold"]],
) -> list[ExactMatchGroup]:
    """本地。精确同名候选。只保留成员 parent_table_id 不少于 2 的组。不调用模型。

    纯函数：ops / equality_basis 由调用方传入；不读配置、不读契约。

    equality_basis：
    - target_name：规范化后 target_name 相等成组。
    - source_label：规范化后 source_label 相等成组；空串不进组。
    - either：两条分支各自成组，不合并；每组 matched_on 标明依据。
    空串规范化后仍为空则不进组。equality_basis 不在枚举 → cross_table_contract_invalid，
    返回前不产生组。

    candidate_id：matched_on + 成员 (parent_table_id, field_id) 按字典序拼接
    （分隔符 \\x1f / \\x1e），同输入多次调用结果相同。
    """
    if equality_basis not in _EQUALITY_BASIS:
        raise CrossTableError(
            "cross_table_contract_invalid",
            "equality_basis not in enum",
            {"equality_basis": equality_basis},
        )
    if field_refs is None or not isinstance(field_refs, list):
        raise CrossTableError(
            "cross_table_input_invalid",
            "field_refs must be a list",
        )
    out: list[ExactMatchGroup] = []
    if equality_basis in ("target_name", "either"):
        out.extend(_group_by_normalized_field(field_refs, "target_name", ops))
    if equality_basis in ("source_label", "either"):
        out.extend(_group_by_normalized_field(field_refs, "source_label", ops))
    return out


def _ref_key(ref: dict[str, Any]) -> tuple[str, str]:
    return (str(ref.get("parent_table_id")), str(ref.get("field_id")))


def _lookup_refs(field_refs: list[FieldRef]) -> dict[tuple[str, str], FieldRef]:
    return {_ref_key(ref): ref for ref in field_refs}


def _members_from_pairs(pairs: Any, index: dict[tuple[str, str], FieldRef]) -> tuple[list[FieldRef], bool]:
    """返回 (成员, 是否引用了未知 field)。"""
    if not isinstance(pairs, list):
        return [], True
    members: list[FieldRef] = []
    unknown = False
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            unknown = True
            continue
        ref = index.get((str(pair[0]), str(pair[1])))
        if ref is None:
            unknown = True
            continue
        members.append(ref)
    return members, unknown


def _exact_pair_set(exact_groups: list[ExactMatchGroup]) -> list[set[tuple[str, str]]]:
    sets = []
    for group in exact_groups or []:
        sets.append({_ref_key(ref) for ref in group.get("member_refs") or []})
    return sets


def _overlaps_exact(members: list[FieldRef], exact_sets: list[set[tuple[str, str]]]) -> bool:
    got = {_ref_key(ref) for ref in members}
    for other in exact_sets:
        if got & other:
            return True
    return False


def propose_synonym_candidates(
    field_refs: list[FieldRef],
    exact_groups: list[ExactMatchGroup],
    user_utterance: str,
    member_set_rule: str,
) -> list[SynonymCandidate]:
    """AI。每次只调模型一次。不内部重试，不读配置。

    模型返回不是数组、缺成员、引用未知 field、或与精确组相交 → proposal_status=invalid，
    交给 validate_ai_proposal 标 needs_retry.synonym。不把精确组改写成近义组。
    member_set_rule 原样放进 prompt，本函数不改写这句话。
    """
    index = _lookup_refs(field_refs)
    exact_sets = _exact_pair_set(exact_groups)
    payload = {
        "user_utterance": user_utterance,
        "member_set_rule": member_set_rule,
        "field_refs": [
            {
                "parent_table_id": ref.get("parent_table_id"),
                "field_id": ref.get("field_id"),
                "role": ref.get("role"),
                "target_name": ref.get("target_name"),
                "source_label": ref.get("source_label"),
                "source_labels": ref.get("source_labels"),
            }
            for ref in field_refs
        ],
        "exact_groups": [
            {
                "candidate_id": group.get("candidate_id"),
                "members": [_ref_key(ref) for ref in group.get("member_refs") or []],
            }
            for group in exact_groups or []
        ],
    }
    prompt = (
        "Return one JSON object with key candidates. "
        "Each candidate has candidate_id, member_pairs [[parent_table_id, field_id]], "
        "proposal_status one of ok, ambiguous, invalid. "
        "Do not repeat exact_groups. If there is no extra set, return {\"candidates\": []}. "
        + json.dumps(payload, ensure_ascii=False)
    )
    raw = _one_model_call(prompt)
    from pipeline.block_cross_table.join_hypothesis import LAST_EXCHANGE

    LAST_EXCHANGE.clear()
    LAST_EXCHANGE["prompt"] = prompt
    LAST_EXCHANGE["raw"] = raw
    from pipeline.block_cross_table.b3c import _extract_rows

    rows = _extract_rows(raw, "candidates")
    if not isinstance(rows, list):
        return [
            SynonymCandidate(
                candidate_id="invalid",
                member_refs=[],
                proposal_status="invalid",
                ai_raw={"body": raw},
                decision_status="proposed",
            )
        ]
    out: list[SynonymCandidate] = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(
                SynonymCandidate(
                    candidate_id="invalid",
                    member_refs=[],
                    proposal_status="invalid",
                    ai_raw={"body": row},
                    decision_status="proposed",
                )
            )
            continue
        members, unknown = _members_from_pairs(row.get("member_pairs"), index)
        status = row.get("proposal_status")
        if status not in _PROPOSAL_STATUS:
            status = "invalid"
        if unknown or not members:
            status = "invalid"
        elif _overlaps_exact(members, exact_sets):
            continue
        parents = {str(ref.get("parent_table_id")) for ref in members}
        if len(parents) == 0 or len(parents) == 1:
            continue
        out.append(
            SynonymCandidate(
                candidate_id=str(row.get("candidate_id") or "invalid"),
                member_refs=members,
                proposal_status=status,  # type: ignore[typeddict-item]
                ai_raw=row,
                decision_status="proposed",
            )
        )
    return out




def validate_ai_proposal(
    bindings: list[Any],
    synonyms: list[SynonymCandidate],
    field_refs: list[FieldRef],
    user_utterance: str = "",
) -> dict[str, Any]:
    """本地。needs_retry 仅同义词结构不完整。ambiguous 走人机且不重试。不校验别名。不调用模型。"""
    from pipeline.block_cross_table.b3c import validate_ai_proposal as _impl

    return _impl(bindings, synonyms, field_refs, user_utterance)




def apply_cross_table_reply(
    question: CrossTableQuestion,
    reply: Literal["confirm", "reject", "defer"],
    reply_text: str,
    selected_field_id: str | None,
) -> ExactMatchGroup | SynonymCandidate | dict[str, Any]:
    """本地。不改入参。selected_field_id 仅 align_key_pick+confirm 可非空。"""
    from pipeline.block_cross_table.b3c import apply_cross_table_reply as _impl

    return _impl(question, reply, reply_text, selected_field_id)




def propose_value_alias_links(
    align_keys: list[dict[str, Any]],
    key_values: dict[str, list[str]],
    user_utterance: str,
) -> list[RowLink]:
    """AI。每次只调模型一次。空键列返回 []。端点非法则整批丢弃。"""
    from pipeline.block_cross_table.b3c import propose_value_alias_links as _impl

    return _impl(align_keys, key_values, user_utterance)




def write_decision_log(
    log: DecisionLog,
    session_dir: str,
) -> dict[str, str]:
    """本地。父表少于两张不得写。目录不可写 → cross_table_log_io。不读配置。"""
    from pipeline.block_cross_table.b3c import write_decision_log as _impl

    return _impl(log, session_dir)


def replay_decision_log(
    log: DecisionLog,
    fingerprint: str,
) -> DecisionLog:
    """本地。指纹一致则返回日志，不调模型。"""
    from pipeline.block_cross_table.b3c import replay_decision_log as _impl

    return _impl(log, fingerprint)


def assert_r2_keys_unchanged(
    before_keys: list[list[str]],
    after_keys: list[list[str]],
) -> dict[str, bool]:
    """本地。集合不等 → cross_table_r2_key_mutation_forbidden。"""
    from pipeline.block_cross_table.b3c import assert_r2_keys_unchanged as _impl

    return _impl(before_keys, after_keys)


def derive_exact_links(
    align_keys: list[dict[str, Any]],
    key_values: dict[str, list[str]],
    ops: list[Literal["trim", "casefold", "width_fold"]],
) -> list[RowLink]:
    """本地。空键列不报错。缺条目 → cross_table_input_invalid。"""
    from pipeline.block_cross_table.b3c import derive_exact_links as _impl

    return _impl(align_keys, key_values, ops)




def snapshot_r2_keys(
    executable: list[Any],
    block_plan: dict[str, Any],
) -> list[list[str]]:
    """本地。每条 source 一个二元组，按 (parent_table_id, 键) 排序。不读 subtables。

    缺口 B2-3：用 source.block_id 在 block_plan.blocks 查 parent_table_ids。
    有 field_id → [parent_table_id, "field:"+field_id]；否则 [parent_table_id, "col:"+col]。
    找不到块、缺 block_id、或 parent_table_ids 长度不是 1 → cross_table_input_invalid
    （投影输入错误，不是 resolve_parent_table_id 的边界防御码）。
    """
    if executable is None:
        executable = []
    if not isinstance(executable, list):
        raise CrossTableError(
            "cross_table_input_invalid",
            "executable must be a list",
        )
    blocks = _as_block_map(block_plan)
    pairs: list[list[str]] = []
    for item in executable:
        if not isinstance(item, dict):
            raise CrossTableError(
                "cross_table_input_invalid",
                "executable item must be an object",
            )
        for source in item.get("sources_resolved") or []:
            if not isinstance(source, dict):
                raise CrossTableError(
                    "cross_table_input_invalid",
                    "source must be an object",
                )
            block_id = source.get("block_id")
            if block_id is None:
                raise CrossTableError(
                    "cross_table_input_invalid",
                    "source missing block_id",
                )
            block = blocks.get(block_id)
            if block is None:
                block = blocks.get(str(block_id))
            if block is None:
                raise CrossTableError(
                    "cross_table_input_invalid",
                    "block_id not in block_plan",
                    {"block_id": block_id},
                )
            parents = block.get("parent_table_ids")
            if not isinstance(parents, list) or len(parents) != 1:
                raise CrossTableError(
                    "cross_table_input_invalid",
                    "block parent_table_ids length is not 1",
                    {"block_id": block_id},
                )
            parent = str(parents[0])
            field_id = source.get("field_id")
            if field_id not in (None, ""):
                pairs.append([parent, "field:" + str(field_id)])
            else:
                pairs.append([parent, "col:" + str(source.get("col"))])
    pairs.sort(key=lambda pair: (pair[0], pair[1]))
    return pairs


def _binding_matches_item(item: dict[str, Any], binding: dict) -> bool:
    sources = item.get("sources_resolved") or []
    members = list(binding.get("member_refs") or [])
    for side in ("left", "right"):
        endpoint = binding.get(side)
        if isinstance(endpoint, dict):
            members.append(endpoint)
    for member in members:
        if not isinstance(member, dict):
            continue
        for source in sources:
            if not isinstance(source, dict):
                continue
            if str(source.get("block_id")) != str(member.get("block_id")):
                continue
            source_field = source.get("field_id")
            member_field = member.get("field_id")
            if source_field not in (None, "") and str(source_field) == str(member_field):
                return True
            if source_field in (None, "") and item.get("target_name") == member.get(
                "target_name"
            ):
                return True
    return False


def copy_executable_with_bindings(
    executable: list[Any],
    bindings: list[Any],
) -> list[Any]:
    """本地。浅拷贝 executable，在新项顶层写 join_hypothesis_ids。

    新 dict 拷贝顶层键；sources_resolved 换成新 list，元素仍是原 dict。
    只收录 decision_status=confirmed 的 hypothesis_id。未命中的项写空列表。
    不修改原 executable 项，也不修改 source 元素。
    """
    if executable is None:
        executable = []
    if not isinstance(executable, list):
        raise CrossTableError(
            "cross_table_input_invalid",
            "executable must be a list",
        )
    confirmed = [
        binding
        for binding in (bindings or [])
        if isinstance(binding, dict) and binding.get("decision_status") == "confirmed"
    ]
    copied: list[Any] = []
    for item in executable:
        if not isinstance(item, dict):
            raise CrossTableError(
                "cross_table_input_invalid",
                "executable item must be an object",
            )
        new_item = {key: value for key, value in item.items()}
        new_item["sources_resolved"] = list(item.get("sources_resolved") or [])
        ids: list[str] = []
        for binding in confirmed:
            if _binding_matches_item(item, binding):
                hypothesis_id = binding.get("hypothesis_id")
                if isinstance(hypothesis_id, str) and hypothesis_id not in ids:
                    ids.append(hypothesis_id)
        new_item["join_hypothesis_ids"] = ids
        copied.append(new_item)
    return copied


def project_upstream_rectangles(
    block: dict[str, Any] | None,
    draft: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """本地。合并矩形与草案字段列范围投影。不扫网格、不调模型。"""
    from pipeline.block_cross_table.layer_recognize import project_upstream_rectangles as _impl

    return _impl(block, draft)


def project_subtable_boundaries(
    draft: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """本地。每个已有子表标识的定位包络。不调模型。"""
    from pipeline.block_cross_table.layer_recognize import project_subtable_boundaries as _impl

    return _impl(draft)


def compute_containment_facts(
    rectangles: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """本地。只在已收集矩形之间算包含关系。"""
    from pipeline.block_cross_table.layer_recognize import compute_containment_facts as _impl

    return _impl(rectangles)


def validate_layer_proposal(
    rectangles: list[dict[str, Any]],
    containment_facts: list[dict[str, str]],
    proposal: dict[str, Any],
    draft: dict[str, Any] | None,
    dimensions_for_block: dict[Any, list[str]] | None = None,
) -> dict[str, Any]:
    """本地。方案 B：矩形→层映射 + 层有序列表下标校验包含一致性。"""
    from pipeline.block_cross_table.layer_recognize import validate_layer_proposal as _impl

    return _impl(rectangles, containment_facts, proposal, draft, dimensions_for_block)


def shrink_review_locator_ids(
    recognized: dict[str, Any],
    requested_ids: list[Any],
) -> set[str]:
    """本地。缩小本次复核覆盖的定位子集；不改层定义。"""
    from pipeline.block_cross_table.layer_recognize import shrink_review_locator_ids as _impl

    return _impl(recognized, requested_ids)


def run_layer_recognize_block(
    block: dict[str, Any],
    draft: dict[str, Any] | None,
    tables_by_id: dict[str, Any],
    dimensions_for_block: dict[Any, list[str]] | None,
    user_utterance: str,
    review_input_capacity: int | None,
    chat: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """A' 单块识别。容量由调用方从契约传入；缺则结构错误。"""
    from pipeline.block_cross_table.layer_recognize import run_layer_recognize_block as _impl

    return _impl(
        block,
        draft,
        tables_by_id,
        dimensions_for_block,
        user_utterance,
        review_input_capacity,
        chat,
    )


def resolve_parent_table_id(
    source: dict[str, Any],
    block: dict[str, Any],
    matrix_draft: dict[str, Any] | None,
) -> str:
    """边界防御。路径 1 下 parent_table_ids 长度恰为 1 时返回该唯一元素。

    缺口 B2-2：长度不是 1（含空、2、3）→ cross_table_parent_table_unresolvable。
    正常路径不触发。不读配置、不读契约。source / matrix_draft 不参与猜测。
    """
    del source, matrix_draft
    parents = block.get("parent_table_ids") if isinstance(block, dict) else None
    if not isinstance(parents, list) or len(parents) != 1:
        raise CrossTableError(
            "cross_table_parent_table_unresolvable",
            "parent_table_ids length is not 1",
        )
    return str(parents[0])
