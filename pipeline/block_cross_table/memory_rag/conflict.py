# -*- coding: utf-8 -*-
"""MemoryConflictPackage → E (no silent overwrite)."""
from __future__ import annotations

from typing import Any, Sequence
from uuid import uuid4

from pipeline.block_cross_table.memory_rag.constants import TRACE_CONFLICT
from pipeline.block_cross_table.query_engine.ie_query import (
    build_five_part_ask,
    route_e_entry,
)


def build_memory_conflict_package(
    *,
    candidate_binding_ids: Sequence[str],
    conflict_kind: str,
    source_trace_id: str,
    speculation: str | None = None,
    evidence: str | None = None,
) -> dict[str, Any]:
    """Assemble conflict package; does not pick a winner locally."""
    ids = [str(x) for x in candidate_binding_ids]
    if len(ids) < 2 and conflict_kind != "stale_vs_new":
        # still allow single-id packages for stale_vs_new with external new
        pass
    five = build_five_part_ask(
        speculation=speculation
        or "memory bindings conflict; local layer cannot choose",
        evidence=evidence
        or ("candidate_binding_ids=%s kind=%s" % (ids, conflict_kind)),
        what_might_be_wrong="multiple confirmed bindings may disagree",
        next_step="user confirms which binding remains active",
        please_confirm_or_correct="confirm or correct the intended binding",
    )
    conflict_id = str(uuid4())
    package = {
        "conflict_id": conflict_id,
        "candidate_binding_ids": ids,
        "conflict_kind": conflict_kind,
        "five_part_ask_ref": five,
        "source_trace_id": source_trace_id,
        "silent_overwrite_forbidden": True,
    }
    routed = route_e_entry(source="qp_needs_user", five_part=five)
    traces = [
        {
            "kind": TRACE_CONFLICT,
            "conflict_id": conflict_id,
            "candidate_binding_ids": ids,
            "conflict_kind": conflict_kind,
            "e_entry": routed.get("e_entry"),
        }
    ]
    return {
        "package": package,
        "e_route": routed,
        "traces": traces,
    }


def detect_multi_binding_conflict(
    accepted_candidates: Sequence[dict[str, Any]],
    *,
    source_trace_id: str,
) -> dict[str, Any] | None:
    """
    If ≥2 accepted share the same utterance_span_fingerprint → conflict package.
    Otherwise None (no silent merge).
    """
    by_fp: dict[str, list[str]] = {}
    for c in accepted_candidates:
        fp = str(c.get("utterance_span_fingerprint") or "")
        bid = str(c.get("binding_id") or "")
        if not fp or not bid:
            continue
        by_fp.setdefault(fp, []).append(bid)
    for fp, bids in by_fp.items():
        uniq = sorted(set(bids))
        if len(uniq) >= 2:
            return build_memory_conflict_package(
                candidate_binding_ids=uniq,
                conflict_kind="multi_binding",
                source_trace_id=source_trace_id,
                evidence="shared_utterance_span_fingerprint=%s ids=%s" % (fp, uniq),
            )
    return None
