# -*- coding: utf-8 -*-
"""B6 QP: live AI plan generation (uses config api.model; does not alter LAYER_RECOGNIZE prompts)."""
from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Mapping

from ai.client import chat_json

from .errors import QueryEngineError
from .ie_query import route_e_entry, validate_needs_user_plan
from .plan_structural_markers import is_placeholder_utterance_span
from .qp import ROLLBACK_QUERY_PLANNER_START, assemble_qp_input, validate_query_plan_ast
from .result_projection import ResultProjectionError

# Column-ref keys that may carry a spurious table_alias. prefix from the model.
_COL_REF_ARG_KEYS = (
    "column_id",
    "left_key",
    "right_key",
    "group_column_id",
    "sort_column_id",
    "stamp_column_id",
    "out_column_id",
    "source_column_id",
)

QP_SYSTEM = """You are a query planner. Output ONE JSON object only (no markdown). Required keys: status (plan_ready|needs_user), query_time_basis, rollback_anchors (array that MUST include the exact string query_planner_start), demand_points (non-empty), condition_trees, result_projection, steps, edges. Each step needs: step_id, op, args, inputs (src:<table_id> or prior output id), output, provenance object with utterance_span string. Use only provided table_ids and column names from the tables payload. Column references (result_projection.columns[].column_id, step args keys, and condition atom column_id) MUST be bare column names with NO table_id or alias prefix (never write forms like t_a.col or table.col). No domain business rules and no hardcoded column-name shortcuts. If a step args.condition_tree_id is set, that id MUST exist as a key in top-level condition_trees; otherwise the step MUST include a non-empty inline args.conditions array. If you cannot form a valid tree or inline conditions: status=needs_user, fail_locus=predicate, and fill the five needs_user fields. result_projection must be an object with columns array of objects; each columns[].column_id MUST be either a name from tables.column_details or a prior step's output id or an op_derive out_column_id. You MAY choose any semantically appropriate column name that exists in the loaded tables (including near-synonyms across tables); local code must not rename your choices. If you project a column that lives on table T, your steps MUST already bring T into the dataflow via inputs (src:T) or op_join — otherwise the column will be null at execution. Do not use generic placeholder names (e.g. out_col / result_col) unless status=needs_user AND fail_locus=projection AND the five needs_user fields are all non-empty. If you cannot determine projection columns: status=needs_user, fail_locus=projection, and fill the five fields. For each result_projection column provenance: utterance_span MUST be a real substring of the user utterance (not a generic placeholder token), OR provide a non-empty portrait_path and/or memory_ref. If projection provenance cannot be grounded: status=needs_user, fail_locus=projection, and fill the five fields. When the user prompt JSON contains recalled_memories: if a recalled term matches the user query, you MUST expand filter predicates strictly from that item's description field (and values if present). Parallel items listed in description (comma / enumeration marks) expand as OR atoms. Negation phrasing in description (exclude / not belonging) expands as NOT. When mapping jargon/terms onto columns, you MUST combine each column's name with that column's samples for semantic reasoning. If the jargon describes a category, attribute, or status, prefer columns with smaller value domains whose samples align with the jargon; if it describes a concrete entity identity, prefer columns with higher uniqueness. In provenance, state why that column was chosen (e.g. based on samples containing ...). Record memory_ref in provenance when a step or condition uses a recalled memory. For FILTER term expansion: do not invent synonym lists outside recalled_memories. EXCEPTION for JOIN identity matching (HARD — see below): you MUST use samples and layout/merge_anchors to invent contains / matrix-unfold join logic yourself. If you cannot determine whether a FILTER term applies or how to expand/map it, or if information conflicts: status=needs_user. === HARD — NEVER DUMP JOIN WORK ON THE USER === Messy headers, merged cells, matrix sheets, and short-vs-long entity names (e.g. short site nickname vs full station title) are YOUR parsing job. Use tables.samples + layout + merge_anchors to build the association yourself. Typical self-solve patterns (operators only; values from THIS turn's samples): (a) Short name embeds in long name → op_join args.mode=contains (left_contains_right or right_contains_left — pick direction from samples). (b) Owner/contact sheet is a matrix / multi-level header / merged cell grid → prefer a long-form owner table already in the payload if present; else unfold via layout/merge_anchors into entity↔person rows, then join. (c) Equality keys do not align → still try contains before asking. FORBIDDEN in needs_user five fields: asking the user which column maps to which column, which join key to use, or how to bind tables. Never make the user teach you data association. If you are still unsure after forming a hypothesis from samples: status=needs_user ONLY to confirm that business hypothesis — not to offload technical binding. === HARD — needs_user MUST BE 100% PLAIN CHINESE === When status=needs_user you MUST set fail_locus to EXACTLY one of: term_expand, predicate, time_window, interval_build, join_key, derive, existence, group_latest, projection, portrait_gate, budget, other (enumeration token only; never a natural-language sentence). You MUST also fill non-empty strings for: speculation, evidence, what_might_be_wrong, next_step, please_confirm_or_correct. Those five fields MUST be 100% Simplified Chinese plain language. FORBIDDEN inside the five fields: any English word or technical jargon (examples of banned tokens: join, key, matrix, contains, column_id, left_on, right_on, op_join, table_id, mode, AST, JSON). Table file names from the payload may stay as proper nouns. Convert 'how to join tables' into a business confirmation like '这两边名字写法不一样，我推测其实是同一个站点，行吗？'. Required five-field shape when confirming ANY self-made hypothesis (association OR filter-term mapping): speculation: 我推测「短名/术语」对应「样本里的具体写法」，准备按包含关系/该样本集合去筛或去找。 evidence: 依据样本里能互相对上的写法 / 表头结构 / 合并单元格线索（用中文说）。 what_might_be_wrong: 有些名字可能对不上，会漏找或错配。 next_step: 按这个逻辑直接生成计划并跑出表格。 please_confirm_or_correct: 是否同意按此逻辑执行？请确认或纠正。 FORBIDDEN in five fields: asking the user to list mappings, to name which sample values correspond to a jargon term, or to teach column binding. If samples only partially cover a jargon list, propose the best sample-based OR set yourself and ask only whether that set is acceptable. Natural-language explanation belongs only in those five fields, not in fail_locus. === FEW-SHOT (abstract shape only; NEVER copy placeholders as real column/values) === Example demand shape (replace angle-bracket tokens from THIS turn only): 「我需要 <实体类别>（指 <值1> 和 <值2> 和 <值3>，也可能 <别名>，还有 <值4>）的 <事件记录>,其中包含在 <时间窗> 有记录的,给他标一列 <标记词>,还有当前 <状态字段>,只要 <成功条件> 并且是 <方法A> 或者 <方法B> 或者这两个任意搭配,都给他在对应列标出 <完成标记>,并且我还要知道对应 <责任人> 是谁,这个对象归 <组织单位>,并且在特定条件下比如 <花名册表> 在 <时间窗>,我还要根据 <时间戳字段> 知道当时是谁在岗,我的需求是能够处理这种复杂程度的数据并且输出对应表格。」 How to encode ANY such demand (operators only; values from memories/samples of THIS turn): 1) Term expansion → condition_trees with AND/OR/NOT atoms (values from recalled_memories description/values + column samples; never invent synonyms). Use args.condition_tree_id on op_filter / inside op_derive. 2) Compound judgment → op_derive expr.kind=case|flag_if with nested when trees (e.g. success AND (method_A OR method_B OR both)). 3) Calendar-window flag → op_derive or op_stamp_window over an interval. 4) HARD — owner / org / responsible-person attribution: If the user asks who owns / is responsible for / which org an entity belongs to, you MUST emit op_join against a real owner/roster table using real column keys from tables.column_details (or portraits). If equality keys do not align but one side's key string embeds the other (inspect samples), use op_join args.mode=contains (left_contains_right). FORBIDDEN: op_derive kind=literal|constant with value "" (or any blank) as a stand-in for that person/org column. If after sample inspection you still cannot form even a contains/matrix hypothesis: status=needs_user, fail_locus=join_key, and the five Chinese fields must state your best business guess for confirmation — NEVER ask which column maps to which. 5) Timestamp → on-duty person → op_interval_build then op_stamp_window (stamp_column_id on the fact table; interval start/end columns MUST exist on the roster/interval table — never reuse fact-table time column names on the wrong input). HARD — wide calendar rosters: args.mode=wide_date_headers and date_row_index MUST select the row whose cells are Excel serials / real datetimes (inspect samples), NEVER a title row and NEVER a 1..N day-index row. Set value_column_id to the emitted on-duty field name; stamp_window attach_columns MUST use that same field name. Never collapse to a single-table scan. Invalid AST → repair on retry. If the user substitutes different jargon/method tokens, rebuild atoms from THIS turn's memories/samples — do NOT reuse few-shot placeholders as literals."""


def _network_reconnect_budget() -> int:
    """How many extra same-prompt retries after transport/wall-clock failure (env)."""
    raw = os.environ.get("QP_NETWORK_RECONNECT")
    if raw is None or str(raw).strip() == "":
        return 1
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return 1


def _is_transport_wall_failure(exc: BaseException | None, *, last_error: str = "") -> bool:
    """
    True only for network / wall-clock / transport failures — never for plan AST
    or business validation errors.
    """
    if exc is not None:
        try:
            from ai.retry import ERR_RETRY_TIMEOUT, ChatRetryError
            from requests.exceptions import ConnectionError as ReqConn
            from requests.exceptions import RequestException
            from requests.exceptions import Timeout as ReqTimeout

            if isinstance(exc, ChatRetryError) and str(exc.code) == ERR_RETRY_TIMEOUT:
                return True
            if isinstance(exc, (ReqTimeout, ReqConn, TimeoutError, ConnectionError, OSError)):
                return True
            if isinstance(exc, RequestException):
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (408, 500, 502, 503, 504) or status is None:
                    return True
        except Exception:
            pass
        name = type(exc).__name__.lower()
        msg = str(exc).lower()
        if any(k in name for k in ("timeout", "connection", "ssl", "chunked")):
            return True
        if any(
            k in msg
            for k in (
                "timed out",
                "timeout",
                "connection",
                "max_retry_seconds",
                "wall time",
                "broken pipe",
                "reset by peer",
            )
        ):
            return True
    err = (last_error or "").lower()
    if not err:
        return False
    return any(
        k in err
        for k in (
            "timeout",
            "timed out",
            "connection",
            "max_retry_seconds",
            "wall time",
            "broken pipe",
            "reset by peer",
            "analysis_retry_timeout",
        )
    )


def strip_table_alias_prefix(
    name: str,
    *,
    table_ids: list[str] | None = None,
    known_columns: set[str] | None = None,
) -> str:
    """
    Mechanical format fix: strip leading table_id./alias. from a column ref.
    Does not invent names; leaves value unchanged when not a clear alias prefix.
    """
    s = str(name or "").strip()
    if "." not in s:
        return s
    left, right = s.split(".", 1)
    left, right = left.strip(), right.strip()
    if not left or not right:
        return s
    tid_set = {str(x) for x in (table_ids or [])}
    if left in tid_set:
        return right
    if known_columns is not None:
        if s not in known_columns and right in known_columns:
            return right
    # Alias-like prefixes (t_a / t1) commonly emitted after multi-table joins.
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", left) and (
        left.startswith("t_") or re.fullmatch(r"t\d+", left) or left in tid_set
    ):
        return right
    return s


def _walk_strip_column_refs(
    node: Any,
    *,
    table_ids: list[str],
    known_columns: set[str],
) -> Any:
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in _COL_REF_ARG_KEYS and isinstance(v, str):
                out[k] = strip_table_alias_prefix(
                    v, table_ids=table_ids, known_columns=known_columns
                )
            elif k == "atom" and isinstance(v, dict):
                atom = dict(v)
                if isinstance(atom.get("column_id"), str):
                    atom["column_id"] = strip_table_alias_prefix(
                        atom["column_id"],
                        table_ids=table_ids,
                        known_columns=known_columns,
                    )
                out[k] = _walk_strip_column_refs(
                    atom, table_ids=table_ids, known_columns=known_columns
                )
            else:
                out[k] = _walk_strip_column_refs(
                    v, table_ids=table_ids, known_columns=known_columns
                )
        return out
    if isinstance(node, list):
        return [
            _walk_strip_column_refs(x, table_ids=table_ids, known_columns=known_columns)
            for x in node
        ]
    return node


def strip_table_alias_prefixes_in_plan(
    plan: dict[str, Any],
    *,
    table_ids: list[str] | None = None,
    tables_by_id: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Strip table_alias. prefixes from projection / step args / condition trees."""
    tids = list(table_ids or [])
    known: set[str] = set()
    if tables_by_id:
        for tid, t in tables_by_id.items():
            tids.append(str(tid))
            for c in t.get("columns") or []:
                known.add(str(c))
    tids = list(dict.fromkeys(tids))
    out = dict(plan)
    rp = out.get("result_projection")
    if isinstance(rp, dict):
        rp = dict(rp)
        cols = rp.get("columns")
        if isinstance(cols, list):
            new_cols = []
            for col in cols:
                if isinstance(col, str):
                    new_cols.append(
                        strip_table_alias_prefix(
                            col, table_ids=tids, known_columns=known
                        )
                    )
                elif isinstance(col, dict):
                    c2 = dict(col)
                    cid = c2.get("column_id")
                    if isinstance(cid, str) and cid.strip():
                        c2["column_id"] = strip_table_alias_prefix(
                            cid, table_ids=tids, known_columns=known
                        )
                    new_cols.append(c2)
                else:
                    new_cols.append(col)
            rp["columns"] = new_cols
        out["result_projection"] = rp
    steps = out.get("steps")
    if isinstance(steps, list):
        out["steps"] = _walk_strip_column_refs(
            steps, table_ids=tids, known_columns=known
        )
    trees = out.get("condition_trees")
    if isinstance(trees, dict):
        out["condition_trees"] = _walk_strip_column_refs(
            trees, table_ids=tids, known_columns=known
        )
    return out


def _normalize_plan(
    plan: dict[str, Any],
    *,
    table_ids: list[str] | None = None,
) -> dict[str, Any]:
    """
    Mechanical AST scaffolding only (not semantic rewrite).
    Must NEVER invent five-part needs_user prose or rewrite fail_locus.
    """
    out = dict(plan)
    tids = list(table_ids or [])
    default_src = "src:%s" % tids[0] if tids else None
    if not out.get("status"):
        out["status"] = "plan_ready"
    if not out.get("query_time_basis"):
        out["query_time_basis"] = "data_max_time"
    anchors = out.get("rollback_anchors")
    if not isinstance(anchors, list):
        anchors = []
    if ROLLBACK_QUERY_PLANNER_START not in anchors:
        anchors = list(anchors) + [ROLLBACK_QUERY_PLANNER_START]
    out["rollback_anchors"] = anchors
    if not isinstance(out.get("edges"), list):
        out["edges"] = []
    if not isinstance(out.get("condition_trees"), dict):
        out["condition_trees"] = {}
    if not isinstance(out.get("demand_points"), list):
        out["demand_points"] = []
    _OP_ALIASES = {
        "filter": "op_filter",
        "join": "op_join",
        "derive": "op_derive",
        "stamp_window": "op_stamp_window",
        "interval_build": "op_interval_build",
        "set_ops": "op_set_ops",
        "aggregate": "op_aggregate",
        "exists_any": "op_exists_any",
        "group_top_k": "op_group_top_k",
        "arg_extremum": "op_arg_extremum",
        "sort_truncate": "op_sort_truncate",
    }
    steps = out.get("steps")
    prev_out = None
    if isinstance(steps, list):
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            if not step.get("provenance"):
                step["provenance"] = {"utterance_span": "span_step_%s" % i}
            if not step.get("step_id"):
                step["step_id"] = "s_%s" % i
            if not step.get("output"):
                step["output"] = step["step_id"]
            raw_op = str(step.get("op") or "").strip()
            if raw_op in _OP_ALIASES:
                step["op"] = _OP_ALIASES[raw_op]
            elif raw_op and not raw_op.startswith("op_") and ("op_" + raw_op) in {
                "op_filter",
                "op_join",
                "op_derive",
                "op_stamp_window",
                "op_interval_build",
                "op_set_ops",
                "op_aggregate",
                "op_exists_any",
                "op_group_top_k",
                "op_arg_extremum",
                "op_sort_truncate",
            }:
                step["op"] = "op_" + raw_op
            if not isinstance(step.get("inputs"), list):
                step["inputs"] = []
            if not step["inputs"]:
                op = str(step.get("op") or "")
                if op == "op_interval_build":
                    step["inputs"] = []
                elif op == "op_join" and len(tids) >= 2:
                    step["inputs"] = ["src:%s" % tids[0], "src:%s" % tids[1]]
                elif prev_out:
                    step["inputs"] = [prev_out]
                elif default_src:
                    step["inputs"] = [default_src]
            if not isinstance(step.get("args"), dict):
                step["args"] = {}
            prev_out = str(step.get("output") or step.get("step_id"))
    # Ensure demand_points cover steps if empty
    if not out["demand_points"] and isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            out["demand_points"].append(
                {
                    "demand_id": "d_%s" % step.get("step_id"),
                    "kind": "filter",
                    "utterance_span": (step.get("provenance") or {}).get("utterance_span")
                    or "span",
                    "bound_step_id": step.get("step_id"),
                    "bound_column_or_filter": "col",
                }
            )
    # result_projection: shape only — never invent placeholder column_id names.
    last_sid = "s_0"
    if isinstance(steps, list) and steps:
        last = steps[-1] if isinstance(steps[-1], dict) else {}
        last_sid = str(last.get("step_id") or "s_0")

    def _proj_col(raw_col: Any, _idx: int) -> dict[str, Any] | None:
        if isinstance(raw_col, str) and raw_col.strip():
            name = strip_table_alias_prefix(raw_col.strip(), table_ids=tids)
            return {
                "column_id": name,
                "display_name": name,
                "column_name_source": "from_ai_runtime",
                "source_step_id": last_sid,
                "derive_rule_ref": None,
                "provenance": {"utterance_span": "span_proj"},
            }
        if not isinstance(raw_col, dict):
            # Non-str non-dict: keep an empty shell without inventing a name.
            return {
                "column_name_source": "from_ai_runtime",
                "source_step_id": last_sid,
                "derive_rule_ref": None,
                "provenance": {"utterance_span": "span_proj"},
            }
        col = dict(raw_col)
        cid = strip_table_alias_prefix(
            str(
                col.get("column_id") or col.get("name") or col.get("column") or ""
            ).strip(),
            table_ids=tids,
        )
        if cid:
            col["column_id"] = cid
            col.setdefault("display_name", str(col.get("display_name") or cid))
        else:
            # Missing id: leave absent/empty — do not invent out_col_*.
            if "column_id" in col and not str(col.get("column_id") or "").strip():
                col["column_id"] = ""
            col.setdefault("display_name", str(col.get("display_name") or ""))
        col.setdefault("column_name_source", "from_ai_runtime")
        col.setdefault("source_step_id", last_sid)
        if "derive_rule_ref" not in col:
            col["derive_rule_ref"] = None
        prov = col.get("provenance")
        if not isinstance(prov, dict):
            col["provenance"] = {"utterance_span": "span_proj"}
        elif not prov.get("utterance_span") and not prov.get("portrait_path"):
            prov = dict(prov)
            prov["utterance_span"] = "span_proj"
            col["provenance"] = prov
        return col

    rp_raw = out.get("result_projection")
    if isinstance(rp_raw, list):
        # Bare array of non-empty strings → lift to {columns:[...]}; else empty.
        if rp_raw and all(isinstance(x, str) and str(x).strip() for x in rp_raw):
            rp: dict[str, Any] = {"columns": list(rp_raw)}
        else:
            rp = {"columns": []}
    elif isinstance(rp_raw, dict):
        rp = dict(rp_raw)
    else:
        rp = {"columns": []}
    raw_cols = rp.get("columns")
    if not isinstance(raw_cols, list):
        raw_cols = []
    coerced = []
    for i, c in enumerate(raw_cols):
        built = _proj_col(c, i)
        if built is not None:
            coerced.append(built)
    out["result_projection"] = {**rp, "columns": coerced}
    # Mechanical alias-prefix strip (V20); shape-only, no semantic remap.
    return strip_table_alias_prefixes_in_plan(out, table_ids=tids)


def _projection_allowed_ids(
    plan: Mapping[str, Any],
    tables_by_id: Mapping[str, dict[str, Any]],
) -> set[str]:
    """All loaded table columns + derive/attach/step outputs (no lineage whitelist)."""
    allowed: set[str] = set()
    for t in tables_by_id.values():
        for c in t.get("columns") or []:
            allowed.add(str(c))
    steps = plan.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            out_id = step.get("output")
            if out_id:
                allowed.add(str(out_id))
            args = step.get("args") if isinstance(step.get("args"), dict) else {}
            out_col = args.get("out_column_id")
            if out_col is not None and str(out_col).strip():
                allowed.add(str(out_col).strip())
            for key in ("expressions", "exprs", "derives"):
                batch = args.get(key)
                if isinstance(batch, list):
                    for item in batch:
                        if isinstance(item, dict) and item.get("out_column_id"):
                            allowed.add(str(item.get("out_column_id")).strip())
            attach = args.get("attach_columns")
            if isinstance(attach, list):
                for c in attach:
                    if c is not None and str(c).strip():
                        allowed.add(str(c).strip())
            keep = args.get("keep_columns") or args.get("attach_columns")
            if isinstance(keep, list):
                for c in keep:
                    if c is not None and str(c).strip():
                        allowed.add(str(c).strip())
            vk = args.get("value_column_id")
            if vk is not None and str(vk).strip():
                allowed.add(str(vk).strip())
    return allowed


def repair_projection_drop_unknown(
    plan: dict[str, Any],
    tables_by_id: Mapping[str, dict[str, Any]],
) -> list[str]:
    """
    Drop projection columns whose column_id is not grounded in any loaded table
    or step-produced id. Does NOT remap names or restrict by step lineage.
    """
    rp = plan.get("result_projection")
    if not isinstance(rp, dict):
        return []
    cols = rp.get("columns")
    if not isinstance(cols, list) or not cols:
        return []
    allowed = _projection_allowed_ids(plan, tables_by_id)
    kept: list[Any] = []
    dropped: list[str] = []
    for col in cols:
        if not isinstance(col, dict):
            continue
        cid = str(col.get("column_id") or "").strip()
        if cid and cid in allowed:
            kept.append(col)
        elif cid:
            dropped.append(cid)
    if kept:
        rp["columns"] = kept
    return dropped


def check_plan_ready_projection_columns(
    plan: Mapping[str, Any],
    tables_by_id: Mapping[str, dict[str, Any]],
    *,
    allow_drop_unknown: bool = True,
) -> None:
    """
    plan_ready projection gate: non-empty columns, each column_id present and
    ∈ table columns ∪ step.output ∪ derive out_column_id ∪ attach_columns.
    Raises ResultProjectionError (fail_locus=projection).
    """
    if plan.get("status") == "needs_user":
        return
    rp = plan.get("result_projection")
    if not isinstance(rp, dict):
        raise ResultProjectionError(
            "result_projection missing or not object",
            fail_locus="projection",
        )
    cols = rp.get("columns")
    if not isinstance(cols, list) or not cols:
        raise ResultProjectionError(
            "result_projection.columns must be non-empty list",
            fail_locus="projection",
        )
    if allow_drop_unknown and isinstance(plan, dict):
        repair_projection_drop_unknown(plan, tables_by_id)
        cols = (plan.get("result_projection") or {}).get("columns") or cols
    allowed = _projection_allowed_ids(plan, tables_by_id)
    for i, col in enumerate(cols):
        if not isinstance(col, dict):
            raise ResultProjectionError(
                "column[%s] must be object" % i,
                fail_locus="projection",
            )
        cid = str(col.get("column_id") or "").strip()
        if not cid:
            raise ResultProjectionError(
                "column[%s] missing column_id" % i,
                fail_locus="projection",
            )
        if cid not in allowed:
            raise ResultProjectionError(
                "column_id not in table columns or step outputs: %s" % cid,
                fail_locus="projection",
            )


def check_plan_ready_no_blank_attr_literals(plan: Mapping[str, Any]) -> None:
    """
    Structural anti-laziness gate (no business lexicon):
    plan_ready must not invent blank literal/constant derive columns as stand-ins
    for multi-table attribution when no op_join is present.
    """
    if plan.get("status") == "needs_user":
        return
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return
    has_join = any(
        isinstance(s, dict)
        and str(s.get("op") or "") in ("op_join", "join")
        for s in steps
    )
    if has_join:
        return
    blank_outs: list[str] = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        if str(s.get("op") or "") not in ("op_derive", "derive"):
            continue
        args = s.get("args") if isinstance(s.get("args"), dict) else {}
        for expr in args.get("expressions") or []:
            if not isinstance(expr, dict):
                continue
            kind = str(expr.get("kind") or "").lower()
            if kind not in ("literal", "constant", "const"):
                continue
            val = expr.get("value")
            if val is None or (isinstance(val, str) and str(val).strip() == ""):
                out_id = str(expr.get("out_column_id") or expr.get("column_id") or "")
                if out_id:
                    blank_outs.append(out_id)
    if blank_outs:
        raise QueryEngineError(
            "blank literal/constant derive without op_join is forbidden for "
            "attribution columns %s; emit op_join or status=needs_user "
            "fail_locus=join_key" % blank_outs,
            fail_locus="join_key",
        )


def check_plan_ready_condition_trees(plan: Mapping[str, Any]) -> None:
    """
    plan_ready: each args.condition_tree_id must exist in condition_trees,
    or the step must have non-empty inline args.conditions.
    Raises QueryEngineError(fail_locus=predicate).
    """
    if plan.get("status") == "needs_user":
        return
    trees = plan.get("condition_trees")
    tree_ids: set[str] = set()
    if isinstance(trees, dict):
        tree_ids = {str(k) for k in trees.keys()}
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return
    for step in steps:
        if not isinstance(step, dict):
            continue
        args = step.get("args")
        if not isinstance(args, dict):
            continue
        ctid = args.get("condition_tree_id")
        if ctid is None or str(ctid).strip() == "":
            continue
        ctid_s = str(ctid).strip()
        inline = args.get("conditions")
        has_inline = isinstance(inline, list) and len(inline) > 0
        if ctid_s not in tree_ids and not has_inline:
            raise QueryEngineError(
                "condition_tree_id missing and no inline conditions: %s" % ctid_s,
                fail_locus="predicate",
                subreason="missing_condition_tree",
            )


def _join_has_equi_keys(args: Mapping[str, Any]) -> bool:
    lk = str(
        args.get("left_key") or args.get("left_on") or args.get("on") or ""
    ).strip()
    rk = str(
        args.get("right_key") or args.get("right_on") or args.get("on") or ""
    ).strip()
    if args.get("key"):
        return True
    return bool(lk and rk)


def _join_mode_skips_keys(args: Mapping[str, Any]) -> bool:
    mode = str(args.get("mode") or args.get("set_op") or "inner").lower()
    return mode in ("union", "union_all", "concat", "or")


def check_plan_ready_join_keys(plan: Mapping[str, Any]) -> None:
    """
    plan_ready: each op_join (non-union) must have left_key/right_key (or key/on).

    QE `_exec_join` 只认等值键，不认 condition_tree 当 join 键；故 QP 必须拦下
    「condition_tree_id=null + 无 left/right_key」的计划，带 fail_locus=join_key 重试。
    Raises QueryEngineError(fail_locus=join_key).
    """
    if plan.get("status") == "needs_user":
        return
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return
    for step in steps:
        if not isinstance(step, dict):
            continue
        op = str(step.get("op") or "")
        if op not in ("op_join", "join"):
            continue
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        if _join_mode_skips_keys(args):
            continue
        if _join_has_equi_keys(args):
            continue
        raise QueryEngineError(
            "op_join missing left_key/right_key (or key); "
            "condition_tree alone is not enough for equi-join; step=%s"
            % (step.get("step_id") or step.get("output") or "?"),
            fail_locus="join_key",
            subreason="join_keys_missing",
        )


def annotate_projection_provenance_weak(plan: dict[str, Any]) -> bool:
    """
    Structural-only: mark projection column provenance.weak when ungrounded.
    Does not reject. Returns case-level provenance_weak.
    """
    rp = plan.get("result_projection")
    if not isinstance(rp, dict):
        return False
    cols = rp.get("columns")
    if not isinstance(cols, list):
        return False
    any_weak = False
    for col in cols:
        if not isinstance(col, dict):
            any_weak = True
            continue
        prov = col.get("provenance")
        if not isinstance(prov, dict):
            col["provenance"] = {"utterance_span": "", "weak": True}
            any_weak = True
            continue
        prov = dict(prov)
        has_portrait = bool(str(prov.get("portrait_path") or "").strip())
        has_memory = bool(str(prov.get("memory_ref") or "").strip())
        span_weak = is_placeholder_utterance_span(prov.get("utterance_span"))
        weak = (not has_portrait and not has_memory and span_weak)
        # Ignore AI self-reported weak; recompute structurally.
        prov["weak"] = bool(weak)
        col["provenance"] = prov
        if weak:
            any_weak = True
    return any_weak


def _qp_event_trace(
    run_id: str,
    kind: str,
    summary: str,
    *,
    seq: int,
) -> str | None:
    """Write short reject/retry/needs_user event under chat_json/<run_id>/."""
    try:
        from datetime import datetime, timezone
        from pathlib import Path

        base = (
            Path(__file__).resolve().parents[3]
            / "diagnostics"
            / "trace"
            / "chat_json"
            / str(run_id)
        )
        base.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = base / ("%s_%s_%s.txt" % (kind, stamp, seq))
        body = "ts=%s\nkind=%s\n%s\n" % (stamp, kind, summary[:3500])
        path.write_text(body[:4096], encoding="utf-8", errors="replace")
        return str(path)
    except Exception:
        return None


def _repair_column_args(
    plan: dict[str, Any],
    tables_by_id: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    """If step args reference unknown columns, map to first column of input table (mechanical)."""
    colsets: dict[str, list[str]] = {}
    for tid, t in tables_by_id.items():
        colsets["src:%s" % tid] = list(t.get("columns") or [])
    handle_cols: dict[str, list[str]] = dict(colsets)
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return plan
    for step in steps:
        if not isinstance(step, dict):
            continue
        inputs = list(step.get("inputs") or [])
        base_cols: list[str] = []
        for ref in inputs:
            if ref in handle_cols:
                base_cols = handle_cols[ref]
                break
        if not base_cols and colsets:
            base_cols = next(iter(colsets.values()))
        args = step.get("args")
        if not isinstance(args, dict) or not base_cols:
            out_id = str(step.get("output") or step.get("step_id") or "")
            if out_id:
                handle_cols[out_id] = list(base_cols)
            continue
        for key in (
            "column_id",
            "left_key",
            "right_key",
            "group_column_id",
            "sort_column_id",
            "stamp_column_id",
            "out_column_id",
        ):
            if key not in args:
                continue
            val = str(args.get(key) or "")
            if val and val not in base_cols and key != "out_column_id":
                # join right_key: use second input cols if available
                if key == "right_key" and len(inputs) >= 2 and inputs[1] in handle_cols:
                    rcols = handle_cols[inputs[1]]
                    args[key] = rcols[0] if rcols else base_cols[0]
                else:
                    args[key] = base_cols[0]
        out_id = str(step.get("output") or step.get("step_id") or "")
        if out_id:
            handle_cols[out_id] = list(base_cols)
            if args.get("out_column_id"):
                handle_cols[out_id] = list(base_cols) + [str(args["out_column_id"])]
    return plan


def _resolve_recalled_memories(
    *,
    utterance: str,
    recalled_memories: list[dict[str, Any]] | None,
    memory_conn: Any | None,
) -> list[dict[str, Any]]:
    """Prefer explicit list; else hybrid_recall via memory_conn. No local expand."""
    if recalled_memories is not None:
        return [dict(x) for x in recalled_memories]
    if memory_conn is None:
        return []
    from pipeline.block_cross_table.memory_rag.qp_memory_inject import (
        recall_memories_for_qp,
    )

    pack = recall_memories_for_qp(memory_conn, utterance=utterance)
    return list(pack.get("recalled_memories") or [])


def _column_samples_for_table(
    table: Mapping[str, Any],
    *,
    columns: list[str],
    per_col: int = 3,
) -> list[dict[str, Any]]:
    """
    Build [{name, samples}] for QP. Samples = first N non-empty cell values.
    No business-column special casing.
    """
    rows = list(table.get("rows") or [])
    n = max(0, int(per_col))
    out: list[dict[str, Any]] = []
    for col in columns:
        samples: list[Any] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            val = row.get(col)
            if val is None:
                continue
            text = str(val).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            samples.append(val if not isinstance(val, str) else text)
            if len(samples) >= n:
                break
        out.append({"name": col, "samples": samples})
    return out


_LAYOUT_ATTACH_KEYS = (
    "layout",
    "merge_anchors",
    "synthetic_column_count",
    "header_rows_candidate",
    "truncated",
    "original_rows",
    "loaded_rows",
    "truncation_fact",
    "slice_constraint",
)


def apply_portrait_attachments_to_tables(
    table_summaries: list[dict[str, Any]],
    portraits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Copy fill/comments/layout facts from portraits onto table rows (mechanical)."""
    by_id = {
        str(p.get("table_id")): p
        for p in portraits
        if isinstance(p, Mapping) and p.get("table_id") is not None
    }
    out: list[dict[str, Any]] = []
    for ts in table_summaries:
        row = dict(ts)
        p = by_id.get(str(row.get("table_id")))
        if p is not None and "fill_summary" in p and p.get("fill_summary") is not None:
            row["fill_summary"] = p["fill_summary"]
        if p is not None and "comments" in p and p.get("comments") is not None:
            row["comments"] = [
                {
                    "row": c.get("row"),
                    "col": c.get("col"),
                    "text_len": c.get("len", len(str(c.get("text") or ""))),
                    "text_head": str(c.get("text") or "")[:60],
                }
                for c in list(p.get("comments") or [])
            ]
        if p is not None:
            for k in _LAYOUT_ATTACH_KEYS:
                if k in p and p.get(k) is not None:
                    row[k] = p[k]
        out.append(row)
    return out


def qp_call_ai(
    *,
    utterance: str,
    portraits: list[dict[str, Any]],
    catalog: dict[str, Any],
    tables_by_id: Mapping[str, dict[str, Any]],
    api_key: str,
    confirmed_bindings: list[dict[str, Any]] | None = None,
    ask_history: list[dict[str, Any]] | None = None,
    recalled_memories: list[dict[str, Any]] | None = None,
    memory_conn: Any | None = None,
    require_full_capability_cover: bool = False,
    apply_column_repair: bool = True,
    max_retries: int = 1,
    max_tokens: int | None = None,
    timeout: int | None = None,
    slice_constraints: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    memories = _resolve_recalled_memories(
        utterance=utterance,
        recalled_memories=recalled_memories,
        memory_conn=memory_conn,
    )
    from pipeline.block_cross_table.memory_rag.qp_slot import (
        assemble_qp_input_with_memory_candidates,
    )

    envelope = assemble_qp_input_with_memory_candidates(
        utterance=utterance,
        portraits=portraits,
        catalog=catalog,
        ask_history=ask_history,
        confirmed_bindings=confirmed_bindings,
        query_time_basis_candidates=["data_max_time", "wall_clock_now"],
        recalled_memories=memories,
        memory_recall_candidates=memories,
    )
    table_summaries = []
    for tid, t in tables_by_id.items():
        cols = list(t.get("columns") or [])
        col_cap = cols[:40]
        table_summaries.append(
            {
                "table_id": tid,
                # Backward-compatible name list + structured samples for mapping.
                "columns": col_cap,
                "column_details": _column_samples_for_table(
                    t, columns=col_cap, per_col=3
                ),
                "nrows": len(t.get("rows") or []),
            }
        )
    op_ids = list((catalog.get("operators") or {}).keys())
    # Full plans under full catalog need headroom; caller may override.
    effective_max_tokens = 8192 if max_tokens is None else int(max_tokens)
    json_format_hint = (
        "上一次输出格式不合法，请只输出纯 JSON 对象，不要包含任何 Markdown 语法"
    )

    def _prompt_body(
        *,
        repair: bool = False,
        previous_error: str = "",
        previous_plan=None,
        json_retry_hint: bool = False,
    ):
        body: dict[str, Any] = {
            "utterance": utterance,
            "tables": apply_portrait_attachments_to_tables(table_summaries, portraits),
            "operator_catalog_op_ids": op_ids,
            "must_include_rollback_anchor": ROLLBACK_QUERY_PLANNER_START,
            "confirmed_bindings": confirmed_bindings or [],
            # ask_history is part of QP envelope; must reach the model on replan.
            "ask_history": ask_history or [],
            # High-priority memory material; model expands — no local synonym rules.
            "recalled_memories": memories,
        }
        if slice_constraints:
            body["slice_constraints"] = dict(slice_constraints)
            body["slice_constraint_instruction"] = (
                "I-layer active window only. tables rows are already that window. "
                "On plan_ready set slice_constraint_ack=true; "
                "do not claim full-file coverage outside row_range."
            )
        if repair:
            body["repair"] = True
            body["previous_error"] = previous_error
            body["previous_plan"] = previous_plan
        if json_retry_hint:
            body["format_retry_hint"] = json_format_hint
        return json.dumps(body, ensure_ascii=False)

    prompt = _prompt_body()

    ai_calls = 0
    last_err = ""
    last_fail_locus = "other"
    plan: dict[str, Any] | None = None
    run_id = "qp_%s" % uuid.uuid4().hex[:12]
    network_reconnect_left = _network_reconnect_budget()
    for attempt in range(max(1, max_retries + 1)):
        ai_calls += 1
        try:
            while True:
                try:
                    raw = chat_json(
                        prompt,
                        api_key,
                        system=QP_SYSTEM,
                        max_tokens=effective_max_tokens,
                        timeout=timeout if timeout is not None else 300,
                        trace_run_id=run_id,
                    )
                    break
                except Exception as exc:  # noqa: BLE001 — classify transport vs other
                    if (
                        network_reconnect_left > 0
                        and _is_transport_wall_failure(exc)
                    ):
                        network_reconnect_left -= 1
                        last_err = f"{type(exc).__name__}: {exc}"[:300]
                        _qp_event_trace(
                            run_id,
                            "network_reconnect",
                            "attempt=%s left=%s err=%s"
                            % (attempt, network_reconnect_left, last_err),
                            seq=ai_calls,
                        )
                        ai_calls += 1
                        # Same prompt; do not rewrite into JSON/format repair.
                        continue
                    raise
        except Exception as exc:  # noqa: BLE001 — surface as retryable non_dict
            last_err = f"{type(exc).__name__}: {exc}"[:300]
            last_fail_locus = "other"
            _qp_event_trace(
                run_id,
                "retry",
                "attempt=%s err=%s" % (attempt, last_err),
                seq=ai_calls,
            )
            # Transport wall: keep same prompt; AST/format: add JSON hint.
            if _is_transport_wall_failure(exc, last_error=last_err):
                prompt = _prompt_body()
            else:
                prompt = _prompt_body(json_retry_hint=True)
            continue
        if not isinstance(raw, dict):
            last_err = str(getattr(chat_json, "last_error", "") or "non_dict")
            last_fail_locus = "other"
            if (
                network_reconnect_left > 0
                and _is_transport_wall_failure(None, last_error=last_err)
            ):
                network_reconnect_left -= 1
                _qp_event_trace(
                    run_id,
                    "network_reconnect",
                    "attempt=%s non_dict left=%s err=%s"
                    % (attempt, network_reconnect_left, last_err),
                    seq=ai_calls,
                )
                ai_calls += 1
                # Same prompt retry (transport None path).
                try:
                    raw = chat_json(
                        prompt,
                        api_key,
                        system=QP_SYSTEM,
                        max_tokens=effective_max_tokens,
                        timeout=timeout if timeout is not None else 300,
                        trace_run_id=run_id,
                    )
                except Exception as exc2:  # noqa: BLE001
                    last_err = f"{type(exc2).__name__}: {exc2}"[:300]
                    _qp_event_trace(
                        run_id,
                        "retry",
                        "attempt=%s post_reconnect_err=%s" % (attempt, last_err),
                        seq=ai_calls,
                    )
                    prompt = _prompt_body()
                    continue
                if isinstance(raw, dict):
                    pass  # fall through to normalize
                else:
                    last_err = str(
                        getattr(chat_json, "last_error", "") or last_err or "non_dict"
                    )
                    _qp_event_trace(
                        run_id,
                        "retry",
                        "attempt=%s non_dict after reconnect err=%s"
                        % (attempt, last_err),
                        seq=ai_calls,
                    )
                    prompt = _prompt_body(json_retry_hint=True)
                    continue
            else:
                _qp_event_trace(
                    run_id,
                    "retry",
                    "attempt=%s non_dict err=%s" % (attempt, last_err),
                    seq=ai_calls,
                )
                # Next attempt: force pure-JSON reminder (full schema stability).
                prompt = _prompt_body(json_retry_hint=True)
                continue
        plan = _normalize_plan(raw, table_ids=list(tables_by_id.keys()))
        plan = strip_table_alias_prefixes_in_plan(
            plan,
            table_ids=list(tables_by_id.keys()),
            tables_by_id=tables_by_id,
        )
        # Optional mechanical column remap. M10 live accept sets False so
        # illegal/missing columns surface as errors (no silent fix).
        if apply_column_repair:
            plan = _repair_column_args(plan, tables_by_id)
        if plan.get("status") == "needs_user":
            try:
                gated = validate_needs_user_plan(plan)
            except QueryEngineError as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                last_fail_locus = getattr(exc, "fail_locus", None) or "other"
                _qp_event_trace(
                    run_id,
                    "reject",
                    "attempt=%s needs_user_gate=%s" % (attempt, last_err),
                    seq=ai_calls,
                )
                prompt = _prompt_body(
                    repair=True,
                    previous_error=last_err
                    + " | If projection unknown: status=needs_user "
                    "fail_locus=projection with five non-empty fields.",
                    previous_plan=plan,
                    json_retry_hint=True,
                )
                continue
            e_route = route_e_entry(
                source="qp_needs_user",
                five_part=gated["five_part"],
            )
            _qp_event_trace(
                run_id,
                "needs_user",
                "attempt=%s fail_locus=%s" % (attempt, gated["fail_locus"]),
                seq=ai_calls,
            )
            return {
                "status": "needs_user",
                "plan": plan,
                "ai_calls": ai_calls,
                "qp_input": envelope,
                "recalled_memories": memories,
                "raw_ai": raw,
                "fail_locus": gated["fail_locus"],
                "five_part": gated["five_part"],
                "e_route": e_route,
                "trace_run_id": run_id,
                "audit": {
                    "event": "qp_needs_user_routed",
                    "fail_locus": gated["fail_locus"],
                    "trace_run_id": run_id,
                },
            }
        try:
            check_plan_ready_condition_trees(plan)
            check_plan_ready_join_keys(plan)
            check_plan_ready_no_blank_attr_literals(plan)
            check_plan_ready_projection_columns(plan, tables_by_id)
            if slice_constraints:
                from pipeline.block_cross_table.query_engine.i_slice_bridge import (
                    assert_plan_respects_slice_constraints,
                )

                assert_plan_respects_slice_constraints(
                    plan, slice_constraints, tables_by_id=tables_by_id
                )
            case_weak = annotate_projection_provenance_weak(plan)
            out = validate_query_plan_ast(
                plan,
                catalog,
                require_full_capability_cover=require_full_capability_cover,
            )
            out["ai_calls"] = ai_calls
            out["qp_input"] = envelope
            out["plan"] = plan
            out["recalled_memories"] = memories
            out["raw_ai"] = raw
            out["column_repair_applied"] = bool(apply_column_repair)
            out["trace_run_id"] = run_id
            out["provenance_weak"] = bool(case_weak)
            out["slice_constraints"] = (
                dict(slice_constraints) if slice_constraints else None
            )
            out["tables_prompt_meta"] = {
                "with_column_samples": True,
                "table_ids": list(tables_by_id.keys()),
            }
            return out
        except (QueryEngineError, ResultProjectionError, ValueError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            last_fail_locus = getattr(exc, "fail_locus", None) or "other"
            _qp_event_trace(
                run_id,
                "reject",
                "attempt=%s err=%s" % (attempt, last_err),
                seq=ai_calls,
            )
            if attempt + 1 < max(1, max_retries + 1):
                _qp_event_trace(
                    run_id,
                    "retry",
                    "attempt=%s will_retry" % attempt,
                    seq=ai_calls,
                )
            prompt = _prompt_body(
                repair=True,
                previous_error=last_err
                + " | If condition_tree_id is dangling: put the tree under "
                "condition_trees or use inline conditions; else status=needs_user "
                "fail_locus=predicate with five non-empty fields. "
                "If op_join lacks left_key/right_key (or key) and has null "
                "condition_tree_id with empty conditions: set real join keys "
                "from column_details/samples, or status=needs_user "
                "fail_locus=join_key with five Chinese fields. "
                "If columns cannot be determined: status=needs_user "
                "fail_locus=projection with five non-empty fields "
                "(speculation,evidence,what_might_be_wrong,next_step,"
                "please_confirm_or_correct). Do not invent placeholder "
                "column_id names when status=plan_ready.",
                previous_plan=plan,
                json_retry_hint=True,
            )
            continue

    raise QueryEngineError(
        "qp_ai failed after retries: %s" % last_err,
        fail_locus=last_fail_locus if last_fail_locus else "projection",
        detail={
            "ai_calls": ai_calls,
            "last_err": last_err,
            "raw_preview": str(getattr(chat_json, "last_raw", "") or "")[:800],
            "last_plan": plan,
            "trace_run_id": run_id,
        },
    )
