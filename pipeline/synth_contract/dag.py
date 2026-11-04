# -*- coding: utf-8 -*-
"""
Region dependency DAG + review batch independence.
Cycle → trigger cycle; review batches must not share sample manifests.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Set, Tuple

from .validate import ValidationResult


def build_region_graph(regions: Sequence[dict]) -> Tuple[Dict[str, Set[str]], ValidationResult]:
    """
    regions[] items: region_id, depends_on[] (region ids).
    Returns adjacency (id → successors) and structural issues.
    """
    r = ValidationResult(ok=True)
    ids: Set[str] = set()
    for i, reg in enumerate(regions or []):
        if not isinstance(reg, dict):
            r.fail("region_bad", "not object", "regions[%d]" % i)
            continue
        rid = str(reg.get("region_id") or (reg.get("scope") or {}).get("region") or "")
        if not rid:
            r.fail("region_id_missing", "region_id missing", "regions[%d]" % i)
            continue
        if rid in ids:
            r.fail("region_id_dup", "duplicate region_id", "regions[%d]" % i)
        ids.add(rid)
    adj: Dict[str, Set[str]] = {rid: set() for rid in ids}
    indeg = {rid: 0 for rid in ids}
    for i, reg in enumerate(regions or []):
        if not isinstance(reg, dict):
            continue
        rid = str(reg.get("region_id") or (reg.get("scope") or {}).get("region") or "")
        if not rid:
            continue
        for dep in reg.get("depends_on") or []:
            d = str(dep)
            if d not in ids:
                r.fail("region_dep_unknown", "depends_on unknown: %s" % d,
                       "regions[%d].depends_on" % i)
                continue
            if rid not in adj[d]:
                adj[d].add(rid)
                indeg[rid] = indeg.get(rid, 0) + 1
    return adj, r


def detect_cycle(adj: Dict[str, Set[str]]) -> Optional[List[str]]:
    """Return one cycle path if present, else None. Kahn + leftover nodes."""
    indeg = {n: 0 for n in adj}
    for n, succs in adj.items():
        for s in succs:
            indeg[s] = indeg.get(s, 0) + 1
            if s not in indeg:
                indeg[s] = indeg.get(s, 0)
    from collections import deque
    q = deque([n for n, d in indeg.items() if d == 0])
    seen = 0
    while q:
        n = q.popleft()
        seen += 1
        for s in adj.get(n) or []:
            indeg[s] -= 1
            if indeg[s] == 0:
                q.append(s)
    if seen == len(indeg):
        return None
    leftover = [n for n, d in indeg.items() if d > 0]
    return leftover


def validate_region_dag(regions: Sequence[dict]) -> ValidationResult:
    adj, r = build_region_graph(regions)
    if not r.ok and not adj:
        return r
    cyc = detect_cycle(adj)
    if cyc:
        r.fail(
            "region_dag_cycle",
            "cycle among regions: %s" % ",".join(cyc),
            "regions",
            state_id="fail",
        )
    return r


def validate_review_batch_independence(batches: Sequence[dict]) -> ValidationResult:
    """
    Review batches must not share sample_manifest_id or overlapping coord sets
    when mode requires independence.
    """
    r = ValidationResult(ok=True)
    manifests: Dict[str, int] = {}
    coord_sets: List[Tuple[int, Set[str]]] = []
    for i, b in enumerate(batches or []):
        if not isinstance(b, dict):
            r.fail("review_batch_bad", "not object", "review_batches[%d]" % i)
            continue
        mid = str(b.get("sample_manifest_id") or "")
        if mid:
            if mid in manifests:
                r.fail(
                    "review_batch_share_manifest",
                    "sample_manifest_id reused across batches",
                    "review_batches[%d]" % i,
                    state_id="review_reread",
                )
            manifests[mid] = i
        coords = set(str(x) for x in (b.get("coords") or []))
        if coords:
            for j, prev in coord_sets:
                if coords & prev:
                    r.fail(
                        "review_batch_coord_overlap",
                        "review batch coords overlap batches %d and %d" % (j, i),
                        "review_batches[%d]" % i,
                        state_id="review_reread",
                    )
                    break
            coord_sets.append((i, coords))
        # plan vs review anchors inside batch
        if b.get("plan_anchor") is not None or b.get("review_anchor") is not None:
            pa = b.get("plan_anchor") or {}
            ra = b.get("review_anchor") or {}
            mode = str(b.get("review_anchor_mode") or "")
            if mode == "disjoint_sample":
                a = str(pa.get("sample_manifest_id") or "")
                bb = str(ra.get("sample_manifest_id") or "")
                sa = set(str(x) for x in (pa.get("coords") or []))
                sb = set(str(x) for x in (ra.get("coords") or []))
                if not a or not bb or a == bb or (sa & sb):
                    r.fail(
                        "review_reread",
                        "batch review sample not disjoint",
                        "review_batches[%d]" % i,
                        state_id="review_reread",
                    )
    return r


def enforce_dag_and_review(plan: dict) -> ValidationResult:
    r = ValidationResult(ok=True)
    if plan.get("regions") is not None:
        # only DAG-check when depends_on appears
        regs = plan.get("regions") or []
        if any(isinstance(x, dict) and x.get("depends_on") is not None for x in regs):
            r = r.merge(validate_region_dag(regs))
        elif any(isinstance(x, dict) and x.get("region_id") for x in regs):
            # still reject duplicate ids
            r = r.merge(validate_region_dag(regs))
    if plan.get("review_batches") is not None:
        r = r.merge(validate_review_batch_independence(plan.get("review_batches") or []))
    return r
