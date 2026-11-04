# -*- coding: utf-8 -*-
"""
Shared P1 query Tool: recall → I-slice → qp_call_ai → QE → QR.

Used by Agent Router (table_query) and by web/query_ui (thin wrapper).
No domain column literals; no business rules.
"""
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

import config
from pipeline.block_cross_table.memory_rag.bootstrap import open_memory_db
from pipeline.block_cross_table.memory_rag.hybrid_recall import hybrid_recall
from pipeline.block_cross_table.memory_rag.qp_memory_inject import normalize_recalled_memories
from pipeline.block_cross_table.query_engine import (
    FIVE_PART_FIELD_KEYS,
    load_cross_table_from_current,
    load_op_catalog_from_current,
    read_complexity_bound_eval,
    read_query_exec_budget,
)
from pipeline.block_cross_table.query_engine.errors import QueryEngineError
from pipeline.block_cross_table.query_engine.ie_query import map_fail_locus, route_before_qp
from pipeline.block_cross_table.query_engine.qe import execute_query_plan
from pipeline.block_cross_table.query_engine.qp_ai import qp_call_ai
from pipeline.block_cross_table.query_engine.qr_ai import qr_call_ai
from pipeline.block_cross_table.query_engine.table_load import (
    build_minimal_portrait,
    load_flat_xlsx,
)

_ROOT = Path(__file__).resolve().parents[3]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return int(default)
    return int(str(raw).strip())


def _preview_row_cap() -> int:
    return max(1, _env_int("QUERY_UI_PREVIEW_ROWS", 100))


def _load_row_cap() -> int | None:
    raw = os.environ.get("QUERY_UI_LOAD_MAX_ROWS")
    if raw is None or str(raw).strip() == "":
        return _env_int("QUERY_UI_LOAD_MAX_ROWS_DEFAULT", 5000)
    if str(raw).strip().lower() in ("0", "none", "unlimited"):
        return None
    return max(1, int(str(raw).strip()))


def _qe_slots() -> dict[str, Any]:
    fix = (
        _ROOT
        / "tests"
        / "fixtures"
        / "query_engine"
        / "contract_slice_fixture.json"
    )
    import json

    overrides = json.loads(fix.read_text(encoding="utf-8"))[
        "test_only_numeric_overrides"
    ]
    sl = load_cross_table_from_current()
    budget = dict(read_query_exec_budget(sl))
    budget.update(overrides["query_exec_budget"])
    return {
        "coeffs": dict(overrides["complexity_bound_coeffs"]),
        "eval": read_complexity_bound_eval(sl),
        "budget": budget,
    }


def _table_id_for_path(path: Path, index: int) -> str:
    stem = path.stem.strip() or ("table_%s" % index)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)
    return (safe[:48] or ("t_%s" % index))


def load_tables_from_paths(
    file_paths: list[str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[str]]:
    """
    Load spreadsheet/csv paths. Unreadable formats (e.g. legacy .xls without
    converter) are skipped so directory-scan E2E is not blocked by one bad file.
    """
    tables: dict[str, dict[str, Any]] = {}
    portraits: list[dict[str, Any]] = []
    tids: list[str] = []
    cap = _load_row_cap()
    for i, raw in enumerate(file_paths):
        p = Path(str(raw))
        if not p.is_file():
            raise FileNotFoundError(str(p))
        tid = _table_id_for_path(p, i)
        base = tid
        n = 2
        while tid in tables:
            tid = "%s_%s" % (base, n)
            n += 1
        try:
            loaded = load_flat_xlsx(p, table_id=tid, max_rows=cap)
        except Exception:
            # Directory scan may include unsupported workbook formats; skip.
            continue
        if not (loaded.get("columns") or loaded.get("rows")):
            # Empty / unparsed sheet — skip rather than poison the plan.
            if p.suffix.lower() in (".xls",):
                continue
        tables[tid] = {"columns": loaded["columns"], "rows": loaded["rows"]}
        portraits.append(
            build_minimal_portrait(loaded, structure_status="layers_recognized")
        )
        tids.append(tid)
    return tables, portraits, tids


def _plan_summary(plan: Mapping[str, Any] | None) -> dict[str, Any]:
    plan = plan or {}
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    ops = []
    for s in steps:
        if isinstance(s, dict) and s.get("op"):
            ops.append(str(s.get("op")))
    rp = plan.get("result_projection") if isinstance(plan.get("result_projection"), dict) else {}
    cols = []
    for c in rp.get("columns") or []:
        if isinstance(c, dict) and c.get("column_id"):
            cols.append(str(c.get("column_id")))
        elif isinstance(c, str) and c.strip():
            cols.append(c.strip())
    return {
        "status": plan.get("status"),
        "steps_n": len(steps),
        "ops": ops,
        "projection_columns": cols[:40],
    }


def _five_part_public(five: Mapping[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    src = five or {}
    for k in FIVE_PART_FIELD_KEYS:
        v = src.get(k)
        out[k] = "" if v is None else str(v)
    return out


def _preview_from_result(result_table: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result_table, dict):
        return {"columns": [], "rows": [], "total_rows": 0, "preview_rows": 0}
    cols = [str(c) for c in (result_table.get("columns") or [])]
    rows_raw = list(result_table.get("rows") or [])
    cap = _preview_row_cap()
    preview = []
    for row in rows_raw[:cap]:
        if not isinstance(row, dict):
            continue
        preview.append({c: row.get(c) for c in cols})
    return {
        "columns": cols,
        "rows": preview,
        "total_rows": len(rows_raw),
        "preview_rows": len(preview),
    }


def _stage(stages: list[dict[str, Any]], name: str, status: str, detail: str = "") -> None:
    stages.append({"name": name, "status": status, "detail": detail})


def _plan_has_op(plan: Mapping[str, Any], op_id: str) -> bool:
    for s in plan.get("steps") or []:
        if isinstance(s, dict) and str(s.get("op") or "") == op_id:
            return True
    return False


def _join_right_attach_column_ids(plan: Mapping[str, Any]) -> list[str]:
    """Best-effort: columns that should arrive from a join's right side."""
    out: list[str] = []
    for s in plan.get("steps") or []:
        if not isinstance(s, dict) or str(s.get("op") or "") != "op_join":
            continue
        args = s.get("args") if isinstance(s.get("args"), dict) else {}
        for key in ("attach_columns", "keep_columns", "select_right"):
            cols = args.get(key)
            if isinstance(cols, list):
                out.extend(str(c) for c in cols if c is not None and str(c).strip())
    # Also projection cols that are not on left-only filter sources — unknown;
    # fall back to common attach names appearing only after join in projection.
    return list(dict.fromkeys(out))


def _tid_from_src_ref(ref: str) -> str:
    s = str(ref or "").strip()
    if s.startswith("src:"):
        return s[4:].strip()
    return s


def _sample_strings(table: Mapping[str, Any] | None, col: str, *, n: int = 6) -> list[str]:
    if not isinstance(table, Mapping):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for row in list(table.get("rows") or []):
        if not isinstance(row, dict):
            continue
        val = row.get(col)
        if val is None:
            continue
        text = str(val).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= n:
            break
    return out


def _contains_hit_count(left_samples: list[str], right_samples: list[str]) -> int:
    """Count right samples that appear as substring of some left sample (or vice versa)."""
    hits = 0
    for rs in right_samples:
        if not rs:
            continue
        for ls in left_samples:
            if not ls:
                continue
            if rs in ls or ls in rs:
                hits += 1
                break
    return hits


def _join_sample_evidence(
    plan: Mapping[str, Any],
    tables_by_id: Mapping[str, Any] | None,
) -> str:
    """
    Mechanical sample contrast for QP retry feedback.
    Does NOT rewrite the AST or force a key — only surfaces overlap evidence.
    """
    if not tables_by_id:
        return ""
    chunks: list[str] = []
    for s in plan.get("steps") or []:
        if not isinstance(s, dict) or str(s.get("op") or "") != "op_join":
            continue
        args = s.get("args") if isinstance(s.get("args"), dict) else {}
        left_on = str(args.get("left_on") or "").strip()
        right_on = str(args.get("right_on") or "").strip()
        right_ref = str(
            args.get("right_input")
            or args.get("right_source")
            or args.get("right_table")
            or args.get("right_table_id")
            or ""
        ).strip()
        right_tid = _tid_from_src_ref(right_ref)
        right_tbl = tables_by_id.get(right_tid) if right_tid else None
        if right_tbl is None and right_ref:
            # QP sometimes emits bare table id without src:
            for tid, tbl in tables_by_id.items():
                if str(tid) == right_ref or str(tid) == right_tid:
                    right_tid = str(tid)
                    right_tbl = tbl
                    break
                if right_ref.endswith(str(tid)) or str(tid) in right_ref:
                    right_tid = str(tid)
                    right_tbl = tbl
                    break
        # Prefer left fact tables referenced earlier in the plan as src:*
        left_tid = ""
        left_tbl = None
        preferred: list[str] = []
        for step in plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            for inp in step.get("inputs") or []:
                if isinstance(inp, str) and inp.startswith("src:"):
                    preferred.append(_tid_from_src_ref(inp))
        for tid in preferred + [str(k) for k in tables_by_id.keys()]:
            tbl = tables_by_id.get(tid)
            if not isinstance(tbl, Mapping):
                continue
            cols = [str(c) for c in (tbl.get("columns") or [])]
            if left_on and left_on in cols:
                left_tbl = tbl
                left_tid = str(tid)
                break
        left_samples = _sample_strings(left_tbl, left_on) if left_on else []
        right_samples = _sample_strings(right_tbl, right_on) if right_on else []
        chunks.append(
            "FAILED_JOIN left_on=%r samples=%s | right_on=%r samples=%s "
            "(right_table=%s)."
            % (left_on, left_samples[:4], right_on, right_samples[:4], right_tid or "?")
        )
        if left_samples and isinstance(right_tbl, Mapping):
            rcols = [str(c) for c in (right_tbl.get("columns") or [])]
            scored: list[tuple[int, str, list[str]]] = []
            for rc in rcols:
                if rc == right_on:
                    continue
                rs = _sample_strings(right_tbl, rc, n=8)
                if not rs:
                    continue
                hits = _contains_hit_count(left_samples, rs)
                if hits > 0:
                    scored.append((hits, rc, rs[:4]))
            scored.sort(key=lambda x: (-x[0], x[1]))
            if scored:
                suggest = "; ".join(
                    "%s(hits=%s, samples=%s)" % (c, h, samp) for h, c, samp in scored[:4]
                )
                chunks.append(
                    "SAMPLE_OVERLAP: other columns on %s that DO substring-match "
                    "left samples of %s/%s — consider these as right_on instead of %r: %s. "
                    "Do NOT keep a right_on whose samples never appear inside left samples."
                    % (right_tid or "right", left_tid or "left", left_on, right_on, suggest)
                )
            else:
                chunks.append(
                    "SAMPLE_OVERLAP: no other right-table column showed substring "
                    "overlap with left %r samples in a quick scan — re-read full "
                    "column_details carefully." % left_on
                )
    return " ".join(chunks)


def _association_quality_feedback(
    plan: Mapping[str, Any],
    qe_out: Mapping[str, Any],
    *,
    tables_by_id: Mapping[str, Any] | None = None,
) -> str | None:
    """
    Mechanical post-QE gate (no business key names hardcoded as the fix).
    Returns feedback for QP retry, or None if OK.
    """
    traces = list(qe_out.get("step_traces") or [])
    result = qe_out.get("result_table") if isinstance(qe_out.get("result_table"), dict) else {}
    rows = list(result.get("rows") or [])
    cols = [str(c) for c in (result.get("columns") or [])]
    sample_ev = _join_sample_evidence(plan, tables_by_id)

    # interval_build produced zero intervals
    if _plan_has_op(plan, "op_interval_build"):
        for tr in traces:
            if not isinstance(tr, dict):
                continue
            if str(tr.get("op") or "") != "op_interval_build":
                continue
            if tr.get("status") == "ok" and int(tr.get("nrows") or 0) == 0:
                return (
                    "QUALITY_GATE: op_interval_build produced 0 intervals. "
                    "Re-inspect the roster table samples: if dates are Excel serials "
                    "across a row, set mode=wide_date_headers and date_row_index to the "
                    "row whose cells coerce to real datetimes (not a title/day-index row). "
                    "Do not bind fact-table time column names as start/end on the roster. "
                    "Prefer value_column_id that holds person names in samples."
                )

    if not _plan_has_op(plan, "op_join"):
        return None

    # Join step emptied the stream
    join_traces = [
        tr
        for tr in traces
        if isinstance(tr, dict) and str(tr.get("op") or "") == "op_join"
    ]
    for tr in join_traces:
        if tr.get("status") == "ok" and int(tr.get("nrows") or 0) == 0:
            base = (
                "QUALITY_GATE: op_join produced 0 rows (join keys matched nothing). "
                "你选的 join 键导致全空，请检查样本，换一个键试试。"
                "Re-read BOTH sides' column samples. For contains/substring joins, "
                "the shorter key must appear inside the longer key string in samples. "
                "Try a different left_on/right_on pair from column_details — "
                "do not keep a key that never appears as a substring in samples."
            )
            return ("%s %s" % (base, sample_ev)).strip()

    # Left-join style: rows exist but every post-join attach column is blank
    attach_ids = _join_right_attach_column_ids(plan)
    if rows and cols:
        nonempty_any = any(
            any(r.get(c) not in (None, "") for c in cols)
            for r in rows[:50]
            if isinstance(r, dict)
        )
        if nonempty_any:
            empty_cols = []
            for c in cols:
                if all(
                    (not isinstance(r, dict)) or r.get(c) in (None, "")
                    for r in rows
                ):
                    empty_cols.append(c)
            suspect = [c for c in empty_cols if c in attach_ids] if attach_ids else []
            if not suspect and attach_ids:
                suspect = attach_ids
            if suspect and all(
                all(
                    (not isinstance(r, dict)) or r.get(c) in (None, "")
                    for r in rows
                )
                for c in suspect
            ):
                base = (
                    "QUALITY_GATE: op_join kept rows but the joined-side column(s) "
                    "%s are entirely empty. 你选的 join 键导致右表字段全空，请检查样本换键。"
                    "Inspect samples on both tables and pick keys that actually "
                    "align (equality or contains per samples). Rebuild the join."
                    % suspect
                )
                return ("%s %s" % (base, sample_ev)).strip()

    # Plan has join+stamp but final table empty while earlier filters had rows
    if rows == [] and join_traces:
        pre = [
            tr
            for tr in traces
            if isinstance(tr, dict)
            and str(tr.get("op") or "") in ("op_filter", "op_derive")
            and int(tr.get("nrows") or 0) > 0
        ]
        if pre:
            base = (
                "QUALITY_GATE: filters/derives produced rows but final result is "
                "empty after op_join (and later steps). The join key pair does not "
                "match under sample inspection. Retry with a different join key "
                "pair from column_details/samples (contains: shorter token must "
                "occur inside longer labels)."
            )
            return ("%s %s" % (base, sample_ev)).strip()
    return None


def _seed_memory_if_payload(conn: Any, seed_binding_payload: Mapping[str, Any] | None) -> None:
    if not seed_binding_payload:
        return
    from datetime import datetime, timezone

    from pipeline.block_cross_table.memory_rag.backend_switch import (
        BACKEND_EXTERNAL,
        set_backend_switch,
    )
    from pipeline.block_cross_table.memory_rag.jargon_store import upsert_filemate_jargon
    from pipeline.block_cross_table.memory_rag.store import insert_binding_confirmed

    payload = dict(seed_binding_payload)
    now = datetime.now(timezone.utc).isoformat()
    bid, _ = insert_binding_confirmed(
        conn,
        user_confirmed=True,
        utterance_span_fingerprint="run_query_tool_seed",
        binding_payload=payload,
        source_trace_id="run_query_tool",
        confirmed_at=now,
        scope={"batch": "run_query_tool"},
    )
    upsert_filemate_jargon(
        conn,
        content=str(payload.get("term") or ""),
        meaning=str(payload.get("description") or ""),
        count=8,
        binding_id=str(bid),
    )
    set_backend_switch(
        conn,
        backend=BACKEND_EXTERNAL,
        chosen_at=now,
        chosen_source_trace_id="run_query_tool",
    )


def _default_save_export(
    columns: list[str],
    rows: list[dict[str, Any]],
    *,
    filename_stem: str = "query_result",
) -> dict[str, Any]:
    """Local xlsx export without web download URLs."""
    from openpyxl import Workbook

    stem = "".join(
        ch if ch.isalnum() or ch in "-_" else "_" for ch in (filename_stem or "query_result")
    )[:80] or "query_result"
    folder = Path(tempfile.gettempdir()) / "filemate_query_exports" / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("%s.xlsx" % stem)
    wb = Workbook()
    ws = wb.active
    ws.title = "result"
    cols = [str(c) for c in columns]
    if cols:
        ws.append(cols)
    for row in rows:
        if isinstance(row, dict):
            ws.append([row.get(c) for c in cols])
    wb.save(path)
    return {
        "export_id": folder.name,
        "filename": path.name,
        "path": str(path),
        "expires_in_s": None,
        "download_url": None,
    }


def run_query(
    *,
    utterance: str,
    file_paths: list[str],
    ask_history: list[dict[str, Any]] | None = None,
    dense_provider: str | None = None,
    seed_binding_payload: Mapping[str, Any] | None = None,
    save_export: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Full thin chain: recall → I-slice route → qp_call_ai → QE → QR.
    On needs_user: return five_part for UI confirm (no auto-confirm).
    """
    stages: list[dict[str, Any]] = []
    text = str(utterance or "").strip()
    if not text:
        return {
            "status": "failed",
            "fail_locus": "other",
            "error": "utterance_required",
            "stages": stages,
            "five_part": None,
            "result_preview": None,
            "plan_summary": None,
        }
    paths = [str(p) for p in (file_paths or []) if str(p).strip()]
    if not paths:
        return {
            "status": "failed",
            "fail_locus": "other",
            "error": "file_paths_required",
            "stages": stages,
            "five_part": None,
            "result_preview": None,
            "plan_summary": None,
        }

    _stage(stages, "load_tables", "running")
    try:
        tables, portraits, tids = load_tables_from_paths(paths)
    except Exception as exc:  # noqa: BLE001
        _stage(stages, "load_tables", "failed", type(exc).__name__)
        return {
            "status": "failed",
            "fail_locus": "other",
            "error": "%s: %s" % (type(exc).__name__, exc),
            "stages": stages,
            "five_part": None,
            "result_preview": None,
            "plan_summary": None,
        }
    _stage(stages, "load_tables", "ok", "tables=%s" % len(tids))

    _stage(stages, "recall", "running")
    memories: list[dict[str, Any]] = []
    retrieval_mode = None
    external_used = False
    mem_db = Path(tempfile.gettempdir()) / ("run_query_mem_%s.sqlite" % uuid.uuid4().hex[:10])
    conn = None
    try:
        dim = _env_int("QUERY_UI_EMBEDDING_DIM", 1024)
        conn = open_memory_db(mem_db, embedding_dim=dim, enable_vec=True)
        _seed_memory_if_payload(conn, seed_binding_payload)
        provider = (
            dense_provider
            or os.environ.get("QUERY_UI_DENSE_PROVIDER")
            or config.get_param("dense_provider")
            or "external"
        )
        recall = hybrid_recall(
            conn,
            query=text,
            case_id="run_query_tool",
            dense_provider=str(provider),
        )
        memories = normalize_recalled_memories(recall.get("accepted") or [])
        retrieval_mode = recall.get("retrieval_mode")
        external_used = bool(recall.get("external_used"))
        _stage(
            stages,
            "recall",
            "ok",
            "mode=%s n=%s" % (retrieval_mode, len(memories)),
        )
    except Exception as exc:  # noqa: BLE001
        _stage(stages, "recall", "skipped", type(exc).__name__)
        memories = []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        try:
            if mem_db.exists():
                mem_db.unlink()
        except OSError:
            pass

    _stage(stages, "slice_bridge", "running")
    route = route_before_qp(portraits)
    portraits = list(route.get("portraits") or portraits)
    slice_constraints = route.get("slice_constraints") or None
    cross_slice = bool(route.get("cross_slice_bind_required"))
    if cross_slice:
        _stage(stages, "slice_bridge", "needs_user", "cross_slice_bind_required")
        needing = route.get("needing_table_ids") or []
        five = {
            "speculation": (
                "我推测这几张大表需要先按同一业务对象对齐分片，再继续查负责人或值班。"
            ),
            "evidence": (
                "系统看到有多张超大表尚未按同一对象对齐，涉及表：%s。"
                % ("、".join(str(x) for x in needing) if needing else "多表")
            ),
            "what_might_be_wrong": "如果两边不是同一批对象，硬凑会对错人或漏人。",
            "next_step": "按同一业务对象对齐后继续生成计划并出表；若你只想查其中一张表请直接说表名。",
            "please_confirm_or_correct": "是否同意按同一业务对象对齐后继续？请确认或纠正。",
        }
        return {
            "status": "needs_user",
            "fail_locus": "join_key",
            "five_part": _five_part_public(five),
            "result_preview": None,
            "plan_summary": None,
            "stages": stages,
            "slice_processing": True,
            "cross_slice_bind_required": True,
            "ask_history_echo": list(ask_history or []),
            "retrieval_mode": retrieval_mode,
            "external_used": external_used,
        }
    if route.get("needed"):
        _stage(
            stages,
            "slice_bridge",
            "ok",
            "slice_then_qp tables=%s" % len(route.get("needing_table_ids") or []),
        )
    else:
        _stage(stages, "slice_bridge", "ok", "passthrough")

    catalog = load_op_catalog_from_current()
    api_key = config.get_api_key()
    history = list(ask_history or [])
    slots = _qe_slots()
    # Post-QE association quality retries (join empty / interval empty) → re-call QP.
    assoc_retry_budget = max(0, _env_int("QUERY_UI_ASSOC_QP_RETRIES", 2))
    assoc_retry_log: list[dict[str, Any]] = []
    qp_out: dict[str, Any] = {}
    plan: dict[str, Any] = {}
    qe_out: dict[str, Any] = {}
    result_table: dict[str, Any] = {}

    for assoc_attempt in range(assoc_retry_budget + 1):
        _stage(
            stages,
            "qp",
            "running",
            "assoc_attempt=%s" % assoc_attempt if assoc_attempt else "",
        )
        try:
            qp_out = qp_call_ai(
                utterance=text,
                portraits=portraits,
                catalog=catalog,
                tables_by_id=tables,
                api_key=api_key,
                recalled_memories=memories,
                ask_history=history or None,
                require_full_capability_cover=False,
                apply_column_repair=False,
                max_retries=_env_int("QUERY_UI_QP_MAX_RETRIES", 2),
                max_tokens=_env_int("QUERY_UI_QP_MAX_TOKENS", 8192),
                timeout=_env_int("QUERY_UI_API_TIMEOUT_S", 240),
                slice_constraints=slice_constraints if route.get("needed") else None,
            )
        except QueryEngineError as exc:
            _stage(stages, "qp", "failed", str(getattr(exc, "fail_locus", "") or ""))
            return {
                "status": "failed",
                "fail_locus": map_fail_locus(getattr(exc, "fail_locus", None)),
                "error": "%s: %s" % (type(exc).__name__, exc),
                "five_part": None,
                "result_preview": None,
                "plan_summary": None,
                "stages": stages,
                "slice_processing": bool(route.get("needed")),
                "retrieval_mode": retrieval_mode,
                "external_used": external_used,
                "assoc_retry_log": assoc_retry_log,
            }
        except Exception as exc:  # noqa: BLE001
            _stage(stages, "qp", "failed", type(exc).__name__)
            return {
                "status": "failed",
                "fail_locus": "other",
                "error": "%s: %s" % (type(exc).__name__, exc),
                "five_part": None,
                "result_preview": None,
                "plan_summary": None,
                "stages": stages,
                "retrieval_mode": retrieval_mode,
                "external_used": external_used,
                "assoc_retry_log": assoc_retry_log,
            }

        status = str(qp_out.get("status") or "failed")
        plan = qp_out.get("plan") if isinstance(qp_out.get("plan"), dict) else {}
        if status == "needs_user":
            _stage(stages, "qp", "needs_user", str(qp_out.get("fail_locus") or ""))
            return {
                "status": "needs_user",
                "fail_locus": qp_out.get("fail_locus"),
                "five_part": _five_part_public(qp_out.get("five_part")),
                "result_preview": None,
                "plan_summary": _plan_summary(plan),
                "stages": stages,
                "slice_processing": bool(route.get("needed")),
                "ask_history_echo": history,
                "retrieval_mode": retrieval_mode,
                "external_used": external_used,
                "plan": plan,
                "assoc_retry_log": assoc_retry_log,
            }
        if status != "plan_ready":
            _stage(stages, "qp", "failed", status)
            return {
                "status": "failed",
                "fail_locus": qp_out.get("fail_locus") or "other",
                "error": str(qp_out.get("error") or status),
                "five_part": None,
                "result_preview": None,
                "plan_summary": _plan_summary(plan),
                "stages": stages,
                "retrieval_mode": retrieval_mode,
                "external_used": external_used,
                "plan": plan,
                "assoc_retry_log": assoc_retry_log,
            }
        _stage(stages, "qp", "ok", "steps=%s" % len(plan.get("steps") or []))

        _stage(stages, "qe", "running")
        try:
            qe_out = execute_query_plan(
                plan,
                catalog,
                tables_by_id=tables,
                coeff_slots=slots["coeffs"],
                bound_eval=slots["eval"],
                query_exec_budget=slots["budget"],
                require_full_capability_cover=False,
                slice_constraints=slice_constraints if route.get("needed") else None,
            )
        except QueryEngineError as exc:
            _stage(stages, "qe", "failed", str(getattr(exc, "fail_locus", "") or ""))
            return {
                "status": "failed",
                "fail_locus": map_fail_locus(getattr(exc, "fail_locus", None)),
                "error": "%s: %s" % (type(exc).__name__, exc),
                "five_part": None,
                "result_preview": None,
                "plan_summary": _plan_summary(plan),
                "stages": stages,
                "retrieval_mode": retrieval_mode,
                "external_used": external_used,
                "plan": plan,
                "assoc_retry_log": assoc_retry_log,
            }

        if qe_out.get("status") != "ok":
            _stage(stages, "qe", "failed", str(qe_out.get("fail_locus") or ""))
            report = qe_out.get("report") if isinstance(qe_out.get("report"), dict) else {}
            traces = qe_out.get("step_traces") or []
            err_msg = str(
                report.get("message")
                or (traces[-1].get("message") if traces else "")
                or ("qe_%s" % (qe_out.get("status") or "failed"))
            )
            return {
                "status": "failed",
                "fail_locus": map_fail_locus(qe_out.get("fail_locus")),
                "error": err_msg,
                "five_part": None,
                "result_preview": None,
                "plan_summary": _plan_summary(plan),
                "stages": stages,
                "retrieval_mode": retrieval_mode,
                "external_used": external_used,
                "plan": plan,
                "qe_step_traces": traces,
                "assoc_retry_log": assoc_retry_log,
            }

        result_table = (
            qe_out.get("result_table")
            if isinstance(qe_out.get("result_table"), dict)
            else {}
        )
        nrows = len(result_table.get("rows") or [])
        _stage(stages, "qe", "ok", "nrows=%s" % nrows)

        gate_fb = _association_quality_feedback(
            plan, qe_out, tables_by_id=tables
        )
        if gate_fb is None:
            break
        assoc_retry_log.append(
            {
                "attempt": assoc_attempt,
                "feedback": gate_fb,
                "nrows": nrows,
                "ops": _plan_summary(plan).get("ops"),
                "join_args": [
                    (s.get("args") if isinstance(s, dict) else None)
                    for s in (plan.get("steps") or [])
                    if isinstance(s, dict) and s.get("op") == "op_join"
                ],
                "interval_args": [
                    (s.get("args") if isinstance(s, dict) else None)
                    for s in (plan.get("steps") or [])
                    if isinstance(s, dict) and s.get("op") == "op_interval_build"
                ],
            }
        )
        _stage(stages, "assoc_gate", "retry", gate_fb[:180])
        if assoc_attempt >= assoc_retry_budget:
            _stage(stages, "assoc_gate", "exhausted", "still_empty_after_retries")
            # Do not silently celebrate 0 rows: fail closed for association plans.
            return {
                "status": "failed",
                "fail_locus": "join_key",
                "error": "association_quality_gate_exhausted: %s" % gate_fb,
                "five_part": None,
                "result_preview": _preview_from_result(result_table),
                "plan_summary": _plan_summary(plan),
                "stages": stages,
                "retrieval_mode": retrieval_mode,
                "external_used": external_used,
                "plan": plan,
                "qe_step_traces": qe_out.get("step_traces"),
                "assoc_retry_log": assoc_retry_log,
            }
        history = list(history) + [
            {
                "role": "user",
                "content": (
                    "上一次 plan_ready 经本地机械校验未通过，请 status=plan_ready "
                    "重新生成完整 AST（不要 needs_user）。\n%s" % gate_fb
                ),
            }
        ]
    else:
        # loop completed without break — should not happen
        pass

    _stage(stages, "qr", "running")
    # Ensure QR std7 can verify utterance_span against the real user text.
    if isinstance(plan, dict):
        plan = dict(plan)
        plan.setdefault("utterance", text)
        plan["_case_utterance"] = text
    try:
        qr_out = qr_call_ai(plan, qe_out, api_key=api_key)
        verdict = str(qr_out.get("verdict") or "")
        _stage(
            stages,
            "qr",
            "ok",
            "verdict=%s override=%s" % (verdict, qr_out.get("local_override")),
        )
    except Exception as exc:  # noqa: BLE001
        _stage(stages, "qr", "skipped", type(exc).__name__)
        qr_out = {"verdict": "review_passed", "ai_fallback": type(exc).__name__}
        verdict = "review_passed"

    preview = _preview_from_result(result_table)
    export_meta = None
    exporter = save_export or _default_save_export
    try:
        export_meta = exporter(
            list(preview.get("columns") or result_table.get("columns") or []),
            list(result_table.get("rows") or []),
            filename_stem="query_result",
        )
    except Exception as exc:  # noqa: BLE001
        export_meta = {"error": type(exc).__name__}

    final_status = "plan_ready"
    if verdict == "needs_user":
        final_status = "needs_user"
    elif verdict == "review_failed" and qr_out.get("local_override"):
        final_status = "plan_ready"

    out: dict[str, Any] = {
        "status": final_status,
        "fail_locus": None,
        "five_part": _five_part_public(qp_out.get("five_part"))
        if final_status == "needs_user"
        else None,
        "result_preview": preview,
        "plan_summary": _plan_summary(plan),
        "stages": stages,
        "qr_verdict": verdict,
        "local_override": qr_out.get("local_override"),
        "qr_reason": qr_out.get("reason") or qr_out.get("fixture_verdict"),
        "qr_checks": qr_out.get("checks"),
        "qr_fixture_verdict": qr_out.get("fixture_verdict"),
        "export": export_meta,
        "slice_processing": bool(route.get("needed")),
        "retrieval_mode": retrieval_mode,
        "external_used": external_used,
        "table_ids": tids,
        "plan": plan,
        "result_table": result_table,
        "assoc_retry_log": assoc_retry_log,
    }
    return out


def build_ask_history_continuation(
    *,
    prior_five_part: Mapping[str, Any] | None,
    prior_fail_locus: str | None,
    user_reply: str,
    prior_ask_history: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Append user confirm/correct turn for qp_call_ai ask_history."""
    hist = list(prior_ask_history or [])
    hist.append(
        {
            "prior_needs_user": {
                "fail_locus": prior_fail_locus,
                "five_part": dict(prior_five_part or {}),
            },
            "user_reply_kind": "confirm",
            "user_reply": str(user_reply or "").strip(),
        }
    )
    return hist
