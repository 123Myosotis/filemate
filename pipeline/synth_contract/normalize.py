# -*- coding: utf-8 -*-
"""
P0 A4/A5: mechanical normalize + null policy.
Ops and null tokens come from the contract version / AI NormalizeSpec — not invented here.
"""
from __future__ import annotations

import unicodedata
from typing import Any, Iterable, List, Optional, Sequence, Set

from .registry import ContractVersion
from .validate import ValidationIssue, ValidationResult


def apply_ops(value: Any, ops: Sequence[str], allowed_ops: Set[str]) -> Any:
    """Apply declared ops in order. Unknown op → leave value unchanged and caller flags."""
    if value is None:
        return None
    s = value if isinstance(value, str) else str(value)
    for op in ops:
        if op not in allowed_ops:
            continue
        if op == "trim":
            s = s.strip()
        elif op == "casefold":
            s = s.casefold()
        elif op == "nfkc":
            s = unicodedata.normalize("NFKC", s)
        elif op == "collapse_ws":
            s = " ".join(s.split())
    return s


def is_null(value: Any, null_tokens: Iterable[str]) -> bool:
    """
    DEPRECATED for executor compare path (R2 §6).
    Executor must use accuracy.classify_empty / compare_empty instead.
    Kept for unit tests of normalize_spec tooling only.
    """
    if value is None:
        return True
    s = str(value).strip()
    tokens = {str(t).casefold() for t in null_tokens}
    return s == "" or s.casefold() in tokens


def compare_with_null_policy(
    left: Any,
    right: Any,
    op: str,
    null_policy: str,
    null_tokens: Sequence[str],
) -> Optional[bool]:
    """
    DEPRECATED for executor compare path (R2 §6).
    Executor must not call this — use accuracy.compare_empty after classify_empty.
    """
    ln = is_null(left, null_tokens)
    rn = is_null(right, null_tokens)
    if null_policy == "reject_null" and (ln or rn):
        return None
    if null_policy == "exclude_null" and (ln or rn):
        return False
    if ln or rn:
        if null_policy == "eq_null":
            return ln and rn if op == "eq" else False
        if null_policy == "neq_null":
            return not (ln and rn) if op == "eq" else False
        return False
    if op == "eq":
        return left == right
    if op in ("le", "ge"):
        try:
            lf, rf = float(left), float(right)
        except (TypeError, ValueError):
            return False
        return lf <= rf if op == "le" else lf >= rf
    return False


def validate_normalize_spec(cv: ContractVersion, spec: dict, path: str = "normalize"
                            ) -> ValidationResult:
    r = ValidationResult(ok=True)
    if not isinstance(spec, dict):
        return r.fail("normalize_not_object", "normalize spec not object", path)
    for k in ("spec_id", "scope", "ops", "null_policy"):
        if k not in spec or spec[k] in (None, "", [], {}):
            r.fail("field_missing", "missing %s" % k, "%s.%s" % (path, k))
    if not r.ok:
        return r
    from .validate import validate_enum, validate_scope_field
    r = r.merge(validate_scope_field(spec.get("scope"), path + ".scope", "region"))
    r = r.merge(validate_enum(cv, "null_policy", spec.get("null_policy"), path + ".null_policy"))
    allowed = set(str(x) for x in (cv.data.get("normalize_ops") or []))
    ops = spec.get("ops")
    if not isinstance(ops, list) or not ops:
        return r.fail("ops_missing", "ops must be non-empty list", path + ".ops")
    for i, op in enumerate(ops):
        if str(op) not in allowed:
            r.fail(
                "normalize_op_unknown",
                "op %r not in contract normalize_ops" % op,
                "%s.ops[%d]" % (path, i),
            )
    return r


def normalize_key(
    value: Any,
    ops: Sequence[str],
    cv: ContractVersion,
    null_tokens: Optional[Sequence[str]] = None,
) -> Any:
    """String-ops only. Does not call is_null (executor-safe for non-compare use)."""
    allowed = set(str(x) for x in (cv.data.get("normalize_ops") or []))
    return apply_ops(value, ops, allowed)
