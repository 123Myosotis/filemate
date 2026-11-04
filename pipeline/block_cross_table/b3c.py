"""B3 本地函数与 C 模型函数的实现。不读配置、不读契约。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.types import (
    CrossTableQuestion,
    ExactMatchGroup,
    FieldRef,
    RowLink,
    SynonymCandidate,
)

_PROPOSAL_STATUS = frozenset({"ok", "ambiguous", "invalid"})
_REPLIES = ("confirm", "reject", "defer")
_STATUS_MAP = {"confirm": "confirmed", "reject": "rejected", "defer": "deferred"}


def _ref_key(ref: dict[str, Any]) -> tuple[str, str]:
    return (str(ref.get("parent_table_id")), str(ref.get("field_id")))


def _index(field_refs: list[FieldRef]) -> dict[tuple[str, str], FieldRef]:
    return {_ref_key(ref): ref for ref in field_refs}


def _members_from_pairs(pairs: Any, index: dict[tuple[str, str], FieldRef]) -> tuple[list[FieldRef], bool]:
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


def _extract_rows(raw: Any, key: str) -> list | None:
    """从模型 JSON 取出命名数组；兼容嵌套容器。不猜业务。"""
    stack = [raw]
    seen: set[int] = set()
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, dict):
            rows = cur.get(key)
            if isinstance(rows, list):
                return rows
            if key == "bindings" and isinstance(cur.get("member_pairs"), list):
                return [cur]
            for value in cur.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(cur, list):
            for value in cur:
                if isinstance(value, (dict, list)):
                    stack.append(value)
    return None



def _reason_catalog() -> dict[str, str]:
    """原因码只来自契约 needs_retry_reason_codes，本函数不另建集合。"""
    try:
        from pipeline.synth_contract.registry import load_contract

        cross = (load_contract().data or {}).get("cross_table") or {}
    except Exception:
        cross = {}
    raw = cross.get("needs_retry_reason_codes") or []
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    return {str(item): str(item) for item in raw}


def contract_reason(name: str) -> str:
    catalog = _reason_catalog()
    if name not in catalog:
        raise CrossTableError("cross_table_contract_invalid", "reason code missing")
    return catalog[name]


def validate_ai_proposal(bindings, synonyms, field_refs, user_utterance: str = ""):
    """needs_retry 仅当同义词缺必填、成员为空、proposal_status=invalid。ambiguous 走人机。bindings 参数保留兼容，不读。"""
    _ = bindings
    _ = user_utterance
    known = {_ref_key(ref) for ref in field_refs or []}
    needs = {"concept": False, "synonym": False}
    reasons: dict[str, list[str]] = {"concept": [], "synonym": []}
    questions: list[CrossTableQuestion] = []

    def add(kind: str, name: str) -> None:
        code = contract_reason(name)
        if code not in reasons[kind]:
            reasons[kind].append(code)

    def bad_members(members: list) -> str | None:
        if not members:
            return "missing_required"
        for member in members:
            if _ref_key(member) not in known:
                return "unknown_member"
        return None

    for syn in synonyms or []:
        members = list(syn.get("member_refs") or [])
        status = syn.get("proposal_status")
        cid = syn.get("candidate_id")
        if not isinstance(cid, str) or cid == "" or status not in _PROPOSAL_STATUS:
            needs["synonym"] = True
            add("synonym", "missing_required")
            continue
        member_problem = bad_members(members)
        if member_problem is not None or status == "invalid":
            needs["synonym"] = True
            add("synonym", member_problem or "missing_required")
            continue
        if status == "ambiguous":
            questions.append(_question("synonym_merge", cid, members, "synonym_merge"))

    ok = not needs["synonym"] and not questions
    return {"ok": ok, "questions": questions, "needs_retry": needs, "reason_codes": reasons}


def _question(kind: str, candidate_id: str, members: list, action: str) -> CrossTableQuestion:
    return CrossTableQuestion(
        question_id=kind + ":" + candidate_id,
        interrupt_kind=kind,  # type: ignore[typeddict-item]
        hypothesis=action,
        evidence=candidate_id,
        if_holds=action,
        ask_user=action + " " + candidate_id,
        candidate_id=candidate_id,
        field_refs=list(members),
        allowed_replies=["confirm", "reject", "defer"],
        recommended_reply=None,
    )



def apply_cross_table_reply(question, reply, reply_text, selected_field_id):
    """不修改入参。协议违反时抛 cross_table_reply_invalid。"""
    allowed = list(question.get("allowed_replies") or [])
    if reply not in allowed or reply not in _REPLIES:
        raise CrossTableError("cross_table_reply_invalid", "reply not allowed")
    kind = question.get("interrupt_kind")
    members = list(question.get("field_refs") or [])
    member_ids = {str(ref.get("field_id")) for ref in members}
    if kind == "align_key_pick" and reply == "confirm":
        if selected_field_id is None or str(selected_field_id) not in member_ids:
            raise CrossTableError("cross_table_reply_invalid", "selected_field_id required")
    elif selected_field_id is not None:
        raise CrossTableError("cross_table_reply_invalid", "selected_field_id must be null")
    status = _STATUS_MAP[reply]
    if kind == "align_key_pick":
        parent = ""
        if members:
            parent = str(members[0].get("parent_table_id"))
        return {
            "hypothesis_id": str(question.get("candidate_id")),
            "parent_table_id": parent,
            "field_id": str(selected_field_id) if reply == "confirm" else None,
            "rule": "user_picked",
            "status": "resolved" if reply == "confirm" else "ambiguous",
        }
    if kind == "exact_merge":
        return ExactMatchGroup(
            candidate_id=str(question.get("candidate_id")),
            member_refs=members,
            matched_on="target_name",
            decision_status=status,  # type: ignore[typeddict-item]
        )
    if kind == "synonym_merge":
        return SynonymCandidate(
            candidate_id=str(question.get("candidate_id")),
            member_refs=members,
            proposal_status="ok",
            ai_raw={"reply_text": reply_text},
            decision_status=status,  # type: ignore[typeddict-item]
        )
    raise CrossTableError("cross_table_reply_invalid", "interrupt_kind not supported")



def propose_value_alias_links(align_keys, key_values, user_utterance):
    """每次只调模型一次。空键列返回空列表。值不在键集合则整批丢弃并抛错。"""
    from pipeline.block_cross_table.api import _one_model_call

    resolved = [spec for spec in align_keys or [] if spec.get("status") == "resolved"]
    for spec in resolved:
        field_id = spec.get("field_id")
        if field_id not in (key_values or {}):
            raise CrossTableError("cross_table_input_invalid", "missing key_values entry")
        if key_values.get(field_id) == []:
            return []
    if not resolved:
        return []
    payload = {
        "user_utterance": user_utterance,
        "align_keys": [
            {"parent_table_id": spec.get("parent_table_id"), "field_id": spec.get("field_id")}
            for spec in resolved
        ],
        "key_values": key_values,
    }
    prompt = (
        "Return one JSON object with key links. "
        "Each link has left_field_id, right_field_id, left_value, right_value, "
        "proposal_status one of ok, ambiguous, invalid. "
        "If there is no alias, return {\"links\": []}. "
        + json.dumps(payload, ensure_ascii=False)
    )
    raw = _one_model_call(prompt)
    from pipeline.block_cross_table.join_hypothesis import LAST_EXCHANGE

    LAST_EXCHANGE.clear()
    LAST_EXCHANGE["prompt"] = prompt
    LAST_EXCHANGE["raw"] = raw
    rows = _extract_rows(raw, "links")
    if not isinstance(rows, list):
        # 无 links 键或非数组：视为无别名（设计：无别名返回空列表，不失败）
        return []
    allowed = {str(spec.get("field_id")) for spec in resolved}
    values = {str(fid): set(map(str, vals)) for fid, vals in (key_values or {}).items()}
    out: list[RowLink] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise CrossTableError("cross_table_proposal_invalid", "alias row invalid")
        status = row.get("proposal_status")
        left_f = str(row.get("left_field_id") or "")
        right_f = str(row.get("right_field_id") or "")
        left_v = str(row.get("left_value") or "")
        right_v = str(row.get("right_value") or "")
        if status == "invalid":
            out.append(
                RowLink(
                    link_id="a%s" % index,
                    hypothesis_id=str(resolved[0].get("hypothesis_id")),
                    left_field_id=left_f,
                    right_field_id=right_f,
                    left_value=left_v,
                    right_value=right_v,
                    link_kind="alias",
                    decision_status="proposed",
                    proposal_status="invalid",
                )
            )
            continue
        if left_f not in allowed or right_f not in allowed:
            raise CrossTableError("cross_table_proposal_invalid", "alias endpoint is not an align key")
        if left_v not in values.get(left_f, set()) or right_v not in values.get(right_f, set()):
            raise CrossTableError("cross_table_proposal_invalid", "alias value not in key_values")
        out.append(
            RowLink(
                link_id="a%s" % index,
                hypothesis_id=str(resolved[0].get("hypothesis_id")),
                left_field_id=left_f,
                right_field_id=right_f,
                left_value=left_v,
                right_value=right_v,
                link_kind="alias",
                decision_status="proposed",
                proposal_status=status if status in _PROPOSAL_STATUS else "ok",
            )
        )
    return out




def _parent_count(log: dict) -> int:
    return len({str(ref.get("parent_table_id")) for ref in log.get("field_refs") or []})


def write_decision_log(log, session_dir):
    """不同 parent_table_id 少于 2 不得写。目录不可写 → cross_table_log_io。"""
    parents = _parent_count(log)
    if parents == 0 or parents == 1:
        raise CrossTableError("cross_table_input_invalid", "no cross-table candidate")
    path = Path(session_dir)
    filename = log.get("filename") or "cross_table_decision_log.json"
    target = path if path.suffix == ".json" else path / str(filename)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise CrossTableError("cross_table_log_io", str(exc)) from exc
    return {"path": str(target), "fingerprint": str(log.get("fingerprint") or "")}


def replay_decision_log(log, fingerprint):
    if log.get("fingerprint") != fingerprint:
        raise CrossTableError("cross_table_fingerprint_mismatch", "fingerprint mismatch")
    return log


def assert_r2_keys_unchanged(before_keys, after_keys):
    left = sorted(tuple(item) for item in before_keys or [])
    right = sorted(tuple(item) for item in after_keys or [])
    if left != right:
        raise CrossTableError("cross_table_r2_key_mutation_forbidden", "r2 keys changed")
    return {"ok": True}


def derive_exact_links(align_keys, key_values, ops):
    """resolved 键缺条目则抛错。空列表不产生链接。"""
    from pipeline.block_cross_table.api import normalize_match_text

    resolved = [spec for spec in align_keys or [] if spec.get("status") == "resolved"]
    for spec in resolved:
        field_id = spec.get("field_id")
        if field_id not in (key_values or {}):
            raise CrossTableError("cross_table_input_invalid", "missing key_values entry")
    links: list[RowLink] = []
    for i, left in enumerate(resolved):
        for right in resolved[i + 1 :]:
            if str(left.get("parent_table_id")) == str(right.get("parent_table_id")):
                continue
            left_id = str(left.get("field_id"))
            right_id = str(right.get("field_id"))
            left_vals = key_values.get(left_id) or []
            right_vals = key_values.get(right_id) or []
            right_norm = {}
            for value in right_vals:
                right_norm.setdefault(normalize_match_text(str(value), ops), []).append(str(value))
            n = 0
            for value in left_vals:
                hit = right_norm.get(normalize_match_text(str(value), ops)) or []
                for other in hit:
                    links.append(
                        RowLink(
                            link_id="e%s" % n,
                            hypothesis_id=str(left.get("hypothesis_id")),
                            left_field_id=left_id,
                            right_field_id=right_id,
                            left_value=str(value),
                            right_value=other,
                            link_kind="exact",
                            decision_status="confirmed",
                            proposal_status=None,
                        )
                    )
                    n += 1
    return links


def fingerprint_of(field_refs, utterance: str, contract_version: str, ops, needs_retry_history=None) -> str:
    from pipeline.block_cross_table.api import normalize_match_text

    lines = []
    ordered = sorted(
        field_refs or [],
        key=lambda ref: (str(ref.get("parent_table_id")), str(ref.get("field_id")), str(ref.get("block_id"))),
    )
    for ref in ordered:
        labels = [normalize_match_text(str(item), ops) for item in (ref.get("source_labels") or [])]
        lines.append(
            "|".join(
                [
                    str(ref.get("parent_table_id")),
                    str(ref.get("field_id")),
                    str(ref.get("role")),
                    normalize_match_text(str(ref.get("target_name") or ""), ops),
                    normalize_match_text(str(ref.get("source_label") or ""), ops),
                    ",".join(labels),
                ]
            )
        )
    hist = json.dumps(needs_retry_history or [], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    blob = "\n".join(lines) + "\n" + (utterance or "") + "\n" + (contract_version or "") + "\n" + hist
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
