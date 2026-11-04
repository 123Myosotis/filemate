# -*- coding: utf-8 -*-
"""
System data context for M4.4 wizard (tables / queryable columns).

Sources (priority):
1. MEMORY_CORRECTION_CONTEXT_JSON — path to QE portrait JSON or multi-table map
2. MEMORY_CORRECTION_CONTEXT_XLSX — path to workbook → build_minimal_portrait
3. In-memory session override (load_placeholder / load_path)
4. Empty → UI offers「加载示例列结构」

Never hardcode domain business terms (device/dept/table names) in this module.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PLACEHOLDER_TABLES = (
    _PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "query_engine"
    / "tables_abstract.json"
)

# Process-local override after user clicks「加载示例」or loads a path.
_SESSION: dict[str, Any] | None = None


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    return Path(str(raw).strip()).expanduser()


def _normalize_from_portrait(portrait: dict[str, Any]) -> dict[str, Any]:
    table_id = portrait.get("table_id") or portrait.get("block_id") or "table"
    ext = portrait.get("portrait_query_extensions") or {}
    cols_out = []
    for c in ext.get("queryable_columns") or []:
        if not isinstance(c, dict):
            continue
        cid = c.get("column_id") or ""
        name = c.get("column_name")
        if not name and isinstance(cid, str) and cid.startswith("col:"):
            name = cid[4:]
        if not name:
            name = str(cid) if cid else None
        if not name:
            continue
        cols_out.append(
            {
                "column_id": cid or ("col:%s" % name),
                "column_name": str(name),
                "type_class": c.get("type_class") or "string_or_mixed",
            }
        )
    # Fallback: bare columns list on portrait
    if not cols_out and portrait.get("columns"):
        for name in portrait["columns"]:
            cols_out.append(
                {
                    "column_id": "col:%s" % name,
                    "column_name": str(name),
                    "type_class": "string_or_mixed",
                }
            )
    return {
        "table_id": str(table_id),
        "columns": cols_out,
        "structure_status": portrait.get("structure_status"),
    }


def _from_abstract_tables(data: dict[str, Any]) -> list[dict[str, Any]]:
    from pipeline.block_cross_table.query_engine.table_load import (
        build_minimal_portrait,
    )

    tables = []
    for tid, body in data.items():
        if not isinstance(body, dict):
            continue
        handle = {
            "table_id": tid,
            "columns": list(body.get("columns") or []),
            "rows": list(body.get("rows") or []),
        }
        portrait = build_minimal_portrait(handle)
        tables.append(_normalize_from_portrait(portrait))
    return tables


def _load_json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _try_live() -> tuple[list[dict[str, Any]], str] | None:
    """Return (tables, source_tag) or None."""
    jpath = _env_path("MEMORY_CORRECTION_CONTEXT_JSON")
    if jpath is not None and jpath.is_file():
        data = _load_json_file(jpath)
        if "portrait_query_extensions" in data or "queryable_columns" in (
            data.get("portrait_query_extensions") or {}
        ):
            return [_normalize_from_portrait(data)], "env_json_portrait"
        if "portrait_query_extensions" in data:
            return [_normalize_from_portrait(data)], "env_json_portrait"
        # map of table_id -> {columns, rows} or list of portraits
        if isinstance(data, dict) and all(
            isinstance(v, dict) and ("columns" in v or "portrait_query_extensions" in v)
            for v in data.values()
        ):
            tables = []
            for tid, body in data.items():
                if "portrait_query_extensions" in body:
                    body = dict(body)
                    body.setdefault("table_id", tid)
                    tables.append(_normalize_from_portrait(body))
                else:
                    tables.extend(_from_abstract_tables({tid: body}))
            return tables, "env_json_tables"

    xpath = _env_path("MEMORY_CORRECTION_CONTEXT_XLSX")
    if xpath is not None and xpath.is_file():
        from pipeline.block_cross_table.query_engine.table_load import (
            build_minimal_portrait,
            load_flat_xlsx,
        )

        table = load_flat_xlsx(xpath, table_id=xpath.stem)
        portrait = build_minimal_portrait(table)
        return [_normalize_from_portrait(portrait)], "env_xlsx"

    return None


def get_system_data_context() -> dict[str, Any]:
    global _SESSION
    if _SESSION is not None:
        return dict(_SESSION)

    live = _try_live()
    if live is not None:
        tables, source = live
        return {
            "source": source,
            "placeholder": False,
            "tables": tables,
            "column_names": _flatten_column_names(tables),
        }

    return {
        "source": "empty",
        "placeholder": False,
        "tables": [],
        "column_names": [],
        "hint": "暂无画像。可点「加载示例列结构」，或设置环境变量 MEMORY_CORRECTION_CONTEXT_JSON / MEMORY_CORRECTION_CONTEXT_XLSX。",
    }


def load_placeholder_context() -> dict[str, Any]:
    global _SESSION
    if not _PLACEHOLDER_TABLES.is_file():
        raise FileNotFoundError("placeholder_tables_missing")
    data = _load_json_file(_PLACEHOLDER_TABLES)
    tables = _from_abstract_tables(data)
    _SESSION = {
        "source": "placeholder_abstract",
        "placeholder": True,
        "tables": tables,
        "column_names": _flatten_column_names(tables),
        "hint": "当前为通用示例列（非业务表）。可用环境变量切换为真实画像。",
    }
    return dict(_SESSION)


def clear_session_context() -> dict[str, Any]:
    global _SESSION
    _SESSION = None
    return get_system_data_context()


def _flatten_column_names(tables: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in tables:
        for c in t.get("columns") or []:
            name = c.get("column_name")
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(str(name))
    return out


def build_or_binding_payload(
    *,
    term: str,
    columns: list[str],
    values: list[str],
) -> dict[str, Any]:
    """
    Legacy M4.4 column×value OR tree. Kept for archive/tests only.
    New UI must use build_general_binding_payload (M4.5).
    """
    term_s = str(term).strip()
    cols = [str(c).strip() for c in columns if str(c).strip()]
    vals = [str(v).strip() for v in values if str(v).strip()]
    if not term_s:
        raise ValueError("term_required")
    if not cols:
        raise ValueError("columns_required")
    if not vals:
        raise ValueError("values_required")
    predicates = []
    for col in cols:
        for val in vals:
            predicates.append({"col": col, "val": val})
    return {
        "term": term_s,
        "op": "OR",
        "predicates": predicates,
    }


def _split_values(text: str) -> list[str]:
    """Split optional tag-like values from description; no column inference."""
    raw = (
        (text or "")
        .replace("，", ",")
        .replace("、", ",")
        .replace("\r", "\n")
    )
    out: list[str] = []
    for part in raw.replace("\n", ",").split(","):
        p = part.strip()
        if p:
            out.append(p)
    return out


def build_general_binding_payload(
    *,
    term: str,
    description: str,
    scope: str = "",
    values: list[str] | None = None,
) -> dict[str, Any]:
    """
    M4.5 generalized knowledge payload.
    Local storage only — no column mapping / no semantic guess.
    """
    term_s = str(term).strip()
    desc_s = str(description).strip()
    scope_s = str(scope or "").strip()
    if not term_s:
        raise ValueError("term_required")
    if not desc_s:
        raise ValueError("description_required")
    if values is None:
        # Optional convenience: if description looks like a short tag list, mirror into values.
        # Never invent column names.
        sep = ("," in desc_s) or ("，" in description) or ("、" in description) or (
            "\n" in description
        )
        vals = _split_values(desc_s) if sep else []
    else:
        vals = [str(v).strip() for v in values if str(v).strip()]
    return {
        "term": term_s,
        "description": desc_s,
        "values": vals,
        "scope": scope_s,
    }


def fingerprint_for_term(term: str) -> str:
    """Stable utterance_span_fingerprint from business term (no hand ID)."""
    import hashlib

    t = str(term).strip()
    digest = hashlib.sha256(t.encode("utf-8")).hexdigest()[:16]
    return "term:%s:%s" % (digest, t[:64])
