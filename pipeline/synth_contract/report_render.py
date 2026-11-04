# -*- coding: utf-8 -*-
"""Local reading layer for a direction-3 summary. No AI, no plan_id12."""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Labels match d3-summary-1. No branch on a business field name.
HTML_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head><body>
<p>schema_version: d4-report-1</p>
<p>contract_version: {contract_version}</p>
<p>contract_hash: {contract_hash}</p>
<p>triage: {triage}</p>
<p>fired_signals: {fired_signals}</p>
<p>primary_table: {primary_table}</p>
<p>ops_present: {ops_present}</p>
<p>row_count_out: {row_count_out}</p>
<p>column_count_out: {column_count_out}</p>
<p>evidence_path: {evidence_path}</p>
<p>human_text: {human_text}</p>
<p>sample_size: {sample_size}</p>
<p>sample_ref_kind: {sample_ref_kind}</p>
{output_is_empty_line}{evidence_file_missing_line}</body></html>
"""

TEXT_TEMPLATE = """schema_version: d4-report-1
contract_version: {contract_version}
contract_hash: {contract_hash}
triage: {triage}
fired_signals: {fired_signals}
primary_table: {primary_table}
ops_present: {ops_present}
row_count_out: {row_count_out}
column_count_out: {column_count_out}
evidence_path: {evidence_path}
human_text: {human_text}
sample_size: {sample_size}
sample_ref_kind: {sample_ref_kind}
{output_is_empty_line}{evidence_file_missing_line}"""

_REQUIRED = (
    "schema_version",
    "contract_version",
    "contract_hash",
    "triage",
    "fired_signals",
    "primary_table",
    "ops_present",
    "row_count_out",
    "column_count_out",
    "sample_ref",
    "evidence_path",
    "human_text",
)

_PLACEHOLDER = re.compile(r"\{([a-z0-9_]+)\}")


def render_report(summary: dict, evidence_body: dict, dest_root: Optional[str] = None,
                  clock=None, fail_parts=None) -> dict:
    """Return exactly report_status, report_html_path, report_text_path, evidence_path.

    P2-2 / P2-3: if the html body, the text body, or the meta file fails to write,
    report_status is write_failed. Files already written are kept. The path for a
    part that was not written is null. A sibling that succeeded is not deleted.
    """
    _ = evidence_body
    raw_path = summary.get("evidence_path") if isinstance(summary, dict) else None
    empty = _result("input_invalid", None, None, raw_path if isinstance(raw_path, str) else None)
    if not isinstance(summary, dict):
        return empty
    if not _summary_shape_ok(summary):
        return empty
    located = _locate(str(summary.get("evidence_path")))
    if located is None:
        return empty
    version, plan_id, evidence_rel = located
    fields = _fields(summary, evidence_rel)
    if fields is None:
        return empty
    root = _root(dest_root)
    if root is None:
        return _result("write_failed", None, None, evidence_rel)
    evidence_file = root.joinpath(*evidence_rel.split("/"))
    if not evidence_file.is_file():
        fields["evidence_file_missing_line"] = "evidence_file_missing: true\n"
    html_rel = "report/%s/%s.html" % (version, plan_id)
    text_rel = "report/%s/%s.txt" % (version, plan_id)
    meta_rel = "report/%s/%s.meta.json" % (version, plan_id)
    html_body = _fill(HTML_TEMPLATE, _escaped(fields)).replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    text_body = _fill(TEXT_TEMPLATE, fields).replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    stamped = clock() if clock is not None else datetime.now(timezone.utc).isoformat()
    meta_body = (json.dumps({"rendered_at": stamped}, sort_keys=True) + "\n").encode("utf-8")
    blocked = set(fail_parts or ())
    # Any one of the three parts missing => write_failed. Do not delete the others.
    html_path = _store(root, html_rel, html_body, "html" in blocked)
    text_path = _store(root, text_rel, text_body, "txt" in blocked)
    meta_path = _store(root, meta_rel, meta_body, "meta" in blocked)
    if html_path is None or text_path is None or meta_path is None:
        return _result("write_failed", html_path, text_path, evidence_rel)
    return _result("written", html_path, text_path, evidence_rel)


def _result(status, html_path, text_path, evidence_path) -> dict:
    return {
        "report_status": status,
        "report_html_path": html_path,
        "report_text_path": text_path,
        "evidence_path": evidence_path,
    }


def _summary_shape_ok(summary: dict) -> bool:
    for key in _REQUIRED:
        if key not in summary:
            return False
    path = summary.get("evidence_path")
    if not isinstance(path, str) or not path.strip():
        return False
    text = summary.get("human_text")
    if not isinstance(text, str) or not text.strip():
        return False
    if not str(summary.get("primary_table") or "").strip():
        return False
    return True


def _locate(evidence_path: str):
    parts = evidence_path.replace("\\", "/").split("/")
    if len(parts) != 3 or parts[0] != "evidence":
        return None
    if any(part in ("", ".", "..") for part in parts):
        return None
    filename = parts[2]
    if not filename.endswith(".json"):
        return None
    plan_id = filename[:-len(".json")]
    if not plan_id or not parts[1]:
        return None
    return parts[1], plan_id, "/".join(parts)


def _fields(summary: dict, evidence_rel: str):
    ref = summary.get("sample_ref")
    if not isinstance(ref, dict):
        return None
    coords = ref.get("sample_coords")
    manifest = ref.get("sample_manifest_id")
    if isinstance(coords, list) and coords:
        sample_size = len(coords)
        sample_kind = "coords"
        empty_line = ""
    elif isinstance(manifest, str) and manifest.strip():
        sample_size = 0
        sample_kind = "manifest"
        empty_line = "output_is_empty: true\n"
    else:
        return None
    signals = summary.get("fired_signals") or []
    ops = summary.get("ops_present") or []
    return {
        "contract_version": str(summary.get("contract_version") or ""),
        "contract_hash": str(summary.get("contract_hash") or ""),
        "triage": str(summary.get("triage") or ""),
        "fired_signals": ",".join(str(item) for item in signals),
        "primary_table": str(summary.get("primary_table") or ""),
        "ops_present": ",".join(str(item) for item in ops),
        "row_count_out": str(summary.get("row_count_out")),
        "column_count_out": str(summary.get("column_count_out")),
        "evidence_path": evidence_rel,
        "human_text": str(summary.get("human_text") or ""),
        "sample_size": str(sample_size),
        "sample_ref_kind": sample_kind,
        "output_is_empty_line": empty_line,
        "evidence_file_missing_line": "",
    }


def _escaped(fields: dict) -> dict:
    out = {}
    for key, value in fields.items():
        if key in ("output_is_empty_line", "evidence_file_missing_line"):
            out[key] = value
        else:
            out[key] = html.escape(value, quote=True)
    return out


def _fill(template: str, fields: dict) -> str:
    def repl(match):
        return fields.get(match.group(1), "")
    return _PLACEHOLDER.sub(repl, template)


def _root(dest_root: Optional[str]):
    if dest_root:
        return Path(dest_root)
    env = os.environ.get("FILEMATE_REPORT_ROOT")
    if env:
        return Path(env)
    return Path.cwd()


def _store(root: Path, rel: str, body: bytes, blocked: bool):
    if blocked:
        return None
    path = root.joinpath(*rel.split("/"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    except OSError:
        return None
    return rel
