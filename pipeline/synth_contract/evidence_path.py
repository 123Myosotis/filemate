# -*- coding: utf-8 -*-
"""Evidence path for a simple-path delivery."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional, Tuple


def plan_id12(plan: dict) -> str:
    # json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") → sha256 → hexdigest()[:12]
    # Source is the plan before gate attaches _gated_*. Those keys stay on the plan for audit only.
    source = {
        key: value
        for key, value in plan.items()
        if not str(key).startswith("_gated_")
    }
    raw = json.dumps(
        source, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def evidence_relpath(plan: dict) -> str:
    ver = str(plan.get("contract_version") or "unknown")
    return "evidence/%s/%s.json" % (ver, plan_id12(plan))


def evidence_body(plan: dict, summary: dict) -> dict:
    return {
        "schema_version": "d3-evidence-1",
        "contract_version": plan.get("contract_version"),
        "contract_hash": plan.get("contract_hash"),
        "triage": "simple",
        "fired_signals": [],
        "ops_present": list(summary.get("ops_present") or []),
        "row_count_out": summary.get("row_count_out"),
        "column_count_out": summary.get("column_count_out"),
        "sample_ref": summary.get("sample_ref"),
    }


def write_evidence_file(
    plan: dict,
    body: dict,
    evidence_path: str,
    dest_root: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Write d3-evidence-1 to disk. Path must match evidence_path byte-for-byte.

    Returns (ok, error_token). error_token is for runtime_notes, not a triage signal.
    """
    expected = evidence_relpath(plan)
    if evidence_path != expected:
        return False, "evidence_write_failed path_mismatch"
    parts = evidence_path.replace("\\", "/").split("/")
    if len(parts) != 3 or parts[0] != "evidence" or not parts[2].endswith(".json"):
        return False, "evidence_write_failed path_shape"
    file_id = parts[2][:-len(".json")]
    if file_id != plan_id12(plan):
        return False, "evidence_write_failed id_mismatch"
    root = Path(dest_root) if dest_root else Path(
        os.environ.get("FILEMATE_REPORT_ROOT")
        or os.environ.get("FILEMATE_ACCEPTANCE_DIR")
        or os.getcwd()
    )
    target = root.joinpath(*parts)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        target.write_bytes(raw)
    except OSError:
        return False, "evidence_write_failed io"
    if not target.is_file():
        return False, "evidence_write_failed missing_after_write"
    return True, None
