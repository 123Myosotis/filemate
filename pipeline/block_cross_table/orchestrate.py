"""跨表编排：关联假设、用户拍板、机械连接。模型函数不读配置。"""

from __future__ import annotations

import time
from typing import Any, Callable

import config
from pipeline.block_cross_table.api import (
    assert_r2_keys_unchanged,
    bind_chat_json,
    collect_cross_table_field_refs,
    copy_executable_with_bindings,
    derive_exact_links,
    group_exact_name_matches,
    propose_synonym_candidates,
    propose_value_alias_links,
    snapshot_r2_keys,
    validate_ai_proposal,
    write_decision_log,
)
from pipeline.block_cross_table.b3c import fingerprint_of, replay_decision_log
from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.join_hypothesis import (
    LAST_EXCHANGE,
    build_join_questions,
    build_output_questions,
    column_question,
    materialize_join,
    project_output,
    propose_join_hypotheses,
    propose_output_spec,
    rank_hypotheses,
    reset_trace_seq,
    validate_join_hypothesis,
    write_trace,
)
from pipeline.synth_contract.registry import load_contract

MEMBER_SET_RULE = (
    "Each candidate member set is pairs of parent_table_id and field_id. "
    "The set must be disjoint from every exact group. "
    "A non-empty intersection is partial overlap. "
    "Equality with an exact group is also invalid. "
    "One shared pair is enough."
)


def _distinct_parents(refs) -> int:
    return len({str(ref.get("parent_table_id")) for ref in refs or []})


def _limit() -> int:
    return int(config.get_param("cross_table.proposal_max_calls_per_kind"))


def _retry_rounds() -> int:
    return int(config.get_param("cross_table.user_retry_max_rounds"))


def _top_n() -> int:
    return int(config.get_param("cross_table.rank_top_n"))


def _require_confirm(ai_confirm) -> None:
    if not callable(ai_confirm):
        raise CrossTableError("cross_table_input_invalid", "ai_confirm is required")


def _field_cols(tables, executable) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for table_id, table in (tables or {}).items():
        found[str(table_id)] = dict(table.get("field_cols") or {})
    for item in executable or []:
        for source in item.get("sources_resolved") or []:
            if not isinstance(source, dict):
                continue
            block_id = source.get("block_id")
            col = source.get("col")
            field_id = source.get("field_id")
            for table_id, table in (tables or {}).items():
                ids = table.get("block_ids")
                if isinstance(ids, list) and block_id not in ids and str(block_id) not in {str(x) for x in ids}:
                    continue
                bucket = found.setdefault(str(table_id), {})
                if field_id not in (None, ""):
                    bucket[str(field_id)] = col
                else:
                    bucket["col:%s:%s" % (table_id, col)] = col
    return found


def _samples(refs, tables, cols_by_parent) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for ref in refs or []:
        parent = str(ref.get("parent_table_id"))
        field_id = str(ref.get("field_id"))
        table = (tables or {}).get(parent) or {}
        grid = table.get("grid") or []
        col = (cols_by_parent.get(parent) or {}).get(field_id)
        values: list[str] = []
        seen = set()
        for row_i, line in enumerate(grid):
            if row_i == 0 or col is None or not isinstance(line, list) or not isinstance(col, int) or col >= len(line):
                continue
            text = "" if line[col] is None else str(line[col])
            if text == "" or text in seen:
                continue
            seen.add(text)
            values.append(text)
        found[field_id] = values
    return found


def _preview(samples: dict[str, list[str]]) -> dict[str, list[str]]:
    cap = config.get_param("cross_table.sample_preview_cap")
    if not isinstance(cap, int):
        return samples
    return {key: list(values)[:cap] for key, values in samples.items()}


def _label_for(refs, field_id) -> str:
    for ref in refs or []:
        if str(ref.get("field_id")) == str(field_id):
            label = ref.get("source_label") or ref.get("target_name") or field_id
            return str(label)
    return str(field_id)


def _trace(kind: str, body: dict, clock, manifest: list) -> None:
    manifest.append(write_trace(kind, body, clock))


def _call_join(refs, groups, utterance, samples, rejections, counts, clock, manifest):
    calls = 0
    last = []
    while True:
        limit = _limit()
        if calls == limit:
            raise CrossTableError("cross_table_join_hypothesis_invalid", "join", {"last": last})
        last = propose_join_hypotheses(utterance, refs, groups, _preview(samples), rejections)
        counts["join"] = counts.get("join", 0) + 1
        calls += 1
        _trace(
            "propose_join",
            {"seq": calls, "prompt": LAST_EXCHANGE.get("prompt"), "raw": LAST_EXCHANGE.get("raw")},
            clock,
            manifest,
        )
        if "prompt" not in LAST_EXCHANGE:
            raise CrossTableError("trace_io_failed", "propose exchange missing")
        verdict = validate_join_hypothesis(last, refs)
        _trace("validate_join", {"input": last, "result": verdict}, clock, manifest)
        if not verdict["needs_retry"]:
            return last
        if calls == limit:
            raise CrossTableError("cross_table_join_hypothesis_invalid", "join", {"last": last})


def _pick_hypothesis(ranked, questions, refs, ai_confirm, clock, manifest, utterance, groups, samples, counts):
    rejections: list[dict] = []
    rounds = 0
    current_questions = list(questions)
    current_ranked = list(ranked)
    while True:
        if not current_questions:
            current_questions = [column_question(refs)]
        for question in current_questions:
            answer = ai_confirm(question)
            _trace("ai_confirm", {"question": question, "answer": answer}, clock, manifest)
            reply = (answer or {}).get("reply")
            if reply == "confirm" and question.get("hypothesis_id"):
                chosen = next(
                    (item for item in current_ranked if item.get("hypothesis_id") == question.get("hypothesis_id")),
                    None,
                )
                if chosen is None:
                    raise CrossTableError("cross_table_reply_invalid", "hypothesis missing")
                return chosen, rejections
            if reply == "specify":
                left_id = (answer or {}).get("left_field_id")
                right_id = (answer or {}).get("right_field_id")
                join_type = (answer or {}).get("join_type")
                if left_id and right_id and join_type:
                    return (
                        {
                            "hypothesis_id": "user",
                            "left": _ref_endpoint(refs, left_id),
                            "right": _ref_endpoint(refs, right_id),
                            "join_type": join_type,
                            "rationale": (answer or {}).get("reply_text") or "user",
                            "user_selected": True,
                        },
                        rejections,
                    )
            if reply == "rethink":
                rejections.append(
                    {
                        "hypothesis_id": question.get("hypothesis_id"),
                        "reply_text": (answer or {}).get("reply_text") or "",
                    }
                )
                _trace("rethink", {"rejected": rejections}, clock, manifest)
                rounds += 1
                if rounds >= _retry_rounds():
                    current_questions = [column_question(refs)]
                    current_ranked = []
                    continue
                refreshed = _call_join(refs, groups, utterance, samples, rejections, counts, clock, manifest)
                current_ranked = _decorate(rank_hypotheses(refreshed, samples, _top_n()), refs)
                _trace("rank_join", {"ranked": current_ranked}, clock, manifest)
                current_questions = build_join_questions(current_ranked, _top_n()) if current_ranked else [column_question(refs)]
                break
        else:
            raise CrossTableError("cross_table_reply_invalid", "no hypothesis chosen")


def _ref_endpoint(refs, field_id):
    for ref in refs or []:
        if str(ref.get("field_id")) == str(field_id):
            return {
                "parent_table_id": ref.get("parent_table_id"),
                "block_id": ref.get("block_id"),
                "field_id": ref.get("field_id"),
            }
    raise CrossTableError("cross_table_reply_invalid", "field_id not in refs")


def _decorate(ranked, refs):
    out = []
    for item in ranked or []:
        copied = dict(item)
        copied["left_label"] = _label_for(refs, (item.get("left") or {}).get("field_id"))
        copied["right_label"] = _label_for(refs, (item.get("right") or {}).get("field_id"))
        out.append(copied)
    return out


def run_cross_table(
    bundle: dict[str, Any],
    user_utterance: str,
    *,
    ai_confirm: Callable[[dict], dict] | None = None,
    clock: Callable[[], str] | None = None,
    session_dir: str,
    prior_log: dict | None = None,
) -> dict[str, Any]:
    started = time.time()
    if not config.get_param("cross_table.enabled"):
        return {"skipped": True, "ai_calls": {}, "elapsed": time.time() - started}
    if clock is None:
        clock = lambda: "1970-01-01T00:00:00Z"
    contract = load_contract()
    cross = contract.data.get("cross_table") or {}
    ops = list(config.get_param("cross_table.name_normalize_ops") or [])
    disposition = cross.get("unmatched_disposition")
    basis = cross.get("equality_basis")
    executable = list(bundle.get("executable") or [])
    block_plan = bundle.get("block_plan") or {}
    before = snapshot_r2_keys(executable, block_plan)
    refs = collect_cross_table_field_refs(
        executable,
        bundle.get("matrix_draft") or {},
        bundle.get("dimensions_by_block") or {},
        block_plan,
    )
    if _distinct_parents(refs) < 2:
        return {"no_candidate": True, "ai_calls": {}, "elapsed": time.time() - started}
    counts: dict[str, int] = {}
    if prior_log is not None:
        fingerprint = fingerprint_of(refs, user_utterance, contract.version, ops, prior_log.get("needs_retry_history") or [])
        prior_log = dict(prior_log)
        prior_log["current_fingerprint"] = fingerprint
        replay_decision_log(prior_log, fingerprint)
        copied = copy_executable_with_bindings(executable, prior_log.get("join_hypotheses") or [])
        after = snapshot_r2_keys(copied, block_plan)
        assert_r2_keys_unchanged(before, after)
        return {
            "outputs": prior_log.get("outputs") or [],
            "log": prior_log,
            "r2_ok": True,
            "ai_calls": counts,
            "replayed": True,
            "elapsed": time.time() - started,
        }
    _require_confirm(ai_confirm)
    reset_trace_seq()
    manifest: list[str] = []
    tables = bundle.get("tables_by_id") or {}
    cols = _field_cols(tables, executable)
    samples = _samples(refs, tables, cols)
    groups = group_exact_name_matches(refs, basis, ops)
    synonyms = propose_synonym_candidates(refs, groups, user_utterance, MEMBER_SET_RULE)
    counts["synonym"] = counts.get("synonym", 0) + 1
    _trace(
        "propose_synonym",
        {"prompt": LAST_EXCHANGE.get("prompt"), "raw": LAST_EXCHANGE.get("raw"), "parsed": synonyms},
        clock,
        manifest,
    )
    syn_check = validate_ai_proposal([], synonyms, refs, user_utterance)
    if syn_check["needs_retry"]["synonym"]:
        raise CrossTableError("cross_table_proposal_invalid", "synonym")
    hypotheses = _call_join(refs, groups, user_utterance, samples, [], counts, clock, manifest)
    ranked = _decorate(rank_hypotheses(hypotheses, samples, _top_n()), refs)
    _trace("rank_join", {"ranked": ranked}, clock, manifest)
    questions = build_join_questions(ranked, _top_n()) if ranked else [column_question(refs)]
    chosen, rejections = _pick_hypothesis(
        ranked, questions, refs, ai_confirm, clock, manifest, user_utterance, groups, samples, counts
    )
    key_values = {
        str((chosen.get("left") or {}).get("field_id")): samples.get(str((chosen.get("left") or {}).get("field_id"))) or [],
        str((chosen.get("right") or {}).get("field_id")): samples.get(str((chosen.get("right") or {}).get("field_id"))) or [],
    }
    align_keys = [
        {
            "hypothesis_id": chosen.get("hypothesis_id"),
            "parent_table_id": (chosen.get("left") or {}).get("parent_table_id"),
            "field_id": (chosen.get("left") or {}).get("field_id"),
            "status": "resolved",
            "rule": "user_picked",
        },
        {
            "hypothesis_id": chosen.get("hypothesis_id"),
            "parent_table_id": (chosen.get("right") or {}).get("parent_table_id"),
            "field_id": (chosen.get("right") or {}).get("field_id"),
            "status": "resolved",
            "rule": "user_picked",
        },
    ]
    exact_links = derive_exact_links(align_keys, key_values, ops)
    alias_links = propose_value_alias_links(align_keys, key_values, user_utterance)
    counts["alias"] = counts.get("alias", 0) + 1
    _trace(
        "propose_alias",
        {"prompt": LAST_EXCHANGE.get("prompt"), "raw": LAST_EXCHANGE.get("raw")},
        clock,
        manifest,
    )
    expanded = materialize_join(chosen, tables, cols)
    sample_rows = (expanded.get("rows") or [])[: int(config.get_param("cross_table.sample_preview_cap") or 0)]
    specs = propose_output_spec(user_utterance, refs, chosen, sample_rows)
    counts["output_spec"] = counts.get("output_spec", 0) + 1
    _trace(
        "propose_output_spec",
        {"prompt": LAST_EXCHANGE.get("prompt"), "raw": LAST_EXCHANGE.get("raw"), "parsed": specs},
        clock,
        manifest,
    )
    output_questions = build_output_questions(specs)
    chosen_spec = None
    if not output_questions:
        chosen_spec = {
            "output_spec_id": "all",
            "columns": [{"field_id": ref.get("field_id"), "role": "keep"} for ref in refs],
            "row_shape": "wide",
            "output_grain": "per_row",
            "rationale": "all fields",
        }
    else:
        for question in output_questions:
            answer = ai_confirm(question)
            _trace("ai_confirm", {"question": question, "answer": answer}, clock, manifest)
            if (answer or {}).get("reply") == "confirm":
                chosen_spec = next(
                    (spec for spec in specs if spec.get("output_spec_id") == question.get("output_spec_id")),
                    None,
                )
                break
    if chosen_spec is None:
        raise CrossTableError("cross_table_reply_invalid", "output spec missing")
    projected = project_output(chosen_spec, expanded.get("rows") or [])
    projected["unmatched_rows"] = expanded.get("unmatched_rows") or []
    if disposition != "retain_unmatched":
        projected["unmatched_rows"] = [
            {**row, "disposition": disposition} for row in projected["unmatched_rows"]
        ]
    outputs = [projected]
    hypothesis_for_copy = {
        "hypothesis_id": chosen.get("hypothesis_id"),
        "decision_status": "confirmed",
        "left": chosen.get("left"),
        "right": chosen.get("right"),
    }
    copied = copy_executable_with_bindings(executable, [hypothesis_for_copy])
    after = snapshot_r2_keys(copied, block_plan)
    assert_r2_keys_unchanged(before, after)
    fingerprint = fingerprint_of(refs, user_utterance, contract.version, ops, [])
    log = {
        "fingerprint": fingerprint,
        "contract_version": contract.version,
        "field_refs": refs,
        "exact_groups": groups,
        "synonym_candidates": synonyms,
        "join_hypotheses": hypotheses,
        "ranked_hypotheses": ranked,
        "chosen_hypothesis": chosen,
        "output_specs": specs,
        "chosen_output_spec": chosen_spec,
        "questions": questions,
        "replies": rejections,
        "row_links": exact_links + alias_links,
        "expanded": {"row_count": len(expanded.get("rows") or [])},
        "outputs": outputs,
        "trace_manifest": manifest,
        "filename": config.get_param("cross_table.decision_log_filename"),
    }
    written = write_decision_log(log, session_dir)
    return {
        "outputs": outputs,
        "log": log,
        "log_path": written["path"],
        "r2_ok": True,
        "ai_calls": counts,
        "elapsed": time.time() - started,
        "executable": copied,
    }


__all__ = ["run_cross_table", "bind_chat_json"]
