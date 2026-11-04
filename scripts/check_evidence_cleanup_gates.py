# -*- coding: utf-8 -*-
"""Evidence cleanup gates — print PASS/FAIL summary only (anti-OOM)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACC = ROOT / "acceptance" / "cross_table"
ARCHIVE = ACC / "archive"
README = ROOT / "MEMORY_RAG_README.md"
EV_ROOT = ARCHIVE / "evidence_root_2026-10-21"
EXT_ARCH = ARCHIVE / "external_branch_real_run_2026-10-22"
TRACE_ARCH = ARCHIVE / "trace_misc_2026-10-22"

KEEP_EXT = {
    "external_branch_real_run_20261022T040016Z.json",
    "external_branch_real_run_20261022T030655Z.json",
    "external_branch_real_run_20261022T024941Z.json",
}
MIG_EXT_NAMES = {
    "external_branch_real_run_20261022T033758Z.json",
    "external_branch_real_run_20261022T034536Z.json",
    "external_branch_real_run_20261022T035049Z.json",
}

# Planned phase-A accounting (scheme): total 395 = in_place 267 + migrated 128
PLAN_TOTAL = 395
PLAN_MIGRATED = 128
PLAN_IN_PLACE = 267


def main() -> int:
    errors: list[str] = []

    # 1) acceptance/cross_table root EVIDENCE*.md == 0 (and repo root)
    root_ev = list(ROOT.glob("EVIDENCE*")) + list(ROOT.glob("EVIDENCE_*.md"))
    ct_root_ev = list(ACC.glob("EVIDENCE*")) + list(ACC.glob("EVIDENCE_*.md"))
    root_evidence = len(root_ev) + len(ct_root_ev)
    if root_evidence != 0:
        errors.append(f"root_evidence={root_evidence}")

    # 2) migrated / in_place / total reconciliation
    # Exclude README-slim detail docs from cleanup accounting (they are docs, not evidence).
    detail_md = {
        "MEMORY_RAG_AUDIT_DETAILS.md",
        "MEMORY_RAG_LEDGER_DETAILS.md",
        "MEMORY_RAG_P0_P1_DETAILS.md",
        "MEMORY_RAG_D7_PLAN_DETAILS.md",
    }

    def _excluded_from_cleanup_accounting(p: Path) -> bool:
        # README-slim detail docs (not evidence-cleanup baseline)
        try:
            rel_a = p.relative_to(ARCHIVE)
            if len(rel_a.parts) == 1 and rel_a.name in detail_md:
                return True
        except ValueError:
            pass
        # P1-pre evidence packs written under query_cases/ (docs, not cleanup set)
        name = p.name
        if name.startswith("p1_pre_"):
            return True
        return False

    all_files = [p for p in ACC.rglob("*") if p.is_file()]
    accounted = [p for p in all_files if not _excluded_from_cleanup_accounting(p)]
    total = len(accounted)
    mig_ext = sum(1 for n in MIG_EXT_NAMES if (EXT_ARCH / n).is_file())
    mig_trace = (
        sum(1 for p in TRACE_ARCH.rglob("*") if p.is_file()) if TRACE_ARCH.is_dir() else 0
    )
    migrated = mig_ext + mig_trace
    in_place = total - migrated

    if total != PLAN_TOTAL:
        errors.append(f"total={total} expected={PLAN_TOTAL}")
    if migrated != PLAN_MIGRATED:
        errors.append(f"migrated={migrated} expected={PLAN_MIGRATED}")
    if in_place != PLAN_IN_PLACE:
        errors.append(f"in_place={in_place} expected={PLAN_IN_PLACE}")
    if PLAN_TOTAL != in_place + migrated:
        errors.append(f"arith {PLAN_TOTAL}!={in_place}+{migrated}")

    # keep ext still in query_cases; mig ext NOT in query_cases
    qc = ACC / "query_cases"
    for n in KEEP_EXT:
        if not (qc / n).is_file():
            errors.append(f"missing_keep:{n}")
    for n in MIG_EXT_NAMES:
        if (qc / n).exists():
            errors.append(f"stale_qc:{n}")
        if not (EXT_ARCH / n).is_file():
            errors.append(f"missing_arch_ext:{n}")

    # 3) README index ↔ evidence_root 1:1; no broken links for indexed files
    broken = 0
    md = README.read_text(encoding="utf-8") if README.is_file() else ""
    # filenames in evidence index table
    # only real filenames (no globs like EVIDENCE_*.md)
    indexed = re.findall(r"`(EVIDENCE(?:_[A-Za-z0-9_]+)?\.md)`", md)
    seen: set[str] = set()
    uniq: list[str] = []
    for fn in indexed:
        if "*" in fn or fn in seen:
            continue
        seen.add(fn)
        uniq.append(fn)
    for fn in uniq:
        if not (EV_ROOT / fn).is_file():
            broken += 1
            errors.append(f"index_miss:{fn}")
    # archive detail docs linked
    for name in (
        "MEMORY_RAG_AUDIT_DETAILS.md",
        "MEMORY_RAG_LEDGER_DETAILS.md",
        "MEMORY_RAG_P0_P1_DETAILS.md",
        "MEMORY_RAG_D7_PLAN_DETAILS.md",
    ):
        if name in md and not (ARCHIVE / name).is_file():
            broken += 1
            errors.append(f"detail_miss:{name}")
    # stale query_cases paths for migrated externals
    for n in MIG_EXT_NAMES:
        if f"query_cases/{n}" in md.replace("\\", "/"):
            broken += 1
            errors.append(f"stale_readme_path:{n}")

    ok = not errors
    status = "PASS" if ok else "FAIL"
    print(
        f"[{status}] root_evidence={root_evidence}, "
        f"migrated={migrated}, in_place={in_place}, total={total}, "
        f"broken_links={broken}"
    )
    if not ok:
        # one short reason line only (no file dumps)
        print(f"[FAIL_DETAIL] count={len(errors)} first={errors[0]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
